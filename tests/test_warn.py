import logging
import threading

from wahoo_lsl.warn import WarnOnce


def test_warns_once_per_key(caplog):
    w = WarnOnce(logging.getLogger("t"))
    with caplog.at_level(logging.WARNING, logger="t"):
        assert w.warn("a", "msg %s", 1) is True
        assert w.warn("a", "msg %s", 2) is False
        assert w.warn("b", "other") is True
    assert [r.getMessage() for r in caplog.records] == ["msg 1", "other"]
    assert w.counts() == {"a": 2, "b": 1}


def test_check_records_without_logging(caplog):
    w = WarnOnce(logging.getLogger("t"))
    with caplog.at_level(logging.DEBUG, logger="t"):
        assert w.check(("k", 1)) is True
        assert w.check(("k", 1)) is False
    assert caplog.records == []
    assert w.counts() == {("k", 1): 2}


def test_thread_safe_single_emit(caplog):
    w = WarnOnce(logging.getLogger("t"))
    results = []

    def worker():
        for _ in range(200):
            results.append(w.warn("same", "x"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    with caplog.at_level(logging.WARNING, logger="t"):
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert results.count(True) == 1
    assert w.counts()["same"] == 1600
    assert len(caplog.records) == 1
