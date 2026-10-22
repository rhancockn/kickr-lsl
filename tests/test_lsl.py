import xml.etree.ElementTree as ET

import pylsl

from wahoo_lsl import __version__
from wahoo_lsl.lsl import Channel, add_dict, sanitize, stream_info

CHANNELS = [Channel("power_w", "W", "measured", "instantaneous power"), Channel("speed_kmh", "km/h", "derived")]


def xml(info: pylsl.StreamInfo) -> ET.Element:
    return ET.fromstring(info.as_xml())


def test_sanitize_keeps_alphanumerics():
    assert sanitize("KICKR V6-ä!") == "KICKRV6"


def test_stream_info_identity_and_format():
    info = stream_info("KICKR-CPS-abcdef", "CPS", "KICKR-CPS-abcdef", CHANNELS, 1.0)
    assert info.name() == "KICKR-CPS-abcdef"
    assert info.source_id() == "KICKR-CPS-abcdef"
    assert info.type() == "CPS"
    assert info.channel_count() == 2
    assert info.nominal_srate() == 1.0
    assert info.channel_format() == pylsl.cf_double64


def test_stream_info_desc_acquisition_and_channels():
    info = stream_info("n", "CPS", "s", CHANNELS, 0.0, acquisition={"clock": "local_clock", "skip": None})
    root = xml(info)
    acq = root.find("desc/acquisition")
    assert acq.findtext("software") == "wahoo_lsl"
    assert acq.findtext("version") == __version__
    assert acq.findtext("clock") == "local_clock"
    assert acq.find("skip") is None
    chans = root.findall("desc/channels/channel")
    assert [(c.findtext("label"), c.findtext("unit"), c.findtext("type")) for c in chans] == [
        ("power_w", "W", "measured"),
        ("speed_kmh", "km/h", "derived"),
    ]
    assert chans[0].findtext("description") == "instantaneous power"


def test_add_dict_stringifies_and_skips_none():
    info = stream_info("n", "CPS", "s", CHANNELS, 1.0)
    add_dict(info.desc(), "device", {"name": "KICKR", "features_raw": 4622, "missing": None})
    dev = xml(info).find("desc/device")
    assert dev.findtext("name") == "KICKR"
    assert dev.findtext("features_raw") == "4622"
    assert dev.find("missing") is None
