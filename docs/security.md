# Security

> [!WARNING]
> The UI has **no login**. If it is reachable from other machines, set
> `auth_token` (or `QK_AUTH_TOKEN`) — that protects every mutating endpoint —
> or put an authenticated reverse proxy in front.

- Mutating requests are also CSRF-guarded when no token is configured: a
  cross-site form post cannot trigger an update.
- The API key is stored in `config.yaml`, never returned by the API, and never
  committed.
- `tls_verify: false` accepts Portainer's default self-signed certificate. If
  Portainer has a real certificate, turn verification on; if it is self-signed,
  mount its certificate and point `tls_ca_file` at it.
- No telemetry, no analytics, no outbound calls except to your Portainer and the
  image registries.

---

[← Back to README](../README.md) · [Installation](installation.md) · [Usage](usage.md) · [Configuration](configuration.md) · [Security](security.md) · [API](api.md)
