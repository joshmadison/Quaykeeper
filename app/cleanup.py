"""Docker leftovers: what is unused, and what is safe to remove.

Only reads here; removal lives in run_cleanup() further down (added with
the Cleanup tab).
"""

# Docker's own networks - always present, never 'unused'
BUILTIN_NETWORKS = {"bridge", "host", "none"}


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
        out.append(n)
    return out
