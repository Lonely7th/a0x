from datetime import datetime

from assistant.config import SHANGHAI
from assistant.worker import afterclose_deadline


def test_afterclose_sync_can_catch_up_after_cutoff():
    before = datetime(2026, 10, 12, 19, 0, tzinfo=SHANGHAI)
    after = datetime(2026, 10, 12, 21, 0, tzinfo=SHANGHAI)
    assert afterclose_deadline(before) == before.replace(hour=20, minute=30)
    assert afterclose_deadline(after) is None


def test_progress_stall_and_recovery(monkeypatch):
    from assistant import progress as module
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    tracker = module.ProgressTracker(timeout=60)
    clock[0] += 61
    assert tracker.snapshot()["stalled"]
    tracker.touch("gap request finished")
    assert not tracker.snapshot()["stalled"]
    assert tracker.snapshot()["stage"] == "gap request finished"


def test_watchdog_dumps_stacks_and_exits_for_systemd_restart(monkeypatch):
    import pytest
    from assistant import worker
    calls = []
    monkeypatch.setattr(worker.faulthandler, "dump_traceback",
                        lambda **kwargs: calls.append("dump"))

    def exit_process(code):
        calls.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(worker.os, "_exit", exit_process)
    worker.restart_if_stalled({"stalled": False})
    assert calls == []
    with pytest.raises(SystemExit):
        worker.restart_if_stalled({"stalled": True, "stage": "gap query",
                                   "age_seconds": 601})
    assert calls == ["dump", 1]
