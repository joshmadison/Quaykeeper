"""Docker leftovers: what is unused, and what is safe to remove.

analyze() builds the Cleanup tab. Every item carries `safe`:
- safe   - nothing of value is lost: unused images that no rollback needs,
           anonymous volumes nothing uses, orphaned networks, one-off
           containers that ended long ago, build cache
- kept   - shown with the reason, removable only on purpose: rollback
           images, named volumes (they hold data), networks/volumes of
           stacks that still exist
Containers that belong to a stack are never removable here: 'stopped'
means 'intentionally off'.

run_cleanup() removes exactly the picked items, one by one and without
force, after re-checking that each is still unused.
"""
import re
import time

from .portainer import PortainerError

MB = 1024 * 1024
PROJECT_LABEL = "com.docker.compose.project"

# Docker's own networks - always present, never 'unused'
BUILTIN_NETWORKS = {"bridge", "host", "none"}
# never offered for removal: Swarm infrastructure (routing mesh)
SYSTEM_NETWORKS = {"ingress", "docker_gwbridge"}
# hold host-specific settings (parent interface, subnet) that are hard to
# recreate - removable only on purpose
SPECIAL_DRIVERS = {"macvlan", "ipvlan"}
# a one-off container (no stack) that ended longer ago than this is safe
ONE_OFF_AGE_DAYS = 30
# containers managed by another tool that starts/stops them itself
MANAGED_PREFIXES = ("nextcloud-aio-", "portainer")
ANON_VOLUME = re.compile(r"^[0-9a-f]{64}$")


def network_ids_in_use(containers) -> tuple:
    """(ids, names) of networks referenced by any container, running or not.

    /containers/json keys NetworkSettings.Networks by network NAME and carries
    the id in NetworkID - comparing the keys against network ids (as the
    inventory did before) matched nothing and reported every network as
    unused."""
    ids, names = set(), set()
    for c in containers or []:
        nets = (c.get("NetworkSettings") or {}).get("Networks") or {}
        for name, info in nets.items():
            names.add(name)
            nid = (info or {}).get("NetworkID")
            if nid:
                ids.add(nid)
    return ids, names


def unused_networks(networks, containers) -> list:
    """Networks no container (running or stopped) is attached to,
    excluding Docker's built-in bridge/host/none."""
    ids, names = network_ids_in_use(containers)
    out = []
    for n in networks or []:
        name = n.get("Name") or ""
        if name in BUILTIN_NETWORKS:
            continue
        if n.get("Id") in ids or name in names:
            continue
        if name in SYSTEM_NETWORKS or n.get("Ingress") or n.get("Scope") == "swarm":
            continue  # Swarm: in use on other nodes / routing mesh
        out.append(n)
    return out


# ------------------------------------------------------------------ helpers
def _norm_ref(ref: str) -> str:
    """docker.io/library/redis:7 -> redis:7 (RepoTags use the short form)."""
    ref = (ref or "").strip()
    for prefix in ("docker.io/library/", "docker.io/", "index.docker.io/library/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def _tags(img) -> list:
    return [t for t in (img.get("RepoTags") or []) if t and t != "<none>:<none>"]


def _rollback_refs() -> dict:
    """{key: [stack names]} of all rollback snapshots - images a 'Restore
    these versions' would need again. Keys are normalized tag refs
    ('redis:7') and, for snapshots pinned to a digest, 'repo@sha256:...'
    (matches the image even after its tag moved on and it went dangling)."""
    from .snapshots import _load_all
    out: dict = {}
    for stack, snap in (_load_all() or {}).items():
        for ref in ((snap or {}).get("images") or {}).values():
            base, _, digest = (ref or "").partition("@")
            out.setdefault(_norm_ref(base), []).append(stack)
            if digest:
                repo = base.rsplit(":", 1)[0] if ":" in base.rsplit("/", 1)[-1] else base
                out.setdefault(f"{_norm_ref(repo)}@{digest}", []).append(stack)
    return out


def _image_keys(img) -> list:
    keys = [_norm_ref(t) for t in _tags(img)]
    for rd in img.get("RepoDigests") or []:
        repo, _, digest = rd.partition("@")
        if digest:
            keys.append(f"{_norm_ref(repo)}@{digest}")
    return keys


def _external_networks(client, eid) -> dict:
    """{network name: [stack names]} declared 'external: true' in any
    Portainer stack - such a network may be unattached while its stacks are
    down, and removing it makes their next deploy fail."""
    import yaml
    out: dict = {}
    try:
        stacks = [s for s in client.stacks() if s.get("EndpointId") in (None, eid)]
    except (PortainerError, AttributeError):
        return out
    for s in stacks:
        try:
            data = yaml.safe_load(client.stack_file(s["Id"], eid) or "") or {}
        except Exception:  # noqa: BLE001 - one bad file must not hide the rest
            continue
        for key, spec in ((data.get("networks") or {}) if isinstance(data, dict) else {}).items():
            if isinstance(spec, dict) and spec.get("external"):
                ext = spec.get("external")
                real = spec.get("name") or (ext.get("name") if isinstance(ext, dict) else None) or key
                out.setdefault(real, []).append(s.get("Name") or "?")
    return out


def _known_projects(client, eid, containers) -> set:
    projects = {((c.get("Labels") or {}).get(PROJECT_LABEL) or "").lower()
                for c in containers}
    try:
        projects |= {(s.get("Name") or "").lower() for s in client.stacks()
                     if s.get("EndpointId") in (None, eid)}
    except (PortainerError, AttributeError):
        pass
    projects.discard("")
    return projects


# ------------------------------------------------------------------ analyze
def analyze(client, eid, networks: bool = True, details: bool = True) -> dict:
    """Everything the Cleanup tab shows. Read-only.
    networks=False: images only (the automatic prune of an update run).
    details=False: skip disk usage and tips (re-check before removing)."""
    imgs = client.images(eid)
    cs = client.all_containers(eid)
    nets = client.networks(eid) if networks else []
    projects = _known_projects(client, eid, cs)
    external = _external_networks(client, eid) if networks else {}
    rollback = _rollback_refs()

    used_img = {c.get("ImageID") for c in cs if c.get("ImageID")}
    images = []
    for i in imgs:
        if i.get("Id") in used_img:
            continue
        tags = _tags(i)
        needed_by = sorted({st for k in _image_keys(i) for st in rollback.get(k, [])})
        images.append({
            "id": i.get("Id"),
            "tags": tags,
            "size_mb": round((i.get("Size") or 0) / MB, 1),
            "created": i.get("Created"),
            "dangling": not tags,
            "rollback_for": needed_by,
            "safe": not needed_by,
            "note": (f"previous version of {', '.join(needed_by)} - needed "
                     f"for a rollback") if needed_by else "",
        })
    images.sort(key=lambda x: -x["size_mb"])

    networks = []
    for n in unused_networks(nets, cs):
        name = n.get("Name") or ""
        project = ((n.get("Labels") or {}).get(PROJECT_LABEL) or "").lower()
        driver = (n.get("Driver") or "").lower()
        if name in external:
            note = (f"declared external by {', '.join(sorted(external[name]))} - "
                    f"needed when that stack starts")
        elif project and project in projects:
            note = f"belongs to stack '{project}' - kept so the stack can start again"
        elif driver in SPECIAL_DRIVERS:
            note = (f"{driver} network - holds parent interface and subnet; "
                    f"recreating it needs those settings")
        else:
            note = ""
        networks.append({
            "id": n.get("Id"),
            "name": name,
            "driver": n.get("Driver"),
            "created": n.get("Created"),
            "project": project or None,
            "safe": not note,
            "note": note or ("orphaned compose network" if project else "no stack"),
        })
    networks.sort(key=lambda x: (not x["safe"], x["name"] or ""))

    stopped = [{
        "id": c.get("Id"),
        "name": _cname(c),
        "image": c.get("Image"),
        "project": (c.get("Labels") or {}).get(PROJECT_LABEL),
        "state": c.get("State"),
        "status": c.get("Status"),
    } for c in cs if c.get("State") != "running"]

    df = {}
    if networks and details:
        try:
            df = client.system_df(eid) or {}
        except (PortainerError, AttributeError):
            df = {}

    containers = _old_containers(cs) if networks else []
    volumes = _volumes(client, eid, cs, projects, df) if networks else []

    build_cache_mb = None
    try:
        recs = df.get("BuildCache")
        if recs is None:
            recs = client.build_cache(eid)
        build_cache_mb = round(sum((b.get("Size") or 0) for b in recs or []
                                   if not b.get("InUse")) / MB, 1)
    except (PortainerError, AttributeError):
        pass

    hints = _hints(client, eid, cs) if networks and details else []
    usage = _usage(df)
    safe_mb = (sum(i["size_mb"] for i in images if i["safe"])
               + sum(v["size_mb"] or 0 for v in volumes or [] if v["safe"])
               + sum(c["size_mb"] or 0 for c in containers if c["safe"])
               + (build_cache_mb or 0))

    return {
        "images": images,
        "networks": networks,
        "volumes": volumes,
        "containers": containers,
        "stopped_containers": stopped,
        "build_cache_mb": build_cache_mb,
        "safe_images_mb": round(sum(i["size_mb"] for i in images if i["safe"]), 1),
        "safe_total_mb": round(safe_mb, 1),
        "usage": usage,
        "hints": hints,
        "analyzed_at": time.time(),
    }


def _cname(c) -> str:
    return ((c.get("Names") or ["?"])[0] or "?").lstrip("/")


def _old_containers(cs) -> list:
    """Stopped containers that belong to no stack. Safe when they ended a
    while ago (typical 'docker run' without --rm); containers managed by
    another tool (Nextcloud AIO, Portainer) are kept."""
    out = []
    now = time.time()
    for c in cs:
        if c.get("State") == "running" or c.get("State") == "restarting":
            continue
        labels = c.get("Labels") or {}
        if labels.get(PROJECT_LABEL) or labels.get("com.docker.stack.namespace"):
            continue  # part of a stack: stopped on purpose
        name = _cname(c)
        age_days = (now - (c.get("Created") or now)) / 86400
        managed = name.startswith(MANAGED_PREFIXES)
        safe = not managed and age_days >= ONE_OFF_AGE_DAYS
        out.append({
            "id": c.get("Id"),
            "name": name,
            "image": c.get("Image"),
            "status": c.get("Status"),
            "created": c.get("Created"),
            "size_mb": round((c.get("SizeRw") or 0) / MB, 1) or None,
            "safe": safe,
            "note": ("managed by " + ("Nextcloud AIO" if name.startswith("nextcloud-aio-")
                                      else "Portainer")) if managed else
                    ("" if safe else f"stopped less than {ONE_OFF_AGE_DAYS} days ago"),
        })
    out.sort(key=lambda x: (not x["safe"], x["name"]))
    return out


def _volumes(client, eid, cs, projects, df) -> list:
    """Unused volumes. Anonymous ones (64-hex name, created for an image's
    VOLUME path) are safe; named ones hold data on purpose and are kept."""
    try:
        vols = client.volumes(eid)
    except (PortainerError, AttributeError):
        return []
    in_use = {m.get("Name") for c in cs for m in (c.get("Mounts") or [])
              if m.get("Type") == "volume"}
    sizes = {v.get("Name"): (v.get("UsageData") or {}).get("Size")
             for v in (df.get("Volumes") or [])}
    out = []
    for v in vols:
        name = v.get("Name") or ""
        if not name or name in in_use:
            continue
        labels = v.get("Labels") or {}
        project = (labels.get(PROJECT_LABEL) or "").lower()
        anon = bool(ANON_VOLUME.match(name)) or "com.docker.volume.anonymous" in labels
        size = sizes.get(name)
        if anon:
            note = ""
        elif project and project in projects:
            note = f"data of stack '{project}' - kept"
        else:
            note = "named volume - may hold data"
        out.append({
            "name": name,
            "anonymous": anon,
            "project": project or None,
            "created": v.get("CreatedAt"),
            "size_mb": round(size / MB, 1) if isinstance(size, (int, float)) and size >= 0 else None,
            "safe": anon,
            "note": note,
        })
    out.sort(key=lambda x: (not x["safe"], -(x["size_mb"] or 0), x["name"]))
    return out


def _usage(df) -> dict:
    """Total Docker disk usage by category, in MB (None if unknown)."""
    if not df:
        return {}
    def mb(n): return round((n or 0) / MB, 1)
    images = sum(i.get("Size") or 0 for i in df.get("Images") or [])
    containers = sum(c.get("SizeRw") or 0 for c in df.get("Containers") or [])
    volumes = sum(max(0, (v.get("UsageData") or {}).get("Size") or 0)
                  for v in df.get("Volumes") or [])
    cache = sum(b.get("Size") or 0 for b in df.get("BuildCache") or [])
    return {"images_mb": mb(images), "containers_mb": mb(containers),
            "volumes_mb": mb(volumes), "build_cache_mb": mb(cache),
            "total_mb": mb(images + containers + volumes + cache)}


def anonymous_mounts(containers) -> list:
    """[(container name, project, path)] for every anonymous volume a
    container mounts - each recreate (update) leaves such a volume behind."""
    out = []
    for c in containers or []:
        for m in c.get("Mounts") or []:
            if m.get("Type") == "volume" and ANON_VOLUME.match(m.get("Name") or ""):
                out.append((_cname(c), (c.get("Labels") or {}).get(PROJECT_LABEL),
                            m.get("Destination")))
    return out


def _hints(client, eid, cs) -> list:
    """Short, actionable tips: what keeps creating leftovers or eats disk."""
    hints = []
    for name, project, path in anonymous_mounts([c for c in cs if c.get("State") == "running"]):
        hints.append({
            "kind": "anon_volume", "container": name, "stack": project, "path": path,
            "text": f"{name} keeps {path} in an anonymous volume - every update "
                    f"leaves one behind.",
            "fix": f"volumes:\n  - {re.sub(r'[^a-z0-9_]+', '_', (name + path).lower()).strip('_')}:{path}",
        })
    no_limit = []
    for c in cs:
        if c.get("State") != "running":
            continue
        try:
            hc = (client.inspect_container(c["Id"], eid) or {}).get("HostConfig") or {}
        except (PortainerError, AttributeError, KeyError):
            continue
        lc = hc.get("LogConfig") or {}
        if (lc.get("Type") or "json-file") == "json-file" and \
                not (lc.get("Config") or {}).get("max-size"):
            no_limit.append(_cname(c))
    if no_limit:
        hints.append({
            "kind": "log_limit", "containers": sorted(no_limit),
            "text": f"{len(no_limit)} container(s) keep their logs without a size "
                    f"limit - they grow forever.",
            "fix": 'logging:\n  options:\n    max-size: "10m"\n    max-file: "3"',
        })
    return hints


# ---------------------------------------------------------------- run
def remove_image(client, eid, img) -> None:
    """Remove one unused image without force: tag by tag (an ID that
    carries several tags is refused by Docker with 409 'referenced in
    multiple repositories'); untagged images by ID. The last untag removes
    the image."""
    tags = img.get("tags") or []
    if not tags:
        client.remove_image(img["id"], eid)
        return
    for tag in tags:
        client.remove_image(tag, eid)


def run_cleanup(client, eid, image_ids, network_ids, build_cache, log,
                volume_names=(), container_ids=()) -> bool:
    """Remove exactly the picked items. Every id is re-checked against a
    fresh analyze(): anything that got used again (or vanished) since the
    user looked is skipped, never forced. Returns True if nothing failed."""
    current = analyze(client, eid, networks=bool(network_ids or volume_names
                                                 or container_ids), details=False)
    imgs = {i["id"]: i for i in current["images"]}
    nets = {n["id"]: n for n in current["networks"]}
    vols = {v["name"]: v for v in current["volumes"] or []}
    ctrs = {c["id"]: c for c in current["containers"]}
    failed = 0
    count = {"image": 0, "network": 0, "volume": 0, "container": 0}
    freed = 0.0

    def attempt(kind, key, item, label, fn, mb=0.0):
        nonlocal failed, freed
        if not item:
            log(f"[INFO] {kind} {label}: no longer unused - skipped")
            return
        try:
            fn()
            count[kind] += 1
            freed += mb or 0
            log(f"[OK] removed {kind} {label}" + (f" ({mb} MB)" if mb else ""))
        except PortainerError as e:
            failed += 1
            log(f"[WARN] {kind} {label}: {e}")

    # containers first: a removed one-off container may free its image
    for cid in container_ids:
        c = ctrs.get(cid)
        attempt("container", cid, c, (c or {}).get("name") or cid[:12],
                lambda: client.remove_container(cid, eid), (c or {}).get("size_mb"))
    for iid in image_ids:
        img = imgs.get(iid)
        attempt("image", iid, img, ((img or {}).get("tags") or [iid[:19]])[0],
                lambda: remove_image(client, eid, img), (img or {}).get("size_mb"))
    for name in volume_names:
        v = vols.get(name)
        attempt("volume", name, v, name[:24] + ("…" if len(name) > 24 else ""),
                lambda: client.remove_volume(name, eid), (v or {}).get("size_mb"))
    for nid in network_ids:
        n = nets.get(nid)
        attempt("network", nid, n, (n or {}).get("name") or nid[:12],
                lambda: client.remove_network(nid, eid))
    cache_mb = 0
    if build_cache:
        try:
            r = client.prune_build_cache(eid)
            cache_mb = (r.get("SpaceReclaimed") or 0) // MB
            log(f"[OK] build cache pruned (~{cache_mb} MB)")
        except PortainerError as e:
            failed += 1
            log(f"[WARN] build cache prune failed: {e}")
    total = int(freed) + cache_mb
    parts = [f"{n} {k}(s)" for k, n in count.items() if n]
    log(f"Cleanup done: {', '.join(parts) or 'nothing removed'}, ~{total} MB freed"
        + (f", {failed} failed" if failed else "") + ".")
    if total or count["image"]:
        try:
            from .history import record_prune_stats
            record_prune_stats(reclaimed_mb=total, images_deleted=count["image"])
        except Exception:  # noqa: BLE001 - stats must never fail the run
            pass
    return failed == 0
