"""Scheduler heartbeat: due jobs must never block the loop.

Regression for the nightly 'startup-recovery / failed' entries: the
scheduled full update used to run inline in the scheduler loop, which froze
last_tick for the whole run. /healthz then answered 503, the container
healthcheck flagged the service unhealthy and it was restarted mid-run.
"""
import threading
import time


def _fresh_scheduler(monkeypatch):
    from app import scheduler as smod
    s = smod.Scheduler()
    now = time.time()
    s.next_check = now + 3600
    s.next_full_update = now + 3600
    monkeypatch.setattr(smod, "_save_next", lambda *a, **k: None)
    monkeypatch.setattr(smod, "load", lambda *a, **k: {})
    return smod, s


class TestTickNeverBlocks:
    def test_scheduled_update_runs_in_worker_thread(self, tmp_state, monkeypatch):
        smod, s = _fresh_scheduler(monkeypatch)
        started, release = threading.Event(), threading.Event()

        def slow_update(runlog):
            started.set()
            release.wait(5)
            return True

        monkeypatch.setattr(smod.updater, "run_full_update", slow_update)
        s.next_full_update = 0  # due now

        t0 = time.time()
        s._tick()
        assert time.time() - t0 < 1.0, "tick must hand the update to a worker"
        assert started.wait(2), "update worker never started"
        assert s.is_busy()

        # the heartbeat keeps advancing while the update is still running
        before = s.last_tick
        time.sleep(0.01)
        s._tick()
        assert s.last_tick > before
        assert s.next_full_update > time.time(), "slot must be bumped"

        release.set()
        for _ in range(100):
            if not s.is_busy():
                break
            time.sleep(0.02)
        assert not s.is_busy()

    def test_busy_gate_does_not_start_second_update(self, tmp_state, monkeypatch):
        smod, s = _fresh_scheduler(monkeypatch)
        calls = []
        monkeypatch.setattr(smod.updater, "run_full_update",
                            lambda rl: calls.append(rl) or True)
        assert smod.gate.try_acquire("manual-test")
        try:
            s.next_full_update = 0
            s._tick()
            time.sleep(0.05)
            assert calls == []
            assert s.next_full_update > time.time()
        finally:
            smod.gate.release()

    def test_check_runs_in_worker_and_unconfigured_retries_soon(self, tmp_state,
                                                                 monkeypatch):
        smod, s = _fresh_scheduler(monkeypatch)
        monkeypatch.setattr(s, "_client", lambda: None)  # not configured yet
        s.next_check = 0
        s._tick()
        for _ in range(100):
            if not s.is_busy():
                break
            time.sleep(0.02)
        # the worker's 10-minute retry must win over the regular interval
        assert s.next_check - time.time() <= 601
