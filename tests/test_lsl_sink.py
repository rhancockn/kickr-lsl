import math
import uuid
import xml.etree.ElementTree as ET

import pylsl
import pytest
from fakes import FakeOutlet

from wahoo_lsl.config import parse_config
from wahoo_lsl.pipeline import Event, Sample
from wahoo_lsl.sinks.lsl_sink import LslSink, channel_keys, stream_id

ADDRESS = "CF46355D-B7CF-4353-8FA5-9F2DB2F24BFA"
ALL = ("power", "wheel_revolutions", "accumulated_torque", "crank_revolutions")
KEYS = [
    "power_w",
    "wheel_revs",
    "wheel_event_t",
    "accumulated_torque_nm",
    "torque_source",
    "crank_revs",
    "crank_event_t",
    "speed_kmh",
    "cadence_rpm",
]


def cfg(measurements=ALL, **sections):
    return parse_config({"device": {"name": "KICKR"}, "measurements": list(measurements), **sections})


def connected(name="Wahoo KICKR 58EC", address=ADDRESS, device_info=None):
    detail = {"name": name, "address": address, "address_kind": "uuid", "device_info": device_info or {}}
    return Event("connected", 1.0, 0, detail)


def features(supported=ALL):
    detail = {"raw": 4622, "supported": list(supported), "unavailable": [], "sensor_location": "Other"}
    return Event("features", 1.0, 0, detail)


def sample(t, fields=None, derived=None, parse_error=None):
    return Sample(0, t, 0, "", 0, fields or {}, parse_error, derived or {})


class Factory:
    def __init__(self, fail_times=0):
        self.outlets: list[FakeOutlet] = []
        self.fail_times = fail_times

    def __call__(self, info):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("liblsl unavailable")
        outlet = FakeOutlet(info)
        self.outlets.append(outlet)
        return outlet


def rows(outlet):
    return [r for chunk, _ in outlet.chunks for r in chunk]


def same(a, b):
    return len(a) == len(b) and all((math.isnan(x) and math.isnan(y)) or x == y for x, y in zip(a, b))


@pytest.mark.parametrize(
    "name, address, device_info, expected",
    [
        ("Wahoo KICKR 58EC", ADDRESS, {}, "KICKR-CPS-f24bfa"),
        ("Wahoo KICKR 58EC", ADDRESS, {"model_number": "KICKR V6"}, "KICKRV6-CPS-f24bfa"),
        ("KICKR CORE 1234", "AA:BB:CC:DD:EE:01", {}, "KICKRCORE-CPS-ddee01"),
        (None, "AA:BB:CC:DD:EE:01", {}, "DEVICE-CPS-ddee01"),
        ("Wahoo 58EC", "AA:BB:CC:DD:EE:01", {"model_number": " "}, "DEVICE-CPS-ddee01"),
    ],
)
def test_stream_id(name, address, device_info, expected):
    assert stream_id(name, address, device_info) == expected


def test_channel_keys_follow_config_order_and_support():
    assert channel_keys(ALL, set(ALL)) == KEYS
    # torque unsupported, no crank support -> no torque channels, no cadence
    assert channel_keys(ALL, {"power", "wheel_revolutions"}) == ["power_w", "wheel_revs", "wheel_event_t", "speed_kmh"]
    # derived speed needs only device support, not configuration
    assert channel_keys(("power",), {"power", "wheel_revolutions"}) == ["power_w", "speed_kmh"]


def test_outlet_created_on_features_with_identity_and_metadata():
    factory = Factory()
    sink = LslSink(cfg(derive={"wheel_circumference_m": 2.096}), factory)
    sink.handle_batch([connected(device_info={"manufacturer": "Wahoo Fitness"})])
    assert factory.outlets == []  # nothing until the device's features are known
    sink.handle_batch([features()])
    (outlet,) = factory.outlets
    info = outlet.info
    assert (info.name(), info.source_id(), info.type()) == ("KICKR-CPS-f24bfa",) * 2 + ("CPS",)
    assert info.nominal_srate() == 1.0
    assert info.channel_format() == pylsl.cf_double64
    root = ET.fromstring(info.as_xml())
    chans = root.findall("desc/channels/channel")
    assert [c.findtext("label") for c in chans] == KEYS
    assert [c.findtext("unit") for c in chans][-2:] == ["km/h", "rpm"]
    assert [c.findtext("type") for c in chans][-3:] == ["measured", "derived", "derived"]
    assert "wheel=0, crank=1" in chans[KEYS.index("torque_source")].findtext("description")
    dev = root.find("desc/device")
    assert dev.findtext("name") == "Wahoo KICKR 58EC"
    assert dev.findtext("address") == ADDRESS
    assert dev.findtext("sensor_location") == "Other"
    assert dev.findtext("features_raw") == "4622"
    assert dev.findtext("manufacturer") == "Wahoo Fitness"
    assert root.find("desc/derivation").findtext("wheel_circumference_m") == "2.096"
    acq = root.find("desc/acquisition")
    assert acq.findtext("software") == "wahoo_lsl"
    assert acq.findtext("nominal_srate") == "1.0"


def test_irregular_rate():
    factory = Factory()
    sink = LslSink(cfg(lsl={"nominal_srate": 0}), factory)
    sink.handle_batch([connected(), features()])
    assert factory.outlets[0].info.nominal_srate() == pylsl.IRREGULAR_RATE


def test_rows_values_encodings_nan_and_timestamps():
    factory = Factory()
    sink = LslSink(cfg(), factory)
    full = {
        "power_w": 200,
        "wheel_revs": 5,
        "wheel_event_t": 2048,
        "accumulated_torque_nm": 1.5,
        "torque_source": "crank",
        "crank_revs": 3,
        "crank_event_t": 1024,
        "offset_compensation": False,
    }
    sink.handle_batch(
        [
            connected(),
            features(),
            sample(10.0, full, {"speed_kmh": 30.0, "cadence_rpm": None}),
            sample(11.0, {"power_w": 150}),  # other fields absent
            sample(12.0, parse_error="truncated"),  # skipped
        ]
    )
    (outlet,) = factory.outlets
    assert len(outlet.chunks) == 1  # one push per batch
    (chunk, stamps) = outlet.chunks[0]
    assert stamps == [10.0, 11.0]
    nan = math.nan
    assert same(chunk[0], [200.0, 5.0, 2048.0, 1.5, 1.0, 3.0, 1024.0, 30.0, nan])
    assert same(chunk[1], [150.0] + [nan] * 8)


def test_offset_compensation_bool_encoding():
    factory = Factory()
    sink = LslSink(cfg(("power", "offset_compensation")), factory)
    sink.handle_batch([connected(), features(("power", "offset_compensation")), sample(1.0, {"offset_compensation": True})])
    assert same(rows(factory.outlets[0])[0], [math.nan, 1.0])


def test_outlet_kept_on_same_reconnect():
    factory = Factory()
    sink = LslSink(cfg(), factory)
    sink.handle_batch([connected(), features(), sample(1.0, {"power_w": 1})])
    sink.handle_batch([connected(), features(), sample(5.0, {"power_w": 2})])
    (outlet,) = factory.outlets
    assert [r[0] for r in rows(outlet)] == [1.0, 2.0]


def test_outlet_replaced_on_change_flushes_old_rows_first(caplog):
    factory = Factory()
    sink = LslSink(cfg(), factory)
    with caplog.at_level("INFO"):
        sink.handle_batch(
            [
                connected(),
                features(),
                sample(1.0, {"power_w": 1}),
                connected(address="AA:BB:CC:DD:EE:01"),
                features(),
                sample(2.0, {"power_w": 2}),
            ]
        )
    old, new = factory.outlets
    assert [r[0] for r in rows(old)] == [1.0]
    assert [r[0] for r in rows(new)] == [2.0]
    assert new.info.source_id() == "KICKR-CPS-ddee01"
    assert any("Replacing LSL outlet" in r.getMessage() for r in caplog.records)


def test_creation_failure_drops_samples_until_next_features(caplog):
    factory = Factory(fail_times=1)
    sink = LslSink(cfg(), factory)
    with caplog.at_level("ERROR"):
        sink.handle_batch([connected(), features(), sample(1.0, {"power_w": 1})])
    assert factory.outlets == []
    assert any("Cannot create LSL outlet" in r.getMessage() for r in caplog.records)
    sink.handle_batch([connected(), features(), sample(2.0, {"power_w": 2})])
    assert [r[0] for r in rows(factory.outlets[0])] == [2.0]


def test_push_failure_does_not_resend_rows():
    factory = Factory()
    sink = LslSink(cfg(), factory)
    sink.handle_batch([connected(), features()])
    outlet = factory.outlets[0]
    original = outlet.push_chunk

    def broken(rows, stamps):
        raise RuntimeError("push failed")

    outlet.push_chunk = broken
    with pytest.raises(RuntimeError):
        sink.handle_batch([sample(1.0, {"power_w": 1})])
    outlet.push_chunk = original
    sink.handle_batch([sample(2.0, {"power_w": 2})])
    assert [r[0] for r in rows(outlet)] == [2.0]


def test_stop_releases_outlet():
    factory = Factory()
    sink = LslSink(cfg(), factory)
    sink.handle_batch([connected(), features()])
    sink.stop()
    sink.handle_batch([sample(1.0, {"power_w": 1})])
    assert rows(factory.outlets[0]) == []


def test_loopback_real_outlet_reaches_an_inlet():
    address = uuid.uuid4().hex
    sink = LslSink(cfg())
    sink.handle_batch([connected(address=address), features()])
    source_id = f"KICKR-CPS-{address[-6:]}"
    streams = pylsl.resolve_byprop("source_id", source_id, timeout=5.0)
    if not streams:
        sink.stop()
        pytest.skip("LSL stream not resolvable on this host (multicast blocked?)")
    inlet = pylsl.StreamInlet(streams[0])
    try:
        inlet.open_stream(timeout=5.0)
        now = pylsl.local_clock()
        sink.handle_batch([sample(now, {"power_w": 123}, {"speed_kmh": 25.0, "cadence_rpm": 90.0})])
        got: list = []
        deadline = pylsl.local_clock() + 5.0
        while not got and pylsl.local_clock() < deadline:
            got, _ = inlet.pull_chunk(timeout=0.5)
        assert got, "no samples received"
        assert got[0][0] == 123.0
        assert got[0][-2:] == [25.0, 90.0]
        assert inlet.info().source_id() == source_id
    finally:
        inlet.close_stream()
        sink.stop()


def _break(outlet):
    def broken(rows, stamps):
        raise RuntimeError("push failed")

    outlet.push_chunk = broken


def test_push_failure_before_device_change_still_switches_outlet():
    factory = Factory()
    sink = LslSink(cfg(), factory)
    sink.handle_batch([connected(), features()])
    old = factory.outlets[0]
    _break(old)
    with pytest.raises(RuntimeError):
        sink.handle_batch(
            [
                sample(1.0, {"power_w": 1}),
                connected(address="AA:BB:CC:DD:EE:01"),
                features(),
                sample(2.0, {"power_w": 2}),
            ]
        )
    old_obj, new = factory.outlets
    assert old_obj is old
    assert new.info.source_id() == "KICKR-CPS-ddee01"
    assert [r[0] for r in rows(new)] == [2.0]
    sink.handle_batch([sample(3.0, {"power_w": 3})])
    assert [r[0] for r in rows(new)] == [2.0, 3.0]
    assert rows(old) == []


def test_stop_releases_outlet_even_if_flush_fails():
    factory = Factory()
    sink = LslSink(cfg(), factory)
    sink.handle_batch([connected(), features()])
    _break(factory.outlets[0])
    with pytest.raises(RuntimeError):
        sink.handle_batch([sample(1.0, {"power_w": 1})])
    sink.stop()
    assert sink._outlet is None


def test_reconnect_without_dis_model_keeps_outlet():
    factory = Factory()
    sink = LslSink(cfg(), factory)
    sink.handle_batch([connected(device_info={"model_number": "KICKR V6"}), features()])
    assert factory.outlets[0].info.source_id() == "KICKRV6-CPS-f24bfa"
    sink.handle_batch([connected(device_info={}), features(), sample(5.0, {"power_w": 2})])
    assert len(factory.outlets) == 1
    assert rows(factory.outlets[0])[-1][0] == 2.0


def test_reconnect_with_different_name_keeps_outlet():
    factory = Factory()
    sink = LslSink(cfg(), factory)
    sink.handle_batch([connected(name="Wahoo KICKR 58EC"), features()])
    sink.handle_batch([connected(name="KICKR"), features()])
    sink.handle_batch([connected(name="KICKR CORE 1234"), features()])
    assert len(factory.outlets) == 1
