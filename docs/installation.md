# Installation

## Requirements

- A running **Portainer** instance
- A Portainer **API key with admin rights** — *Settings → API keys → Add API key*

The key needs admin rights because pruning and backups are admin-only
endpoints. Portainer may use a self-signed certificate; Quaykeeper expects
that by default.

## Install options

### Option A — as a Portainer stack (recommended)

1. **Stacks → Add stack → Repository**
2. Repository URL: `https://github.com/joshmadison/quaykeeper`
   Build method **Dockerfile**, compose path `docker-compose.yml`
3. Add these environment variables (only the first is required):

   | Variable | Value | Why |
   |---|---|---|
   | `QK_LISTEN_HOST` | `0.0.0.0` | Required inside a container |
   | `TZ` | e.g. `Europe/Berlin` | Timezone the schedule refers to |
   | `QK_AUTH_TOKEN` | a long random string | Protects the UI/API. If unset, one is generated on first start and printed in the container log |

4. **Deploy the stack**, then open `http://<host>:8090`
5. In **Settings**: enter the Portainer URL, paste the API key, **Test
   connection**, **Save**. The first update check starts by itself.

Settings are written to `config/config.yaml` in the config volume and survive
redeploys.

### Option B — docker compose

```bash
git clone https://github.com/joshmadison/quaykeeper.git
cd quaykeeper
docker compose up -d --build
# open http://localhost:8090 and finish setup in Settings
```

The shipped compose file leaves the credentials empty on purpose: configure
them in the UI, or uncomment `QK_PORTAINER_URL` / `QK_PORTAINER_API_KEY`.
Environment variables win over settings made in the UI.

### Option C — local Python

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
.venv/bin/pip install -r requirements.txt       # Linux / macOS

.venv/Scripts/python run.py                      # Windows
.venv/bin/python run.py                          # Linux / macOS
# open http://127.0.0.1:8090
```

> [!NOTE]
> The Docker socket is **optional**. Checks, backups, pruning and stack
> redeploys all go through Portainer's API. Only *updating Portainer itself*
> needs the socket (and Portainer's compose directory mounted read-only).

---

[← Back to README](../README.md) · [Installation](installation.md) · [Usage](usage.md) · [Configuration](configuration.md) · [Security](security.md) · [API](api.md)
