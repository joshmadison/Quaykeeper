# API

Every UI action is an API call, so scripts and dashboards can use it too.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/status` | State, last run, current job, next run |
| `GET` | `/api/stacks` | Stacks with per-service check results |
| `GET` | `/api/stacks/<id>` | Stack detail and compose file |
| `POST` | `/api/stacks/<id>/image` | Pin a version: `{"service":"web","image":"nginx:1.27.3"}` |
| `POST` | `/api/stacks/<id>/redeploy` | Redeploy one stack |
| `GET` | `/api/stacks/<id>/rollback_info` | What a rollback would change |
| `POST` | `/api/stacks/<id>/rollback` | Restore a stack's pre-update images |
| `POST` | `/api/check` | Force an update check |
| `POST` | `/api/update` | Start a full update run |
| `POST` | `/api/prune` | Prune images and build cache now |
| `POST` | `/api/self_update` | Redeploy Quaykeeper's own stack |
| `GET` | `/api/inventory` | Unused images and networks |
| `GET` | `/api/runs/current` | Live progress of the active job |
| `GET` | `/api/runs/<id>/log` | Full run log (plain text) |
| `GET` | `/api/history` | Recent runs and 30-day prune stats |
| `GET` | `/api/versions?image=nginx` | Available tags for an image |
| `GET` | `/api/endpoints` | Portainer endpoints |
| `GET`/`POST` | `/api/settings` | Read or change settings |
| `GET` | `/healthz` | Liveness, scheduler heartbeat and whether a job is running |

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
     https://your-host:8090/api/update
```

---

[← Back to README](../README.md) · [Installation](installation.md) · [Usage](usage.md) · [Configuration](configuration.md) · [Security](security.md) · [API](api.md)
