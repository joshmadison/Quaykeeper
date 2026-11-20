"""Docker/Portainer review fixes: update detection shape rules, git
redeploy payload, soft deploy errors, own-container detection, repair
rule idempotency."""
import pytest

from app import dockerhub
from app.portainer import PortainerError

D_OLD, D_NEW = "sha256:" + "1" * 64, "sha256:" + "2" * 64


def _check(monkeypatch, tag, tags, local=D_OLD, remote=D_OLD):
    monkeypatch.setattr(dockerhub, "remote_digest", lambda *a: remote)
    monkeypatch.setattr(dockerhub, "newest_semver_tags", lambda *a: tags)
    return dockerhub._check_update_uncached("docker.io", "library/x", tag, local)


class TestUpdateDetection:
    def test_variant_and_precision_are_kept(self, monkeypatch):
        r = _check(monkeypatch, "1.27-alpine", ["1.29-bookworm", "1.28-alpine", "1.29.1"])
        assert r["update_available"] and r["latest_tag"] == "1.28-alpine"

    def test_major_jump_is_only_mentioned(self, monkeypatch):
        r = _check(monkeypatch, "16", ["16", "17", "18"])
        assert r["update_available"] is False
        assert "18" in r["note"]

    def test_floating_tag_rebuild_detected_by_digest(self, monkeypatch):
        r = _check(monkeypatch, "16", ["16", "16.4"], local=D_OLD, remote=D_NEW)
        assert r["update_available"] is True and r["latest_tag"] == "16"

    def test_current_stays_current(self, monkeypatch):
        r = _check(monkeypatch, "1.2.3", ["1.2.3", "1.2.2"])
        assert r["update_available"] is False


class _Req:
    def __init__(self): self.calls = []
    def __call__(self, method, path, **kw): self.calls.append((method, path, kw))


class TestGitRedeploy:
    def test_payload_carries_ref_and_auth(self):
        from app.portainer import Portainer
        p = Portainer("http://p", "k")
        p._req = _Req()
        p.redeploy_git_stack(7, 3, [], git_config={
            "ReferenceName": "refs/heads/prod",
            "Authentication": {"Username": "bot", "Password": ""}})
        body = p._req.calls[0][2]["json"]
        assert body["RepositoryReferenceName"] == "refs/heads/prod"
        assert body["RepositoryAuthentication"] is True
        assert body["RepositoryUsername"] == "bot"
        assert body["PullImage"] is True

    def test_build_stacks_do_not_pull(self):
        from app.updater import _has_build
        assert _has_build("services:\n  a:\n    build: .\n")
        assert not _has_build("services:\n  a:\n    image: x\n")


class TestDeploy:
    @pytest.mark.parametrize("msg,soft", [
        ("PUT /api/stacks/1 failed: Read timed out. (read timeout=300)", True),
        ("PUT /api/stacks/1 -> HTTP 504: gateway timeout", True),
        ("PUT /api/stacks/1 -> HTTP 400: invalid compose", False)])
    def test_soft_errors(self, msg, soft):
        from app.updater import _soft_deploy_error
        assert _soft_deploy_error(PortainerError(msg)) is soft

    def test_nothing_recreated_is_accepted(self, fake_client, tmp_state, monkeypatch):
        from app import updater
        stack = {"Id": 5, "Name": "same", "EndpointId": 3, "Env": [], "GitConfig": None}
        c = fake_client(stacks=[stack], stack_files={5: "services:\n  a:\n    image: x:1\n"})
        calls = []

        def ready(client, project, eid, log=None, created_after=None):
            calls.append(created_after)
            return (0, 0) if created_after is not None else (1, 1)
        monkeypatch.setattr(updater, "_containers_ready", ready)
        t = [1000.0]
        monkeypatch.setattr(updater.time, "time", lambda: t[0])
        monkeypatch.setattr(updater.time, "sleep", lambda s: t.__setitem__(0, t[0] + s))
        logs = []
        assert updater.redeploy_single_stack(c, stack, 3, logs.append) is True
        assert any("nothing to recreate" in l for l in logs)


class TestSelfDetection:
    def test_own_container_from_mountinfo(self, monkeypatch, tmp_path):
        from app import updater
        cid = "a" * 64
        mi = tmp_path / "mountinfo"
        mi.write_text(f"1 2 0:1 /var/lib/docker/containers/{cid}/hostname /etc/hostname rw - ext4 x rw\n")
        real_open = open
        monkeypatch.setattr("builtins.open", lambda p, *a, **k:
                            real_open(mi, *a, **k) if p == "/proc/self/mountinfo" else real_open(p, *a, **k))
        assert updater._own_container_id() == cid

    def test_self_stack_found_by_id_with_named_volume(self, fake_client, monkeypatch):
        from app import updater
        monkeypatch.setattr(updater, "_own_container_id", lambda: "b" * 64)
        c = fake_client(containers=[
            {"Id": "b" * 64, "Names": ["/quaykeeper"],
             "Labels": {"com.docker.compose.project": "portainer_update_service"},
             "Mounts": [{"Type": "volume", "Destination": "/app/data"}]},
            {"Id": "c" * 64, "Names": ["/other"], "Labels": {"com.docker.compose.project": "immich"}}])
        assert updater._self_stack_names(c, 3) == ["portainer_update_service"]


class TestRepairRule:
    def test_connect_skipped_when_already_attached(self, fake_client):
        from app.updater import _run_repair_rule
        c = fake_client(containers=[{"Id": "x", "Names": ["/apache"], "State": "running",
                                     "NetworkSettings": {"Networks": {"shared": {"NetworkID": "n1"}}}}])
        c.networks = lambda eid=None: [{"Id": "n1", "Name": "shared"}]
        c.connect_network = lambda *a: pytest.fail("must not connect twice")
        logs = []
        rule = {"name": "r", "fix": {"connect_network": "shared", "connect_container": "apache"}}
        assert _run_repair_rule(rule, c, 3, logs.append) is True
        assert any("already on shared" in l for l in logs)
