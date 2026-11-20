# Security

## Authentication

Every `/api/*` call — reads included — needs `Authorization: Bearer <token>`
as soon as a token is set. Only `/healthz` and the UI files are public.

- **Reachable beyond localhost ⇒ token required.** If Quaykeeper listens on
  anything but `127.0.0.1`/`localhost` (always the case in a container) and no
  `auth_token` / `QK_AUTH_TOKEN` is set, it generates one on first start,
  stores it in `config/config.yaml` and prints it once in the log
  (`docker logs quaykeeper`). The browser asks for it once.
- The token cannot be cleared from the UI while Quaykeeper is exposed.
- **Without a token** (local Python on `127.0.0.1`), requests must use a
  loopback host name — this blocks DNS-rebinding pages — and mutating requests
  must come from the same origin (CSRF guard).
- Behind a reverse proxy (Caddy, Traefik, NPM, `tailscale serve`) the token is
  what authenticates; the origin check does not get in the way.

## The Portainer API key

- Stored in `config.yaml`, never returned by the API, never logged, never
  committed. Values that come from environment variables are never written to
  `config.yaml`.
- The key is only ever sent to the configured Portainer URL. *Test connection*
  and *Scan* against a different URL need that URL's own key, and changing the
  URL in Settings requires entering the key again.

## TLS

- `tls_verify: false` (default) accepts Portainer's self-signed certificate.
- `tls_verify: true` verifies against the system CAs — or against
  `tls_ca_file` if set (mount Portainer's certificate for self-signed setups).

## Docker socket and changes to stacks

- The Docker socket is not mounted by default; only *updating Portainer
  itself* needs it.
- Pinned versions must be valid image references; a rollback always restores
  the images from the stored snapshot, never images named in the request.

- No telemetry, no analytics, no outbound calls except to your Portainer and the
  image registries.

---

[← Back to README](../README.md) · [Installation](installation.md) · [Usage](usage.md) · [Configuration](configuration.md) · [Security](security.md) · [API](api.md)
