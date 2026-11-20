"""Stability regressions from the backend review: the job gate is never
left locked, runs never stay 'running', scheduled runs are not silently
skipped, schedule writes are thread-safe, schedule changes apply at once."""
import threading
import time

import pytest

from app.portainer import PortainerError


@pytest.fixture()
def api(tmp_state, fake_configured, monkeypatch):
    from app import main
    main.app.config["TESTING"] = True
    with main.app.test_client() as c:
        yield c, main


class _Boom:
    endpoint_id = 3
    def stacks(self): raise PortainerError("portainer unreachable")


class _OneStack:
    endpoint_id = 3
    def stacks(self): return [{"Id": 1, "Name": "web", "EndpointId": 3}]
    def prune_images(self, eid=None): raise PortainerError("HTTP 403 admin only")
    def prune_build_cache(self, eid=None): return {}


def _wait_idle():
    from app.jobs import gate
    for _ in range(200):
        if not gate.is_busy():
            return True
        time.sleep(0.01)
    return False


class TestGateNeverLeaks:
    def test_rollback_with_portainer_down(self, api, monkeypatch):
        c, main = api
        monkeypatch.setattr(main, "_client", lambda: _Boom())
        assert c.post("/api/stacks/1/rollback", json={}).status_code == 502
        from app.jobs import gate
        assert not gate.is_busy()

    def test_unexpected_exception_finishes_run(self, api, monkeypatch):
        c, main = api
        from app import history
        monkeypatch.setattr(main, "_client", lambda: _OneStack())
        monkeypatch.setattr(main.updater, "redeploy_single_stack",
                            lambda *a: (_ for _ in ()).throw(OSError("disk")))
        r = c.post("/api/stacks/1/redeploy")
        assert r.status_code == 200 and r.get_json()["started"]
        assert _wait_idle()
        last = history.history()[-1]
        assert last["kind"] == "redeploy" and last["success"] is False
        assert history.current_status()["state"] == "failed"

    def test_failed_prune_is_reported_as_failed(self, api, monkeypatch):
        c, main = api
        from app import history
        monkeypatch.setattr(main, "_client", lambda: _OneStack())
        assert c.post("/api/prune").status_code == 200
        assert _wait_idle()
        assert history.history()[-1]["success"] is False


class TestScheduler:
    def _sched(self, monkeypatch):
        from app import scheduler as smod
        s = smod.Scheduler()
        now = time.time()
        s.next_check, s.next_full_update = now + 3600, now + 3600
        s._spec_seen = smod._spec()
        monkeypatch.setattr(smod, "load", lambda *a, **k: {})
        return smod, s

    def test_slot_retried_when_other_job_holds_gate(self, tmp_state, monkeypatch):
        smod, s = self._sched(monkeypatch)
        assert smod.gate.try_acquire("cleanup")
        try:
            s.next_full_update = 0
            s._tick()
            assert 0 < s.next_full_update - time.time() <= 61
        finally:
            smod.gate.release()

    def test_schedule_change_replans_immediately(self, tmp_state, monkeypatch):
        smod, s = self._sched(monkeypatch)
        s.next_full_update = time.time() + 7 * 86400   # old weekly slot
        monkeypatch.setattr(smod, "_spec", lambda: "daily:03:30")
        monkeypatch.setattr(smod, "_next_full_from", lambda now: now + 3600)
        s._tick()
        assert s.next_full_update - time.time() <= 3601

    def test_concurrent_schedule_writes(self, tmp_state):
        from app import scheduler as smod
        errors = []

        def writer():
            for _ in range(100):
                try:
                    smod._save_next(time.time(), time.time())
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
        ts = [threading.Thread(target=writer) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert errors == []
        assert smod._load_next()["next_full_update"] > 0

    def test_loop_survives_tick_exception(self, tmp_state, monkeypatch):
        smod, s = self._sched(monkeypatch)
        calls = []

        def bad_tick():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            s._stop.set()
        monkeypatch.setattr(s, "_tick", bad_tick)
        monkeypatch.setattr(s._stop, "wait", lambda t: s._stop.is_set())
        s._loop()
        assert len(calls) == 2


class TestRunLogger:
    def test_finish_can_be_retried_after_write_error(self, tmp_state, monkeypatch):
        from app import history
        rl = history.RunLogger("full", "manual")
        real = history._write_status
        monkeypatch.setattr(history, "_write_status",
                            lambda st: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            rl.finish(True)
        monkeypatch.setattr(history, "_write_status", real)
        rl.finish(False)
        assert history.current_status()["state"] == "failed"

    def test_prune_stats_respect_days(self, tmp_state):
        from app import history
        history.record_prune_stats(100, 1)
        assert history.prune_stats(30)["reclaimed_mb_30d"] == 100
        assert history.prune_stats(0)["reclaimed_mb_30d"] == 0
