"""Cleanup tab: what counts as unused/safe, and removing exactly what was picked."""
import pytest

from app.portainer import PortainerError

MB = 1024 * 1024


class FakeDocker:
    def __init__(self):
        self.imgs = [
            {"Id": "sha256:used", "RepoTags": ["nginx:1.27"], "Size": 50 * MB},
            {"Id": "sha256:old", "RepoTags": ["ghcr.io/x/app:1.0"], "Size": 300 * MB},
            {"Id": "sha256:rb", "RepoTags": ["redis:7.2"], "Size": 40 * MB},
            {"Id": "sha256:dang", "RepoTags": ["<none>:<none>"], "Size": 10 * MB},
            {"Id": "sha256:busy", "RepoTags": ["busy:1"], "Size": 5 * MB},
        ]
        self.cs = [
            {"Id": "c1", "Names": ["/web"], "Image": "nginx:1.27", "ImageID": "sha256:used",
             "State": "running", "Labels": {"com.docker.compose.project": "web"},
             "NetworkSettings": {"Networks": {"web_default": {"NetworkID": "n-web"}}},
             "Mounts": [{"Type": "volume", "Name": "web_data"}]},
            {"Id": "c2", "Names": ["/jelly"], "Image": "jellyfin", "ImageID": "sha256:jf",
             "State": "exited", "Status": "Exited (0) 3 days ago",
             "Labels": {"com.docker.compose.project": "media"},
             "NetworkSettings": {"Networks": {"media_default": {"NetworkID": "n-media"}}}},
        ]
        self.nets = [
            {"Id": "n-bridge", "Name": "bridge", "Driver": "bridge"},
            {"Id": "n-web", "Name": "web_default", "Driver": "bridge"},
            {"Id": "n-media", "Name": "media_default", "Driver": "bridge"},
            {"Id": "n-orphan", "Name": "gone_default", "Driver": "bridge",
             "Labels": {"com.docker.compose.project": "gone"}},
            {"Id": "n-paused", "Name": "paused_default", "Driver": "bridge",
             "Labels": {"com.docker.compose.project": "paused"}},
            {"Id": "n-manual", "Name": "mynet", "Driver": "bridge"},
        ]
        self.removed = []

    def images(self, eid=None): return self.imgs
    def all_containers(self, eid=None): return self.cs
    def networks(self, eid=None): return self.nets
    # 'paused' is a Portainer stack without containers right now
    def stacks(self): return [{"Name": "web", "EndpointId": 3}, {"Name": "paused", "EndpointId": 3}]
    def volumes(self, eid=None): return [{"Name": "web_data"}, {"Name": "old_data", "Driver": "local"}]
    def build_cache(self, eid=None): return [{"Size": 20 * MB, "InUse": False}, {"Size": 5 * MB, "InUse": True}]

    def remove_image(self, iid, eid=None):
        if iid in ("sha256:busy", "busy:1"):
            raise PortainerError("HTTP 409: image has dependent child images")
        self.removed.append(iid)

    def remove_network(self, nid, eid=None): self.removed.append(nid)
    def prune_build_cache(self, eid=None): return {"SpaceReclaimed": 20 * MB}


@pytest.fixture()
def rollback_snapshot(tmp_state):
    from app import snapshots
    snapshots.snapshot_stack("cache", 3, {"redis": "docker.io/library/redis:7.2"})


class TestAnalyze:
    def test_images(self, tmp_state, rollback_snapshot):
        from app.cleanup import analyze
        a = analyze(FakeDocker(), 3)
        by = {i["id"]: i for i in a["images"]}
        assert "sha256:used" not in by
        assert by["sha256:old"]["safe"] is True
        assert by["sha256:rb"]["safe"] is False          # rollback target
        assert by["sha256:rb"]["rollback_for"] == ["cache"]
        assert by["sha256:dang"]["dangling"] is True
        assert a["images"][0]["id"] == "sha256:old"       # largest first

    def test_networks(self, tmp_state):
        from app.cleanup import analyze
        a = analyze(FakeDocker(), 3)
        by = {n["name"]: n for n in a["networks"]}
        assert set(by) == {"gone_default", "paused_default", "mynet"}
        assert by["gone_default"]["safe"] is True         # stack no longer exists
        assert by["paused_default"]["safe"] is False      # Portainer stack exists
        assert by["mynet"]["safe"] is True

    def test_shown_only(self, tmp_state):
        from app.cleanup import analyze
        a = analyze(FakeDocker(), 3)
        assert [c["name"] for c in a["stopped_containers"]] == ["jelly"]
        assert [v["name"] for v in a["volumes"]] == ["old_data"]
        assert a["build_cache_mb"] == 20.0


class TestRunCleanup:
    def test_removes_only_picked_and_still_unused(self, tmp_state):
        from app.cleanup import run_cleanup
        d, logs = FakeDocker(), []
        ok = run_cleanup(d, 3, ["sha256:old", "sha256:used"], ["n-orphan", "n-web"],
                         True, logs.append)
        assert ok is True
        assert d.removed == ["ghcr.io/x/app:1.0", "n-orphan"]   # in-use ids skipped
        assert any("skipped" in l for l in logs)

    def test_docker_refusal_is_reported_not_forced(self, tmp_state):
        from app.cleanup import run_cleanup
        d, logs = FakeDocker(), []
        assert run_cleanup(d, 3, ["sha256:busy"], [], False, logs.append) is False
        assert d.removed == []
        assert any("[WARN]" in l for l in logs)


class TestApi:
    @pytest.fixture()
    def client(self, tmp_state, fake_configured, monkeypatch):
        from app import main
        self.docker = FakeDocker()
        monkeypatch.setattr(main, "_client", lambda: _Wrap(self.docker))
        main.app.config["TESTING"] = True
        with main.app.test_client() as c:
            yield c

    def test_get(self, client):
        r = client.get("/api/cleanup")
        assert r.status_code == 200
        assert {"images", "networks", "stopped_containers"} <= set(r.get_json())

    def test_post_validates(self, client):
        assert client.post("/api/cleanup", json={}).status_code == 400
        assert client.post("/api/cleanup", json={"images": "x"}).status_code == 400

    def test_post_runs_and_records_history(self, client):
        from app import history
        r = client.post("/api/cleanup", json={"images": ["sha256:old"]})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        assert self.docker.removed == ["ghcr.io/x/app:1.0"]
        assert history.history()[-1]["kind"] == "cleanup"

    def test_post_busy(self, client):
        from app.jobs import gate
        assert gate.try_acquire("update")
        try:
            r = client.post("/api/cleanup", json={"images": ["sha256:old"]})
            assert r.status_code == 409
        finally:
            gate.release()


class _Wrap:
    """FakeDocker with an endpoint_id, like a resolved Portainer client."""
    endpoint_id = 3

    def __init__(self, d): self._d = d
    def __getattr__(self, name): return getattr(self._d, name)


class TestReviewRules:
    """Network/Docker review: Swarm, macvlan, external networks, multi-tag
    images, digest-pinned rollback images, the automatic prune."""

    def _docker(self):
        d = FakeDocker()
        d.nets += [
            {"Id": "n-ingress", "Name": "ingress", "Driver": "overlay", "Ingress": True, "Scope": "swarm"},
            {"Id": "n-gw", "Name": "docker_gwbridge", "Driver": "bridge"},
            {"Id": "n-ovl", "Name": "app_overlay", "Driver": "overlay", "Scope": "swarm"},
            {"Id": "n-mac", "Name": "lan", "Driver": "macvlan"},
            {"Id": "n-proxy", "Name": "proxy", "Driver": "bridge"},
        ]
        d.stacks = lambda: [{"Id": 1, "Name": "web", "EndpointId": 3},
                            {"Id": 2, "Name": "paused", "EndpointId": 3}]
        d.stack_file = lambda sid, eid=None: (
            "services:\n  a:\n    image: x\nnetworks:\n  proxy:\n    external: true\n"
            if sid == 1 else "services: {}\n")
        return d

    def test_swarm_networks_never_listed(self, tmp_state):
        from app.cleanup import analyze
        names = {n["name"] for n in analyze(self._docker(), 3)["networks"]}
        assert not names & {"ingress", "docker_gwbridge", "app_overlay"}

    def test_macvlan_and_external_are_not_safe(self, tmp_state):
        from app.cleanup import analyze
        by = {n["name"]: n for n in analyze(self._docker(), 3)["networks"]}
        assert by["lan"]["safe"] is False and "macvlan" in by["lan"]["note"]
        assert by["proxy"]["safe"] is False and "external" in by["proxy"]["note"]
        assert by["mynet"]["safe"] is True

    def test_multi_tag_image_removed_tag_by_tag(self, tmp_state):
        from app.cleanup import run_cleanup
        d = FakeDocker()
        d.imgs.append({"Id": "sha256:multi", "RepoTags": ["a/b:1", "a/b:latest"], "Size": MB})
        assert run_cleanup(d, 3, ["sha256:multi"], [], False, lambda m: None)
        assert d.removed == ["a/b:1", "a/b:latest"]

    def test_digest_pinned_snapshot_protects_dangling_image(self, tmp_state):
        from app import snapshots
        from app.cleanup import analyze
        snapshots.snapshot_stack("web", 3, {"web": "nginx:latest@sha256:" + "b" * 64})
        d = FakeDocker()
        d.imgs.append({"Id": "sha256:prev", "RepoTags": [], "Size": MB,
                       "RepoDigests": ["nginx@sha256:" + "b" * 64]})
        by = {i["id"]: i for i in analyze(d, 3)["images"]}
        assert by["sha256:prev"]["safe"] is False

    def test_run_prune_keeps_rollback_images(self, tmp_state, rollback_snapshot):
        from app.updater import cleanup_docker
        d = FakeDocker()
        d.prune_images = lambda eid=None: pytest.fail("must not prune everything")
        assert cleanup_docker(d, 3, lambda m: None) in (True, False)
        assert "redis:7.2" not in d.removed            # rollback target kept
        assert "ghcr.io/x/app:1.0" in d.removed
