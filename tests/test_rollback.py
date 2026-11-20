"""snapshots module + updater.rollback_stack - the rollback feature core."""
import json
import time

import pytest


class TestSnapshots:
    def test_snapshot_and_get(self, tmp_state):
        from app import snapshots as s
        s.snapshot_stack("immich", 3, {"server": "img:v1.104", "ml": "img:v1.104"})
        snap = s.get_snapshot("immich")
        assert snap["eid"] == 3
        assert snap["images"]["server"] == "img:v1.104"
        assert snap["ts"] > 0

    def test_overwrite_is_last_state_before_update(self, tmp_state):
        from app import snapshots as s
        s.snapshot_stack("immich", 3, {"server": "img:v1.103"})
        s.snapshot_stack("immich", 3, {"server": "img:v1.104"})
        # the snapshot ALWAYS holds the state right before the newest update
        assert s.get_snapshot("immich")["images"]["server"] == "img:v1.104"

    def test_unknown_stack_none(self, tmp_state):
        from app import snapshots as s
        assert s.get_snapshot("nope") is None
        assert s.last_snapshot_time("nope") is None

    def test_diff_shows_changed_services_only(self, tmp_state):
        from app import snapshots as s
        s.snapshot_stack("st", 3, {"a": "img:1", "b": "img:2", "c": "img:3"})
        current = {"a": "img:9", "b": "img:2", "c": None}
        rows = s.diff_snapshot("st", current)
        # a changed; b same -> skipped; c missing in current -> skipped
        assert rows == [{"service": "a", "current": "img:9", "restore": "img:1"}]

    def test_diff_no_changes_empty(self, tmp_state):
        from app import snapshots as s
        s.snapshot_stack("st", 3, {"a": "img:1"})
        assert s.diff_snapshot("st", {"a": "img:1"}) == []

    def test_persistence_survives_module_reload(self, tmp_state):
        from app import snapshots as s
        s.snapshot_stack("persistent", 3, {"x": "img:1"})
        # simulate re-read from disk
        raw = json.loads((s.SNAPSHOTS_FILE).read_text(encoding="utf-8"))
        assert raw["persistent"]["images"]["x"] == "img:1"

    def test_keep_stacks_cap(self, tmp_state, monkeypatch):
        from app import snapshots as s
        monkeypatch.setattr(s, "KEEP_STACKS", 5)
        for i in range(10):
            s.snapshot_stack(f"stack{i}", 3, {"a": f"img:{i}"}, run_id=f"r{i}")
        raw = json.loads(s.SNAPSHOTS_FILE.read_text(encoding="utf-8"))
        assert len(raw) == 5
        # newest kept, oldest dropped (ts ordering)
        assert "stack9" in raw and "stack0" not in raw


class TestResolvedImages:
    def test_env_substitution_matches_compose(self, tmp_state):
        from app.updater import _resolved_images
        compose = "services:\n  a:\n    image: ${REG}/app:${TAG}\n"
        env = [{"name": "REG", "value": "ghcr.io/acme"},
               {"name": "TAG", "value": "v2.0"}]
        assert _resolved_images(compose, env) == {"a": "ghcr.io/acme/app:v2.0"}

    def test_plain_images(self, tmp_state):
        from app.updater import _resolved_images
        compose = "services:\n  a:\n    image: nginx:1.0\n  b:\n    build: .\n"
        assert _resolved_images(compose, []) == {"a": "nginx:1.0"}


class TestRollbackStack:
    def _patch_client(self, monkeypatch, c):
        """rollback_stack constructs its own Portainer client - inject the fake."""
        import socket as _socket
        import app.updater as upd
        monkeypatch.setattr(_socket, "gethostname", lambda: "host")
        monkeypatch.setattr(upd, "Portainer", lambda *a, **kw: c)
        monkeypatch.setattr(c, "resolve_endpoint", lambda h: 3, raising=False)

    def _stack(self, name="my_stack"):
        return {"Id": 94, "Name": name, "EndpointId": 3, "Env": [],
                "GitConfig": None}

    def test_happy_path_restores_images(self, fake_client, tmp_state, fake_configured, monkeypatch):
        from app import snapshots as s
        from app.updater import rollback_stack
        s.snapshot_stack("my_stack", 3, {"a": "nginx:1.0"})
        compose = "services:\n  a:\n    image: nginx:2.0\n"
        c = fake_client(stacks=[self._stack()], stack_files={94: compose})
        monkeypatch.setattr(
            "app.updater._containers_ready",
            lambda client, project, eid, log=None, created_after=None: (1, 1))
        from app.history import RunLogger
        rl = RunLogger("rollback", "test")
        # Patch socket to avoid hostname resolution weirdness
        self._patch_client(monkeypatch, c)
        ok = rollback_stack(rl, "my_stack", {"a": "nginx:1.0"})
        assert ok is True
        assert "image: nginx:1.0" in c.stack_files[94]
        assert "image: nginx:2.0" not in c.stack_files[94]

    def test_nothing_to_change_returns_true(self, fake_client, tmp_state, fake_configured, monkeypatch):
        from app.updater import rollback_stack
        compose = "services:\n  a:\n    image: nginx:1.0\n"
        c = fake_client(stacks=[self._stack()], stack_files={94: compose})
        self._patch_client(monkeypatch, c)
        from app.history import RunLogger
        rl = RunLogger("rollback", "test")
        ok = rollback_stack(rl, "my_stack", {"a": "nginx:1.0"})
        assert ok is True
        # no edit was made: the stored compose is byte-identical to input
        # (FakePortainer prefills stack_files with the constructor compose)
        assert c.stack_files[94] == compose

    def test_stopped_stack_edits_but_skips_deploy(self, fake_client, tmp_state, fake_configured, monkeypatch):
        from app.updater import rollback_stack
        compose = "services:\n  a:\n    image: nginx:2.0\n"
        c = fake_client(stacks=[self._stack()], stack_files={94: compose},
                        containers=[{"Id": "c1", "State": "exited", "Names": ["/a"],
                                     "Labels": {"com.docker.compose.project": "my_stack"}}])
        self._patch_client(monkeypatch, c)
        from app.history import RunLogger
        rl = RunLogger("rollback", "test")
        ok = rollback_stack(rl, "my_stack", {"a": "nginx:1.0"})
        assert ok is True
        # compose edited, but no deploy call happened
        assert "image: nginx:1.0" in c.stack_files[94]

    def test_git_stack_rejected(self, fake_client, tmp_state, fake_configured, monkeypatch):
        from app.updater import rollback_stack
        stack = self._stack()
        stack["GitConfig"] = {"URL": "https://github.com/x/y"}
        compose = "services:\n  a:\n    image: nginx:2.0\n"
        c = fake_client(stacks=[stack], stack_files={94: compose})
        self._patch_client(monkeypatch, c)
        from app.history import RunLogger
        rl = RunLogger("rollback", "test")
        ok = rollback_stack(rl, "my_stack", {"a": "nginx:1.0"})
        assert ok is False  # silently rewriting git stacks would be a lie

    def test_unknown_stack_false(self, fake_client, tmp_state, fake_configured, monkeypatch):
        from app.updater import rollback_stack
        c = fake_client(stacks=[self._stack()])
        self._patch_client(monkeypatch, c)
        from app.history import RunLogger
        rl = RunLogger("rollback", "test")
        assert rollback_stack(rl, "does_not_exist", {"a": "nginx:1.0"}) is False


class TestSnapshotsInRedeploy:
    """The rollback point is the state BEFORE an update that really changed
    an image, pinned to the digest that ran - not a copy of the compose
    file taken on every run."""
    COMPOSE = "services:\n  a:\n    image: nginx:1.0\n"

    def _setup(self, fake_client, monkeypatch, name, change):
        stack = {"Id": 5, "Name": name, "EndpointId": 3, "Env": [], "GitConfig": None}
        ctr = {"Id": "c1", "State": "running", "ImageID": "sha256:old",
               "Labels": {"com.docker.compose.project": name,
                          "com.docker.compose.service": "a"}}
        c = fake_client(containers=[ctr], stacks=[stack], stack_files={5: self.COMPOSE})
        c.image_inspect = lambda iid, eid=None: {
            "RepoDigests": ["nginx@sha256:" + "a" * 64] if iid == "sha256:old" else []}

        def update(sid, eid, content, env):
            if change:
                ctr["ImageID"] = "sha256:new"
        c.update_stack = update
        monkeypatch.setattr(
            "app.updater._containers_ready",
            lambda client, project, eid, log=None, created_after=None: (1, 1))
        return c, stack

    def test_snapshot_saved_when_image_changed(self, fake_client, tmp_state, monkeypatch):
        from app import snapshots as s
        from app.updater import redeploy_single_stack
        c, stack = self._setup(fake_client, monkeypatch, "snap_stack", change=True)
        logs = []
        assert redeploy_single_stack(c, stack, 3, logs.append) is True
        assert s.get_snapshot("snap_stack")["images"]["a"] == "nginx:1.0@sha256:" + "a" * 64
        assert any("rollback point saved" in l for l in logs)

    def test_unchanged_run_keeps_previous_snapshot(self, fake_client, tmp_state, monkeypatch):
        from app import snapshots as s
        from app.updater import redeploy_single_stack
        s.snapshot_stack("keep", 3, {"a": "nginx:0.9"})
        c, stack = self._setup(fake_client, monkeypatch, "keep", change=False)
        assert redeploy_single_stack(c, stack, 3, lambda m: None) is True
        assert s.get_snapshot("keep")["images"]["a"] == "nginx:0.9"

    def test_snapshot_failure_never_kills_run(self, fake_client, tmp_state, monkeypatch):
        from app import snapshots as s
        from app.updater import redeploy_single_stack
        c, stack = self._setup(fake_client, monkeypatch, "snapfail", change=True)
        monkeypatch.setattr(s, "snapshot_stack",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        logs = []
        assert redeploy_single_stack(c, stack, 3, logs.append) is True
        assert any("rollback point not saved" in l for l in logs)


class TestRollbackApi:
    @pytest.fixture()
    def client(self, tmp_state, monkeypatch):
        from app.main import app
        app.config["TESTING"] = True
        monkeypatch.setenv("QK_CONFIG_DIR", str(tmp_state.parent / "cfg"))
        with app.test_client() as c:
            yield c

    def test_rollback_requires_images_map(self, client):
        r = client.post("/api/stacks/1/rollback", json={})
        assert r.status_code == 400

    def test_rollback_validates_entries(self, client):
        r = client.post("/api/stacks/1/rollback",
                        json={"images": {"svc; drop": "img"}})
        assert r.status_code == 400
        r = client.post("/api/stacks/1/rollback",
                        json={"images": {"svc": "a b c"}})
        assert r.status_code == 400

    def test_rollback_unconfigured_still_gated_shape(self, client):
        # no portainer configured -> _client_or_error errors before gate
        from app import config as cfg
        cfg.load(force=True)
        r = client.post("/api/stacks/1/rollback",
                        json={"images": {"svc": "nginx:1.0"}})
        assert r.status_code in (400, 409, 502)