"""Security regressions found in review: key exfiltration, host/CSRF
guards, token generation, strict image refs, rollback source of truth."""
import pytest


@pytest.fixture()
def client(tmp_state, fake_configured, monkeypatch):
    from app import main
    main.app.config["TESTING"] = True
    with main.app.test_client() as c:
        yield c


class _Spy:
    """Records which URL/key a Portainer client was built with."""
    seen = []

    def __init__(self, url, key, **kw):
        _Spy.seen.append((url, key))

    def endpoints(self): return []
    def status(self): return {"Version": "2"}
    def resolve_endpoint(self, h): return 3


class TestKeyNeverLeaves:
    def test_get_endpoints_is_gone(self, client):
        assert client.get("/api/endpoints?url=http://evil").status_code in (404, 405)
        assert client.get("/api/test").status_code in (404, 405)

    @pytest.mark.parametrize("route", ["/api/endpoints", "/api/test"])
    def test_foreign_url_needs_own_key(self, client, monkeypatch, route):
        from app import main
        _Spy.seen = []
        monkeypatch.setattr(main, "Portainer", _Spy)
        r = client.post(route, json={"portainer_url": "http://evil.example"})
        assert r.status_code == 400
        assert _Spy.seen == []          # stored key was never handed out
        r = client.post(route, json={"portainer_url": "http://evil.example",
                                     "portainer_api_key": "ptr_other"})
        assert r.status_code == 200
        assert _Spy.seen == [("http://evil.example", "ptr_other")]

    def test_stored_url_uses_stored_key(self, client, monkeypatch):
        from app import main
        _Spy.seen = []
        monkeypatch.setattr(main, "Portainer", _Spy)
        assert client.post("/api/test", json={}).status_code == 200
        assert _Spy.seen == [("http://dummy.local:9000", "ptr_dummy")]

    def test_url_change_requires_key(self, client):
        r = client.post("/api/settings", json={"portainer_url": "http://evil.example"})
        assert r.status_code == 400


class TestHostAndOrigin:
    def test_rebinding_host_refused_without_token(self, client):
        r = client.get("/api/status", headers={"Host": "evil.example:8090"})
        assert r.status_code == 403
        assert client.get("/api/status", headers={"Host": "127.0.0.1:8090"}).status_code == 200

    def test_cross_origin_post_refused(self, client):
        r = client.post("/api/check", headers={"Origin": "http://evil.example"})
        assert r.status_code == 403

    def test_non_ascii_auth_header_is_401_not_500(self, client, monkeypatch):
        monkeypatch.setenv("QK_AUTH_TOKEN", "secret123")
        monkeypatch.setattr("app.config._cfg", None)
        r = client.get("/api/status", headers={"Authorization": "Bearer ä"})
        assert r.status_code == 401


class TestTokenGeneration:
    def test_generated_when_listening_beyond_localhost(self, tmp_state, monkeypatch, capsys):
        from app import main, config
        main._ensure_auth_token("0.0.0.0")
        tok = config.get("auth_token")
        assert tok and len(tok) >= 24
        assert tok in capsys.readouterr().out
        assert config.user_vars(config.CONFIG_FILE)["auth_token"] == tok

    def test_not_generated_on_loopback(self, tmp_state):
        from app import main, config
        main._ensure_auth_token("127.0.0.1")
        assert not config.get("auth_token")

    def test_token_cannot_be_cleared_when_exposed(self, client, monkeypatch):
        monkeypatch.setenv("QK_LISTEN_HOST", "0.0.0.0")
        monkeypatch.setenv("QK_AUTH_TOKEN", "secret123")
        monkeypatch.setattr("app.config._cfg", None)
        r = client.post("/api/settings", json={"auth_token": ""},
                        headers={"Authorization": "Bearer secret123"})
        assert r.status_code == 400


class TestConfigFile:
    def test_env_values_never_written_to_config_yaml(self, tmp_state, monkeypatch):
        from app import config
        monkeypatch.setenv("QK_PORTAINER_API_KEY", "ptr_from_env")
        config.load(force=True)
        config.set("keep_backups", 7)
        config.save()
        raw = config.user_vars(config.CONFIG_FILE)
        assert raw == {"keep_backups": 7}


class TestImageRefs:
    @pytest.mark.parametrize("ref", ["nginx", "nginx:1.27.3", "ghcr.io/immich-app/immich-server:v1.121.0",
                                     "registry.lan:5000/team/app:2024-01", "redis@sha256:" + "a" * 64,
                                     "lscr.io/linuxserver/sonarr:latest"])
    def test_valid(self, ref):
        from app.compose import valid_image_ref
        assert valid_image_ref(ref)

    @pytest.mark.parametrize("ref", ["", "@sha256:x", ":latest", "nginx:${TAG}", "nginx latest",
                                     "nginx:1\n  privileged: true", "Nginx:1", "nginx#x"])
    def test_invalid(self, ref):
        from app.compose import valid_image_ref
        assert not valid_image_ref(ref)


class TestTls:
    def test_verify_without_ca_file_means_system_cas(self):
        from app.portainer import Portainer
        assert Portainer("https://p", "k", tls_verify=True)._verify is True
        assert Portainer("https://p", "k", tls_verify=True, tls_ca_file="/ca.pem")._verify == "/ca.pem"
        assert Portainer("https://p", "k", tls_verify=False)._verify is False
