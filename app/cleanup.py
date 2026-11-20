"""Docker leftovers: what is unused, and what is safe to remove.

analyze() builds the Cleanup tab: unused images, unused networks, stopped
containers, unused volumes and build cache - each item flagged with
whether removing it is safe. run_cleanup() removes exactly the items the
user picked, one by one, after re-checking that each is still unused.

Deliberately NOT removable from here:
- stopped containers: 'stopped' means 'intentionally off' (see updater)
- volumes: they hold data
Both are listed so the user sees them, nothing more.
"""
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
def analyze(client, eid, networks: bool = True) -> dict:
    """Everything the Cleanup tab shows. Read-only.
    networks=False: images only (used by the automatic prune of a run)."""
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
        "name": ((c.get("Names") or ["?"])[0] or "?").lstrip("/"),
        "image": c.get("Image"),
        "project": (c.get("Labels") or {}).get(PROJECT_LABEL),
        "state": c.get("State"),
        "status": c.get("Status"),
    } for c in cs if c.get("State") != "running"]

    volumes = None
    try:
        in_use = {m.get("Name") for c in cs for m in (c.get("Mounts") or [])
                  if m.get("Type") == "volume"}
        volumes = [{
            "name": v.get("Name"),
            "driver": v.get("Driver"),
            "project": (v.get("Labels") or {}).get(PROJECT_LABEL),
            "created": v.get("CreatedAt"),
        } for v in client.volumes(eid) if v.get("Name") not in in_use]
    except (PortainerError, AttributeError):
        pass

    build_cache_mb = None
    try:
        build_cache_mb = round(sum((b.get("Size") or 0) for b in client.build_cache(eid)
                                   if not b.get("InUse")) / MB, 1)
    except (PortainerError, AttributeError):
        pass

    return {
        "images": images,
        "networks": networks,
        "stopped_containers": stopped,
        "volumes": volumes,
        "build_cache_mb": build_cache_mb,
        "safe_images_mb": round(sum(i["size_mb"] for i in images if i["safe"]), 1),
        "analyzed_at": time.time(),
    }


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


def run_cleanup(client, eid, image_ids, network_ids, build_cache, log) -> bool:
    """Remove exactly the picked items. Every id is re-checked against a
    fresh analyze(): anything that got used again (or vanished) since the
    user looked is skipped, never forced. Returns True if nothing failed."""
    current = analyze(client, eid, networks=bool(network_ids))
    imgs = {i["id"]: i for i in current["images"]}
    nets = {n["id"]: n for n in current["networks"]}
    failed = 0
    removed_imgs, freed = 0, 0.0
    for iid in image_ids:
        img = imgs.get(iid)
        label = (img and (img["tags"] or [iid[:19]])[0]) or iid[:19]
        if not img:
            log(f"[INFO] image {label}: no longer unused - skipped")
            continue
        try:
            remove_image(client, eid, img)
            removed_imgs += 1
            freed += img["size_mb"]
            log(f"[OK] removed image {label} ({img['size_mb']} MB)")
        except PortainerError as e:
            failed += 1
            log(f"[WARN] image {label}: {e}")
    removed_nets = 0
    for nid in network_ids:
        net = nets.get(nid)
        if not net:
            log(f"[INFO] network {nid[:12]}: no longer unused - skipped")
            continue
        try:
            client.remove_network(nid, eid)
            removed_nets += 1
            log(f"[OK] removed network {net['name']}")
        except PortainerError as e:
            failed += 1
            log(f"[WARN] network {net['name']}: {e}")
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
    log(f"Cleanup done: {removed_imgs} image(s), {removed_nets} network(s), "
        f"~{total} MB freed" + (f", {failed} failed" if failed else "") + ".")
    if removed_imgs or cache_mb:
        try:
            from .history import record_prune_stats
            record_prune_stats(reclaimed_mb=total, images_deleted=removed_imgs)
        except Exception:  # noqa: BLE001 - stats must never fail the run
            pass
    return failed == 0
