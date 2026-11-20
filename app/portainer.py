"""Portainer API client (HTTP only - no docker CLI required).

The Docker API is reached through Portainer's docker proxy
(/api/endpoints/<id>/docker/...) so the app works even without
local docker CLI access.
"""
import json
from urllib.parse import quote

import requests
import urllib3

# Self-signed Portainer certs are explicitly supported (tls_verify=false) -
# suppress the per-request warning noise in the logs.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class PortainerError(RuntimeError):
    pass


class Portainer:
    def __init__(self, base_url, api_key, endpoint_id=None, tls_verify=False,
                 tls_ca_file=None):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = api_key
        # tls_verify=false -> accept self-signed certs.
        # tls_verify=true + tls_ca_file set -> verify against that CA (the
        # way to make verification work with a self-signed Portainer cert:
        # mount the cert and point tls_ca_file at it).
        # verify=None (tls_verify on, no CA file) used to mean NO verification
        # in requests; True = system CA bundle, a path = that CA.
        self._verify = (tls_ca_file or True) if tls_verify else False
        self.session.verify = self._verify
        self.endpoint_id = endpoint_id

    # ------------------------------------------------------------------ HTTP
    def _req(self, method, path, **kw):
        timeout = kw.pop("timeout", 30)
        try:
            # verify passed per request: REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE in
            # the environment would otherwise override the session setting
            r = self.session.request(method, self.base + path, timeout=timeout,
                                     verify=self._verify, **kw)
        except requests.RequestException as e:
            msg = str(e)
            if isinstance(e, requests.exceptions.SSLError) or "SSLError" in msg \
                    or "CERTIFICATE_VERIFY_FAILED" in msg:
                msg = (f"{method} {path} failed: TLS certificate verification "
                       f"failed ({msg[:200]}). This happens when Portainer serves "
                       f"a self-signed certificate. Either disable 'Verify TLS "
                       f"certificate' in Settings, or mount Portainer's cert and "
                       f"set tls_ca_file in config/config.yaml.")
            raise PortainerError(msg) from e
        if r.status_code >= 400:
            raise PortainerError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r

    # ---------------------------------------------------------------- system
    def status(self):
        return self._req("GET", "/api/status").json()

    def endpoints(self):
        return self._req("GET", "/api/endpoints").json()

    def resolve_endpoint(self, hostname: str) -> int:
        """Same strategy as the bash script: exact match -> partial -> single local."""
        if self.endpoint_id is not None:
            return self.endpoint_id
        eps = self.endpoints()
        h = (hostname or "").lower()
        for e in eps:
            if (e.get("Name") or "").lower() == h:
                self.endpoint_id = e["Id"]
                return self.endpoint_id
        for e in eps:
            if h and h in (e.get("Name") or "").lower():
                self.endpoint_id = e["Id"]
                return self.endpoint_id
        local = [e for e in eps if e.get("Type") == 1]
        if len(local) == 1:
            self.endpoint_id = local[0]["Id"]
            return self.endpoint_id
        listing = "; ".join(f"ID {e.get('Id')} '{e.get('Name')}' type={e.get('Type')}" for e in eps)
        raise PortainerError(f"Could not resolve endpoint for host '{hostname}'. Set portainer_endpoint_id. Endpoints: {listing}")

    # ---------------------------------------------------------------- stacks
    def stacks(self):
        return self._req("GET", "/api/stacks").json()

    def stack_file(self, stack_id, eid):
        r = self._req("GET", f"/api/stacks/{stack_id}/file?endpointId={eid}")
        return r.json().get("StackFileContent") or ""

    def update_stack(self, stack_id, eid, compose_content, env, prune=True, pull=True):
        payload = {
            "StackFileContent": compose_content,
            "Env": env or [],
            "Prune": prune,
            "PullImage": pull,
        }
        self._req("PUT", f"/api/stacks/{stack_id}?endpointId={eid}",
                  json=payload, timeout=300)

    def redeploy_git_stack(self, stack_id, eid, env=None, prune=True, pull=True,
                           git_config=None):
        """Redeploy a stack created from a git repository. The regular
        update_stack() (PUT /stacks/{id} with StackFileContent) DETACHES git
        stacks from their repository and redeploys the stale local clone -
        new commits would never arrive. This endpoint re-clones first.

        git_config: the stack's GitConfig. Its branch/ref is passed on, and
        for private repositories RepositoryAuthentication=true makes
        Portainer reuse the stored credentials (an empty password means
        'keep the saved one').
        pull: False only for stacks with build: services - a registry pull
        on build-only services errors out."""
        git_config = git_config or {}
        payload = {
            "Env": env or [],
            "Prune": prune,
            "PullImage": pull,
            "RepositoryAuthentication": bool(git_config.get("Authentication")),
        }
        if git_config.get("ReferenceName"):
            payload["RepositoryReferenceName"] = git_config["ReferenceName"]
        auth = git_config.get("Authentication") or {}
        if isinstance(auth, dict) and auth.get("Username"):
            payload["RepositoryUsername"] = auth["Username"]
        self._req("PUT", f"/api/stacks/{stack_id}/git/redeploy?endpointId={eid}",
                  json=payload, timeout=600)

    # ---------------------------------------------------------- docker proxy
    def _docker(self, method, path, eid=None, **kw):
        eid = eid or self.endpoint_id
        return self._req(method, f"/api/endpoints/{eid}/docker{path}", **kw)

    def docker_info(self, eid=None):
        return self._docker("GET", "/info", eid).json()

    def containers_by_project(self, project, eid=None, all_=True):
        filters = json.dumps({"label": [f"com.docker.compose.project={project}"]})
        return self._docker(
            "GET", f"/containers/json?all={1 if all_ else 0}&filters={filters}", eid
        ).json()

    def all_containers(self, eid=None):
        return self._docker("GET", "/containers/json?all=1", eid).json()

    def inspect_container(self, cid, eid=None):
        return self._docker("GET", f"/containers/{cid}/json", eid).json()

    def image_inspect(self, image_ref, eid=None):
        ref = quote(image_ref, safe=":@")
        return self._docker("GET", f"/images/{ref}/json", eid).json()

    # ---------------------------------------------------------- prune/backup
    def prune_images(self, eid=None):
        """Portainer admin-only: prune ALL unused images (dangling=false)."""
        filters = quote('{"dangling": ["false"]}', safe="")
        return self._docker("POST", f"/images/prune?filters={filters}", eid).json()

    def prune_build_cache(self, eid=None):
        """Portainer admin-only: prune ALL build cache (all=true)."""
        return self._docker("POST", "/build/prune?all=true", eid).json()

    def images(self, eid=None):
        """Top-level images only (all=0): with all=1 the classic builder's
        intermediate layers were listed too - not removable on their own."""
        return self._docker("GET", "/images/json", eid).json()

    def networks(self, eid=None):
        return self._docker("GET", "/networks", eid).json()

    def remove_image(self, image_id, eid=None):
        """Remove ONE image (no force: an image still used by a container or
        with dependent child images is refused by Docker with HTTP 409)."""
        return self._docker("DELETE", f"/images/{quote(image_id, safe='')}", eid)

    def remove_network(self, network_id, eid=None):
        return self._docker("DELETE", f"/networks/{quote(network_id, safe='')}", eid)

    def volumes(self, eid=None):
        return self._docker("GET", "/volumes", eid).json().get("Volumes") or []

    def build_cache(self, eid=None):
        """Build cache records (Docker API 1.42+: /system/df?type=build-cache)."""
        return self._docker("GET", "/system/df?type=build-cache",
                            eid).json().get("BuildCache") or []

    def network_inspect(self, name_or_id, eid=None):
        return self._docker("GET",
                            f"/networks/{quote(name_or_id, safe='')}",
                            eid).json()

    def connect_network(self, network_id, container_id, eid=None):
        return self._docker("POST", f"/networks/{quote(network_id, safe='')}/connect", eid,
                            json={"Container": container_id})

    def restart_container(self, cid, eid=None, timeout_s=30):
        return self._docker("POST", f"/containers/{cid}/restart?t={timeout_s}", eid)

    def exec_in_container(self, cid, cmd, eid=None, timeout_s=30):
        """Run a command inside a container via the Portainer docker-proxy
        exec flow. Returns (exit_code, stdout_text). Handles the 8-byte
        frame-header multiplexing of the non-TTY stream."""
        create = self._docker("POST", f"/containers/{cid}/exec", eid,
                              json={"AttachStdout": True, "AttachStderr": True,
                                    "Cmd": cmd if isinstance(cmd, list) else [cmd]})
        exec_id = create.json().get("Id")
        if not exec_id:
            raise PortainerError("exec create returned no Id")
        r = self._docker("POST", f"/exec/{exec_id}/start", eid,
                         json={"Detach": False, "Tty": False}, timeout=timeout_s)
        # demultiplex the docker stream (non-TTY): 8-byte frames
        # [stream_type(1), 0,0,0, size(4 BE)]
        stdout = bytearray()
        buf = r.content
        i = 0
        while i + 8 <= len(buf):
            size = int.from_bytes(buf[i + 4:i + 8], "big")
            stdout.extend(buf[i + 8:i + 8 + size])
            i += 8 + size
        try:
            insp = self._docker("GET", f"/exec/{exec_id}/json", eid).json()
            code = insp.get("ExitCode")
        except PortainerError:
            code = None  # unknown - never pretend the command succeeded
        return code, stdout.decode("utf-8", errors="replace").strip()

    def backup(self, password=None):
        """Download a Portainer datastore backup (tar.gz attachment).
        Uses the app's session key as auth (admin required)."""
        payload = {"password": password} if password else {}
        r = self._req("POST", "/api/backup", json=payload, timeout=300)
        return r.content