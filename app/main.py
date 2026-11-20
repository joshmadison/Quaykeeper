"""Flask app: REST API + static frontend for Quaykeeper.

Hardened: shared job gate for all mutating routes, optional Bearer-token auth,
input validation, non-blocking /api/stacks, run-progress endpoints, /healthz.
"""
import re
import socket
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from . import checker, compose as compose_mod, dockerhub, history, updater
from .config import get, load, set as cfg_set, save as cfg_save, ConfigError
from .history import RunLogger
from .jobs import gate
from .portainer import Portainer, PortainerError
from .scheduler import scheduler

app = Flask(__name__, static_folder=None)
UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def _err(msg, code=400):
    return jsonify({"error": msg}), code


# ------------------------------------------------------------------- auth
MUTATING = {"POST", "PUT", "DELETE"}


def _request_host() -> str:
    h = request.host or ""
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    return h.split(":", 1)[0]


@app.before_request
def _auth_gate():
    """Who may call what.

    With auth_token set (always the case when listening beyond localhost -
    main() generates one), EVERY /api/* route needs 'Authorization: Bearer
    <token>', reads included: compose files, run logs and settings carry
    secrets and map the whole infrastructure. Only /healthz and the static
    UI files stay public.

    Without a token Quaykeeper only listens on localhost. Two guards apply:
    - Host must be a loopback name. A malicious page that rebinds its own
      DNS name to 127.0.0.1 sends Host: evil.example and is refused.
    - Mutating requests need a same-origin Origin header (or none, for CLI
      tools) - CSRF protection against cross-site form posts.
    """
    path = request.path
    if not path.startswith("/api/"):
        return None
    token = get("auth_token", "")
    if token:
        import hmac
        supplied = request.headers.get("Authorization", "")
        ok = hmac.compare_digest(supplied.encode("utf-8", "replace"),
                                 f"Bearer {token}".encode("utf-8", "replace"))
        if not ok:
            return _err("unauthorized - set Authorization: Bearer <auth_token>", 401)
        return None
    from .config import is_loopback
    if not is_loopback(_request_host()):
        return _err("this host name is not allowed - set auth_token to reach "
                    "Quaykeeper under other names", 403)
    if request.method in MUTATING:
        origin = request.headers.get("Origin", "")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return _err("cross-site request rejected - enable auth_token to "
                        "call the API from other origins", 403)
    return None


def _client() -> Portainer:
    """Portainer client; raises ConfigError-free PortainerError on failure.
    Routes wanting a friendly 'not configured' message should check
    _configured() first."""
    client = Portainer(get("portainer_url"), get("portainer_api_key"),
                       endpoint_id=get("portainer_endpoint_id"),
                       tls_verify=bool(get("tls_verify", False)),
                       tls_ca_file=(get("tls_ca_file", "") or None))
    client.resolve_endpoint(socket.gethostname())
    return client


def _configured() -> bool:
    return bool(get("portainer_url") and get("portainer_api_key"))


def _client_or_error():
    """Returns (client, None) or (None, error_response)."""
    if not _configured():
        return None, _err("not configured - enter Portainer URL and API key in Settings", 400)
    try:
        return _client(), None
    except PortainerError as e:
        return None, _err(str(e), 502)


def _job(holder: str, kind: str, work, background: bool):
    """Run one gated job with a RunLogger. Whatever happens - an exception
    of any type included - the run is finished (never stuck in 'running')
    and the gate is released. work(runlog) -> bool.

    background=True: start in a worker and answer at once with the run_id
    (long jobs must not block one of the 8 waitress threads)."""
    if not gate.try_acquire(holder):
        return _err("another job is running - see /api/runs/current", 409)
    try:
        runlog = RunLogger(kind, "manual")
        gate.set_runlog(runlog)
    except Exception:
        gate.release()
        raise

    def run() -> bool:
        ok = False
        try:
            ok = bool(work(runlog))
        except Exception:  # noqa: BLE001 - recorded in the run log
            import traceback
            runlog.log("[ERROR] crashed with an unexpected error:\n"
                       + traceback.format_exc())
            ok = False
        finally:
            try:
                runlog.finish(ok)
            finally:
                gate.release()
                _inventory_cache.clear()
        return ok

    if background:
        threading.Thread(target=run, name=f"job-{kind}", daemon=True).start()
        return jsonify({"ok": True, "started": True, "run_id": runlog.run_id})
    ok = run()
    return jsonify({"ok": ok, "run_id": runlog.run_id})


# ------------------------------------------------------------------- static UI
@app.route("/")
def index():
    return send_from_directory(UI_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(UI_DIR, filename)


# ------------------------------------------------------------------- status
@app.route("/api/status")
def api_status():
    snap = checker.status_snapshot()
    snap["scheduler"] = {
        "busy": gate.is_busy(),
        "current": gate.current(),
        "interval_hours": get("update_interval_hours", 168),
        "schedule_mode": get("update_schedule_mode", "interval"),
        "schedule_time": get("update_schedule_time", "03:30"),
        "schedule_day": int(get("update_schedule_day", 0)),
        "schedule_desc": scheduler.schedule_desc(),
        "server_time_local": time.strftime("%a %d.%m. %H:%M"),
        "server_tz": time.strftime("%Z"),
        "next_check": scheduler.next_check,
        "next_full_update": scheduler.next_full_update,
        "consecutive_failures": scheduler.consecutive_failures,
        "last_tick": scheduler.last_tick,
        "now": time.time(),
    }
    snap["configured"] = _configured()
    snap["endpoint_configured"] = get("portainer_endpoint_id") is not None
    snap["auth_enabled"] = bool(get("auth_token", ""))
    return jsonify(snap)


@app.route("/healthz")
def healthz():
    """Liveness + scheduler heartbeat. 503 if the scheduler tick is stale."""
    tick = scheduler.last_tick
    # the scheduler loop ticks every 10s and never runs jobs inline, so a
    # stale tick means the loop thread is dead or wedged - not 'busy'
    stale = tick is None or time.time() - tick > 120
    return jsonify({
        "ok": not stale,
        "last_tick": tick,
        "state": history.current_status().get("state"),
        "consecutive_failures": scheduler.consecutive_failures,
        "busy": scheduler.is_busy(),
    }), (200 if not stale else 503)


def _probe_client():
    """Client for the Settings 'Test connection' / 'Scan' buttons, which may
    run BEFORE anything is saved. The stored API key is only ever sent to
    the stored URL: a request that names another URL must bring its own key
    (otherwise any caller could make Quaykeeper send the admin key to a
    server of their choice)."""
    body = request.get_json(silent=True) or {}
    stored_url = (get("portainer_url") or "").rstrip("/")
    url = (body.get("portainer_url") or "").strip().rstrip("/") or stored_url
    key = (body.get("portainer_api_key") or "").strip()
    if not key or key == "***":
        if url != stored_url:
            raise ValueError("enter the API key to test a different Portainer URL")
        key = get("portainer_api_key") or ""
    if not url or not key:
        raise ValueError("not configured - Portainer URL and API key required")
    if not url.startswith(("http://", "https://")):
        raise ValueError("Portainer URL must start with http:// or https://")
    return Portainer(url, key, tls_verify=bool(get("tls_verify", False)),
                     tls_ca_file=(get("tls_ca_file", "") or None))


@app.route("/api/test", methods=["POST"])
def api_test():
    """Settings 'Test connection': verify API + resolve endpoint.
    Body (optional): {"portainer_url", "portainer_api_key"}."""
    try:
        client = _probe_client()
    except ValueError as e:
        return _err(str(e), 400)
    try:
        st = client.status()
        endpoint_id = client.resolve_endpoint(socket.gethostname())
    except PortainerError as e:
        return _err(str(e), 502)
    return jsonify({"ok": True, "version": st.get("Version") or st.get("version") or "?",
                    "endpoint_id": endpoint_id})


@app.route("/api/endpoints", methods=["POST"])
def api_endpoints():
    """Settings 'Scan': list Portainer endpoints. Same body as /api/test."""
    try:
        client = _probe_client()
    except ValueError as e:
        return _err(str(e), 400)
    try:
        eps = client.endpoints()
    except PortainerError as e:
        return _err(str(e), 502)
    return jsonify({"ok": True, "endpoints": [
        {"id": e.get("Id"), "name": e.get("Name"), "type": e.get("Type"),
         "url": e.get("Snapshots", [{}])[0].get("DockerURL", "") if e.get("Snapshots") else ""}
        for e in eps
    ]})


@app.route("/api/portainer_mount_check")
def api_portainer_mount_check():
    """Settings 'Check compose mount' button: verify Portainer's compose dir
    is mounted into this container (at /host-portainer) and contains a
    docker-compose.yml."""
    from pathlib import Path
    compose_dir = "/host-portainer"
    d = Path(compose_dir)
    if not d.exists():
        return jsonify({"ok": False, "state": "not_mounted",
                        "message": f"'{compose_dir}' does not exist inside this "
                                   "container - the volume mount is missing. "
                                   "Add it to the stack (host path on the left, "
                                   "container path on the right of the ':') and "
                                   "redeploy."})
    yml = d / "docker-compose.yml"
    if not yml.exists():
        return jsonify({"ok": False, "state": "no_compose_file",
                        "message": f"'{compose_dir}' is mounted but contains no "
                                   "docker-compose.yml - check the mount points "
                                   "at Portainer's compose dir."})
    try:
        content = yml.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return jsonify({"ok": False, "state": "unreadable",
                        "message": f"compose file not readable: {e}"})
    image = ""
    import re as _re
    m = _re.search(r"image:\s*([^\s]+)", content)
    if m:
        image = m.group(1)
    return jsonify({"ok": True, "state": "ok", "path": compose_dir,
                    "image": image,
                    "message": f"Compose file found (image: {image})"
                               + " - Portainer self-update will work."})


@app.route("/api/history")
def api_history():
    return jsonify({"history": history.history(),
                    "prune_stats_30d": history.prune_stats()})


@app.route("/api/runs/current")
def api_runs_current():
    """Live progress of the active run (steps + log tail)."""
    cur = gate.current()
    if not cur.get("active"):
        return jsonify({"active": False})
    tail = []
    p = history.run_log_path(cur["run_id"]) if cur.get("run_id") else None
    if p:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-15:]
        except OSError:
            pass
    return jsonify({"active": True, "run_id": cur["run_id"],
                    "holder": cur["holder"], "since": cur["since"],
                    "steps": cur["steps"], "log_tail": tail})


@app.route("/api/runs/<run_id>/log")
def api_run_log(run_id):
    p = history.run_log_path(run_id)
    if not p:
        return _err("log not found", 404)
    return app.response_class(p.read_text(encoding="utf-8", errors="replace"),
                              mimetype="text/plain")


# ------------------------------------------------------------------- stacks
# tiny TTL caches so the 15s UI polling doesn't hammer Portainer with a
# fresh TCP+TLS session on every tick (data is informational, staleness OK)
_TTL_STACKS = 10      # seconds - stack list redeploys show up within 10s
_TTL_INVENTORY = 120  # seconds - dashboard card only
_stacks_cache: dict = {}    # endpoint_id -> (ts, payload)
_inventory_cache: dict = {}  # endpoint_id -> (ts, payload)


def _cache_get(cache: dict, key, ttl: float):
    hit = cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_put(cache: dict, key, payload):
    cache[key] = (time.time(), payload)
    return payload


@app.route("/api/stacks")
def api_stacks():
    """Cache-only read: NEVER triggers registry calls in the request thread."""
    client, e = _client_or_error()
    if e:
        return e
    eid = client.endpoint_id
    check = checker.get_cached()  # returns snapshot immediately, refreshes in bg
    cached = _stacks_cache.get(eid)
    if cached and time.time() - cached[0] < _TTL_STACKS:
        stacks = cached[1]
    else:
        stacks = []
        for s in client.stacks():
            if s.get("EndpointId") != eid:
                continue
            stacks.append({"Id": s["Id"], "Name": s.get("Name"),
                           "EndpointId": eid, "Status": s.get("Status")})
        _stacks_cache[eid] = (time.time(), stacks)
    out = []
    for s in stacks:
        name = s["Name"]
        svc_check = (check.get("stacks") or {}).get(name, {})
        out.append({
            "id": s["Id"], "name": name, "status": s.get("Status"),
            "services": svc_check,
            "updates_available": sum(1 for v in svc_check.values()
                                     if v.get("update_available")),
        })
    return jsonify({"endpoint_id": eid,
                    "checked_at": check.get("checked_at"),
                    "check_running": bool(check.get("running")),
                    "stacks": out})


@app.route("/api/stacks/<int:stack_id>")
def api_stack_detail(stack_id):
    client, e = _client_or_error()
    if e:
        return e
    eid = client.endpoint_id
    try:
        stacks = client.stacks()
    except PortainerError as err:
        return _err(str(err), 502)
    stack = next((s for s in stacks if s["Id"] == stack_id), None)
    if not stack:
        return _err("stack not found", 404)
    try:
        content = client.stack_file(stack_id, eid)
    except PortainerError as e:
        return _err(str(e), 502)
    images = compose_mod.services_images(content)
    check = checker.get_cached().get("stacks", {}).get(stack.get("Name"), {})
    return jsonify({
        "id": stack_id, "name": stack.get("Name"),
        "compose": content, "images": images,
        "checks": check,
    })


@app.route("/api/stacks/<int:stack_id>/image", methods=["POST"])
def api_stack_set_image(stack_id):
    """Pin a service to a specific version (or 'latest').

    Body: {"service": "...", "image": "nginx:1.27.3"}
    Edits the compose file via the Portainer API and redeploys the stack.
    """
    body = request.get_json(silent=True) or {}
    service, image = body.get("service"), (body.get("image") or "").strip()
    if not isinstance(service, str) or not re.match(r"^[A-Za-z0-9_.-]{1,128}$", service) \
            or not compose_mod.valid_image_ref(image):
        return _err("service and a valid image reference required")
    if not gate.try_acquire("pin-version"):
        return _err("another job is running - see /api/runs/current", 409)
    try:
        client = _client()
        eid = client.endpoint_id
        stacks = client.stacks()
        stack = next((s for s in stacks if s["Id"] == stack_id), None)
        if not stack:
            return _err("stack not found", 404)
        if stack.get("GitConfig"):
            # a StackFileContent PUT would detach the stack from its repo
            return _err("this stack is deployed from git - change the image "
                        "in the repository instead", 409)
        content = client.stack_file(stack_id, eid)
        new_content = compose_mod.set_image(content, service, image)
        name = stack.get("Name") or f"stack-{stack_id}"
        project = updater._project_name(name)
        env = stack.get("Env") or []
        # the state before the pin is a rollback point by definition
        pre = updater._running_state(client, project, eid, content, env)
        client.update_stack(stack_id, eid, new_content, env)
        updater._commit_snapshot(client, name, project, eid, pre,
                                 lambda m: None, force=True)
        return jsonify({"ok": True, "service": service, "image": image,
                        "redeployed": True})
    except (ValueError, PortainerError) as e:
        return _err(str(e), 500)
    finally:
        gate.release()


@app.route("/api/stacks/<int:stack_id>/redeploy", methods=["POST"])
def api_stack_redeploy(stack_id):
    """Redeploy one stack. Runs in the background (it waits up to
    deploy_wait_time for healthy containers); progress via the run banner."""
    client, e = _client_or_error()
    if e:
        return e
    try:
        stack = next((s for s in client.stacks() if s["Id"] == stack_id), None)
    except PortainerError as ex:
        return _err(str(ex), 502)
    if not stack:
        return _err("stack not found", 404)
    eid = client.endpoint_id
    return _job("redeploy", "redeploy",
                lambda rl: updater.redeploy_single_stack(client, stack, eid, rl.log),
                background=True)


# ------------------------------------------------------------------ versions
@app.route("/api/stacks/<int:stack_id>/rollback_info")
def api_rollback_info(stack_id):
    """Diff for the rollback dialog: what 'Restore last healthy' would
    change on this stack (current images vs the stored pre-update snapshot)."""
    client, e = _client_or_error()
    if e:
        return e
    try:
        stack = next((s for s in client.stacks() if s["Id"] == stack_id), None)
    except PortainerError as ex:
        return _err(str(ex), 502)
    if not stack:
        return _err("stack not found", 404)
    name = stack.get("Name") or f"stack-{stack_id}"
    from . import snapshots as snap_mod
    snap = snap_mod.get_snapshot(name)
    if not snap:
        return jsonify({"ok": False, "has_snapshot": False,
                        "message": "No pre-update snapshot for this stack yet - "
                                   "one is captured on the next update run."})
    try:
        content = client.stack_file(stack_id, client.endpoint_id)
        current = updater._resolved_images(content, _stack_env_of(stack))
    except (PortainerError, RuntimeError) as ex:
        return _err(str(ex), 502)
    rows = snap_mod.diff_snapshot(name, current)
    return jsonify({"ok": True, "has_snapshot": True,
                    "snapshot_ts": snap.get("ts"),
                    "snapshot_run_id": snap.get("run_id"),
                    "changes": rows,
                    "unchanged": len((snap.get("images") or {}) or {}) - len(rows)})


def _stack_env_of(stack) -> list:
    return stack.get("Env") or []


def _run_rollback_worker(runlog, stack_name, target_images):
    try:
        ok = updater.rollback_stack(runlog, stack_name, target_images)
        runlog.finish(ok)
    except Exception:
        import traceback
        err = traceback.format_exc()
        try:
            runlog.log("[ERROR] rollback crashed:\n" + err)
        except Exception:  # noqa: BLE001
            pass
        runlog.finish(False)
    finally:
        gate.release()
        from .scheduler import scheduler as sched
        sched._bump_full()


@app.route("/api/stacks/<int:stack_id>/rollback", methods=["POST"])
def api_stack_rollback(stack_id):
    """'Restore last healthy': async rollback of ONE stack to its stored
    pre-update image snapshot.

    The images always come from the server-side snapshot - never from the
    request. A body {"images": {...}} may only narrow the restore to some
    services and must name exactly the snapshot's images."""
    body = request.get_json(silent=True) or {}
    wanted = body.get("images") or {}
    if not isinstance(wanted, dict):
        return _err("images must be a map service -> image")
    if not gate.try_acquire("rollback"):
        return _err("another job is running - see /api/runs/current", 409)
    handed_off = False
    try:
        client, e = _client_or_error()
        if e:
            return e
        try:
            stack = next((s for s in client.stacks() if s["Id"] == stack_id), None)
        except PortainerError as ex:
            return _err(str(ex), 502)
        if not stack:
            return _err("stack not found", 404)
        from . import snapshots as snap_mod
        snap = snap_mod.get_snapshot(stack.get("Name", "")) or {}
        target = dict(snap.get("images") or {})
        if not target:
            return _err("no snapshot for this stack", 404)
        if wanted:
            if any(target.get(svc) != img for svc, img in wanted.items()):
                return _err("requested images do not match the stored snapshot", 409)
            target = {svc: target[svc] for svc in wanted}
        runlog = RunLogger("rollback", "manual")
        gate.set_runlog(runlog)
        runlog.step("rollback_started", True)
        threading.Thread(target=_run_rollback_worker,
                         args=(runlog, stack.get("Name", ""), target),
                         daemon=True).start()
        handed_off = True  # the worker releases the gate
        return jsonify({"ok": True, "run_id": runlog.run_id})
    finally:
        if not handed_off:
            gate.release()


@app.route("/api/versions")
def api_versions():
    """Known tags for an image ref: /api/versions?image=nginx"""
    image = request.args.get("image", "")
    if not image or len(image) > 300:
        return _err("image query param required")
    return jsonify(dockerhub.available_versions(image))


# ------------------------------------------------------------------- actions
@app.route("/api/check", methods=["POST"])
def api_check():
    """Fire-and-forget update check. The gate is held for the DURATION of
    the check (released in the worker thread), so a full update can't start
    mid-check."""
    if not gate.try_acquire("check"):
        return _err("another job is running - see /api/runs/current", 409)
    try:
        try:
            client = _client()
        except PortainerError as e:
            return _err(str(e), 502)
        force = bool((request.get_json(silent=True) or {}).get("force"))
        started = threading.Thread(
            target=_check_worker, args=(client, client.endpoint_id, force),
            daemon=True)
        started.start()
        return jsonify({"ok": True, "started": True})
    except Exception as e:  # noqa: BLE001 - spawn failure must release the gate
        gate.release()
        return _err(f"could not start check: {e}", 500)


def _check_worker(client, eid, force):
    """Runs OUTSIDE the request context - releases the gate when done."""
    try:
        checker.run_check(client, eid, force=force)
    except Exception as e:  # noqa: BLE001 - defensive: check must never crash silently
        import sys
        print(f"[check] worker crashed: {e}", file=sys.stderr, flush=True)
    finally:
        gate.release()


@app.route("/api/update", methods=["POST"])
def api_update():
    run_id = scheduler.trigger_update_async("manual")
    if not run_id:
        return _err("another job is running - see /api/runs/current", 409)
    return jsonify({"ok": True, "run_id": run_id})


@app.route("/api/self_update", methods=["POST"])
def api_self_update():
    """'Update Quaykeeper' button: deliberately redeploy this app's own stack.

    Runs in a thread - the redeploy replaces this container mid-request,
    so the response may never reach the browser (expected!). The run is
    recorded in history BEFORE the container dies."""
    if not gate.try_acquire("self_update"):
        return _err("another job is running - see /api/runs/current", 409)
    handed_off = False
    try:
        runlog = RunLogger("self_update", "manual")
        gate.set_runlog(runlog)
        runlog.step("self_update_started", True)

        def _worker():
            # the worker owns the gate until the redeploy returned: releasing
            # it earlier let a scheduled update interleave with the redeploy
            try:
                updater.self_update_run(runlog)
            except Exception:  # noqa: BLE001 - recorded, never silent
                import traceback
                runlog.log("[ERROR] self-update crashed:\n" + traceback.format_exc())
                runlog.finish(False)  # idempotent if finish_neutral() ran
            finally:
                gate.release()

        threading.Thread(target=_worker, daemon=True).start()
        handed_off = True
        # give the redeploy a moment to fire; if the container survives
        # (nothing to do / error), the worker has finished already
        threading.Event().wait(2.0)
        return jsonify({"ok": True, "run_id": runlog.run_id,
                        "note": "redeploying - container will restart"})
    finally:
        if not handed_off:
            gate.release()


@app.route("/api/inventory")
def api_inventory():
    """Docker inventory for the dashboard: unused images (reclaimable),
    unused networks, totals. Read-only, TTL-cached (120s)."""
    client, e = _client_or_error()
    if e:
        return e
    eid = client.endpoint_id
    cached = _inventory_cache.get(eid)
    if cached and time.time() - cached[0] < _TTL_INVENTORY:
        return jsonify(cached[1])
    try:
        imgs = client.images(eid)
        cs = client.all_containers(eid)
        nets = client.networks(eid)
    except PortainerError as err:
        return _err(str(err), 502)
    used_ids = {c.get("ImageID") for c in cs if c.get("ImageID")}
    unused = [i for i in imgs if i.get("Id") not in used_ids]
    unused_mb = sum((i.get("Size") or 0) for i in unused) // (1024 * 1024)
    from .cleanup import unused_networks
    unused_nets = unused_networks(nets, cs)
    payload = {
        "images_total": len(imgs),
        "images_unused": len(unused),
        "images_unused_mb": unused_mb,
        "networks_total": len(nets),
        "networks_unused": len(unused_nets),
        "containers_total": len(cs),
    }
    _inventory_cache[eid] = (time.time(), payload)
    return jsonify(payload)


@app.route("/api/prune", methods=["POST"])
def api_prune():
    client, e = _client_or_error()
    if e:
        return e
    eid = client.endpoint_id

    def work(rl):
        # 'Prune all' is the explicit everything-button: rollback images too
        ok = updater.cleanup_docker(client, eid, rl.log, keep_rollback=False)
        rl.step("docker_prune", ok)
        return ok
    return _job("prune", "prune", work, background=True)


@app.route("/api/cleanup", methods=["GET"])
def api_cleanup_get():
    """Unused images/networks (removable) plus stopped containers, unused
    volumes and build cache (shown only) - see app/cleanup.py."""
    client, e = _client_or_error()
    if e:
        return e
    from . import cleanup
    try:
        return jsonify(cleanup.analyze(client, client.endpoint_id))
    except PortainerError as err:
        return _err(str(err), 502)


@app.route("/api/cleanup", methods=["POST"])
def api_cleanup_post():
    """Remove the picked items: {"images": [ids], "networks": [ids],
    "build_cache": bool}. Recorded as a 'cleanup' run in the history."""
    body = request.get_json(silent=True) or {}

    def ids(key):
        v = body.get(key) or []
        if not isinstance(v, list) or len(v) > 500 or \
                not all(isinstance(x, str) and 0 < len(x) <= 128 for x in v):
            raise ValueError(key)
        return list(dict.fromkeys(v))
    try:
        image_ids, network_ids = ids("images"), ids("networks")
    except ValueError as bad:
        return _err(f"'{bad}' must be a list of ids", 400)
    build_cache = bool(body.get("build_cache"))
    if not (image_ids or network_ids or build_cache):
        return _err("nothing selected", 400)
    client, e = _client_or_error()
    if e:
        return e
    eid = client.endpoint_id
    from . import cleanup

    def work(rl):
        try:
            ok = cleanup.run_cleanup(client, eid, image_ids, network_ids,
                                     build_cache, rl.log)
        except PortainerError as err:
            rl.log(f"[ERROR] {err}")
            ok = False
        rl.step("cleanup", ok)
        return ok
    return _job("cleanup", "cleanup", work, background=False)


# ------------------------------------------------------------------ settings
@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    cfg = dict(load())
    # never expose the secrets themselves
    cfg["portainer_api_key"] = "***" if cfg.get("portainer_api_key") else ""
    cfg["auth_token"] = "***" if cfg.get("auth_token") else ""
    # flat alias mirroring cfg["repairs"]["enabled"] (UI checkbox reads this)
    cfg["repairs_enabled"] = bool(cfg.get("repairs", {}).get("enabled", True))
    return jsonify(cfg)


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    body = request.get_json(silent=True) or {}
    allowed = {"portainer_url", "portainer_api_key", "portainer_endpoint_id",
               "update_interval_hours", "update_schedule_mode",
               "update_schedule_time", "update_schedule_day",
               "tls_verify", "max_parallel_deploys", "tls_ca_file",
               "deploy_wait_time", "keep_backups",
               "check_cache_minutes", "listen_port", "auth_token",
               "notify_webhook", "include_portainer",
               "repairs_enabled"}
    # two-pass: validate EVERYTHING first, then apply - a bad key must never
    # leave earlier keys mutated in memory (memory/disk divergence)
    from .config import validate
    validated = {}
    for k, v in body.items():
        if k not in allowed:
            return _err(f"unknown setting: {k}")
        if k == "portainer_api_key" and v == "***":
            continue
        if k == "auth_token" and v == "***":
            continue
        try:
            validated[k] = validate(k, v)  # raises ConfigError on garbage
        except ConfigError as e:
            return _err(str(e))
    from .config import is_loopback
    new_url = validated.get("portainer_url")
    if new_url is not None and new_url.rstrip("/") != (get("portainer_url") or "").rstrip("/") \
            and get("portainer_api_key") and "portainer_api_key" not in validated:
        # the stored admin key must never follow a URL change silently
        return _err("enter the API key again when changing the Portainer URL")
    if "auth_token" in validated and not validated["auth_token"] \
            and not is_loopback(get("listen_host", "127.0.0.1")):
        return _err("auth_token cannot be empty while Quaykeeper listens beyond "
                    "localhost")
    for k, v in validated.items():
        cfg_set(k, v)
    cfg_save()
    return jsonify({"ok": True, "applied": sorted(validated)})


# ----------------------------------------------------------------------- main
def _ensure_auth_token(host) -> None:
    """Listening beyond localhost without a token would hand the admin
    Portainer key's power to everyone on the network. Generate one on first
    start, store it in config.yaml and print it once."""
    from .config import is_loopback
    if get("auth_token", "") or is_loopback(host):
        return
    import secrets
    token = secrets.token_urlsafe(24)
    cfg_set("auth_token", token)
    cfg_save()
    line = "=" * 64
    print(f"{line}\n[quaykeeper] No auth_token was set, so one was generated:\n\n"
          f"    {token}\n\nEnter it in the browser when asked. It is stored in "
          f"config/config.yaml\n(set QK_AUTH_TOKEN to choose your own).\n{line}",
          flush=True)


def main():
    load()
    from .history import mark_interrupted
    mark_interrupted()  # convert stale 'running' markers from a crash
    host = get("listen_host", "127.0.0.1")
    port = int(get("listen_port", 8090))
    _ensure_auth_token(host)
    scheduler.start()
    # production WSGI server (waitress): graceful threads, no dev-server warning.
    # ARCHITECTURAL INVARIANT: this app MUST run as a single process with N
    # threads. The JobGate (app/jobs.py), checker caches and the state files
    # (status.json/history.jsonl) are per-process - running under multiple
    # workers (e.g. gunicorn -w 4) would race concurrent full-updates and
    # corrupt state. Threads>1 is fine; workers>1 is not.
    from waitress import serve
    print(f"[quaykeeper] listening on http://{host}:{port}")
    serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()