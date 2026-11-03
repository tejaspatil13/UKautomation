"""The updater's check: one at a time, and it always records when it finished."""
import threading

from desktop.updater import Updater


def make_updater(check):
    u = Updater("", "dev")  # disabled; wire a fake Velopack manager in
    u._um, u.status = type("UM", (), {"check_for_updates": staticmethod(check)})(), "idle"
    return u


def test_no_update_returns_to_idle_and_records_the_check():
    u = make_updater(lambda: None)
    u.check_and_download()
    assert u.status == "idle" and u.checked_at > 0


def test_offline_check_is_reported_and_the_next_one_can_run():
    def fail():
        raise OSError("offline")
    u = make_updater(fail)
    u.check_and_download()
    assert u.status == "error" and "internet" in u.error
    u._um.check_for_updates = staticmethod(lambda: None)
    u.check_and_download()  # the lock was released
    assert u.status == "idle"


def test_a_second_press_while_checking_does_nothing():
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow():
        calls.append(1)
        started.set()
        release.wait(5)
    u = make_updater(slow)
    t = threading.Thread(target=u.check_and_download)
    t.start()
    started.wait(5)
    u.check_and_download()  # button pressed again: returns at once
    release.set()
    t.join(5)
    assert calls == [1]


def test_an_update_left_waiting_becomes_required(monkeypatch):
    import time as _time

    from desktop import updater as mod

    info = type("Info", (), {"TargetFullRelease": type("R", (), {"Version": "1.0.1"})()})()
    u = make_updater(lambda: info)
    u._um.download_updates = staticmethod(lambda i, cb: None)
    u._required_after = mod.REQUIRED_AFTER_S["stable"]
    u.check_and_download()
    assert u.status == "ready" and not u.required  # "Later" is allowed at first
    later = _time.time() + 25 * 3600
    monkeypatch.setattr(mod.time, "time", lambda: later)
    assert u.required and u.as_dict()["required"]  # a day later: must install


def test_client_and_test_builds_check_every_15_minutes():
    from desktop import updater as mod

    assert mod.CHECK_EVERY_S == {"stable": 900, "test": 900}
    assert mod.REQUIRED_AFTER_S["stable"] == 24 * 3600
