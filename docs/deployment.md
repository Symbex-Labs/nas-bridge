# NAS Bridge — Synology Deployment Guide

This guide walks through deploying the NAS Bridge on a Synology DS423+ running DSM 7.x.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| DSM 7.x | Tested on DSM 7.2 |
| Docker (Container Manager) | Install from Package Center |
| Tailscale | Installed on the NAS and on the client machine |
| SSH access | Needed for the compose-based install (recommended) |
| Jobs share | A Synology shared folder (e.g. `/volume1/Jobs`) |

---

## Step 1 — Copy the files to your NAS

Clone the repository directly on the NAS (recommended):

```bash
ssh admin@<nas-ip>
git clone <repository-url> /volume1/docker/nas-bridge
```

Or transfer the files from a local copy using `scp`:

```bash
# Run this from the repository root on your local machine
scp -r . admin@<nas-ip>:/volume1/docker/nas-bridge
```

Or use Synology File Station to upload the directory.

---

## Step 2 — Create your `.env` file

SSH into the NAS:

```bash
ssh admin@<nas-ip>
cd /volume1/docker/nas-bridge
```

Generate a strong token:

```bash
openssl rand -hex 32
```

Copy `.env.example` to `.env` and fill in your token:

```bash
cp .env.example .env
nano .env
```

Minimum required change — replace the placeholder with your generated token:

```
BRIDGE_TOKEN=<paste-your-generated-token-here>
```

Leave all other values at their defaults unless your Jobs share is on a different volume path.

---

## Step 3 — Configure the volume mount

Open `docker-compose.yml` and check the `volumes` section:

```yaml
volumes:
  - /volume1/Jobs:/volume1/Jobs:ro
```

**The left side (`/volume1/Jobs`) is the path on your Synology host.** Adjust it if your Jobs shared folder is on a different volume:

```yaml
volumes:
  - /volume2/Jobs:/volume1/Jobs:ro   # if Jobs lives on volume2
```

The right side (inside the container) must stay `/volume1/Jobs` unless you also change `ROOT_PATH` in your `.env`.

The `:ro` flag mounts the share read-only at the kernel level — even a container compromise cannot write, rename, or delete files.

---

## Step 4 — Build and start the container

### Option A: SSH + Docker Compose (recommended)

```bash
cd /volume1/docker/nas-bridge
docker compose up -d --build
```

Check logs:

```bash
docker compose logs -f
```

### Option B: Synology Container Manager UI

1. Open **Container Manager** in DSM.
2. Go to **Project** → **Create**.
3. Set the path to `/volume1/docker/nas-bridge`.
4. DSM will detect `docker-compose.yml` automatically.
5. Click **Build** then **Start**.

---

## Step 5 — Validate the health endpoint

From the NAS itself:

```bash
curl http://localhost:8080/health
```

Expected healthy response:

```json
{
  "status": "healthy",
  "root_path": "/volume1/Jobs",
  "root_exists": true,
  "root_readable": true,
  "version": "1.0.0",
  "timestamp": "2025-06-02T10:00:00+00:00"
}
```

### Understanding `degraded`

If you see `"status": "degraded"`, check the `root_exists` and `root_readable` fields:

| `root_exists` | `root_readable` | Likely cause |
|---|---|---|
| `false` | `false` | Volume not mounted, or wrong `ROOT_PATH` in `.env` |
| `true` | `false` | Filesystem permissions — the container runs as a non-root `bridge` user; the Jobs share must allow read access to all users |

To fix a permissions issue, ensure the shared folder grants read access to **Everyone** (DSM → File Station → right-click folder → Properties → Permissions → add Everyone with Read permission). The container runs as a non-root system user (`bridge`) for least-privilege operation — this is intentional and expected.

---

## Step 6 — Tailscale network access

After Tailscale is connected on both the NAS and the client machine, the bridge is accessible at your NAS Tailscale IP on port 8080.

Find your NAS Tailscale IP:

```bash
tailscale ip -4
```

Test from the client machine (substitute your token and Tailscale IP):

```bash
TOKEN=your-bridge-token
NAS_IP=100.x.x.x

# Health check (no auth)
curl http://$NAS_IP:8080/health

# List root folder
curl -H "Authorization: Bearer $TOKEN" "http://$NAS_IP:8080/api/v1/list"

# List a specific folder
curl -H "Authorization: Bearer $TOKEN" "http://$NAS_IP:8080/api/v1/list?path=2024"

# Get file metadata
curl -H "Authorization: Bearer $TOKEN" "http://$NAS_IP:8080/api/v1/metadata?path=2024/JobA/plans.pdf"

# Download a file
curl -H "Authorization: Bearer $TOKEN" \
  "http://$NAS_IP:8080/api/v1/file?path=2024/JobA/plans.pdf" \
  --output plans.pdf
```

---

## Step 7 — Open the interactive docs

Point your browser at:

```
http://<NAS-Tailscale-IP>:8080/docs
```

You can use the Swagger UI to test all endpoints interactively.  Click **Authorize** (top right) and enter your `BRIDGE_TOKEN` to make authenticated calls directly from the browser.

---

## Keeping the container running across reboots

The Compose file sets `restart: unless-stopped`, so the container restarts automatically after a NAS reboot **as long as Docker itself starts on boot**.

Enable Docker auto-start:
1. DSM → **Container Manager** → **Preferences** → check **Enable Docker auto-start at system startup**.

---

## Updating the service

```bash
cd /volume1/docker/nas-bridge
git pull                        # if you cloned from a repo
docker compose up -d --build    # rebuild and restart
```

---

## Security checklist

Review these points before putting the service into production use:

- [ ] **Strong token**: `BRIDGE_TOKEN` is at least 32 random hex characters (`openssl rand -hex 32`).
- [ ] **Token is secret**: `.env` is not committed to version control. `.gitignore` excludes it.
- [ ] **Port not exposed to the internet**: Port 8080 is only reachable over Tailscale, not through a public port forward or Synology QuickConnect.
- [ ] **Read-only mount**: `docker-compose.yml` uses the `:ro` flag on the Jobs volume.
- [ ] **DSM firewall**: Synology Firewall blocks port 8080 from non-Tailscale interfaces (Control Panel → Security → Firewall → Edit Rules, allow only the Tailscale subnet `100.64.0.0/10`).
- [ ] **Tailscale ACLs** (optional but recommended): Restrict which Tailscale devices can reach the NAS on port 8080 using a Tailscale ACL policy.
- [ ] **Health monitoring**: Confirm `GET /health` returns `status: healthy` after each NAS reboot or DSM update.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `Connection refused` on port 8080 | Container not running — check `docker compose ps` and `docker compose logs` |
| HTTP 401 on API calls | Token mismatch — verify `BRIDGE_TOKEN` in `.env` matches the value in your request |
| `status: degraded` | Volume not mounted or wrong `ROOT_PATH` — see Step 5 |
| HTTP 400 "Path traversal rejected" | Caller sent a path containing `..` — fix the client |
| Container exits immediately | View logs with `docker compose logs nas-bridge` for the Python exception |
| High memory usage | Single-worker Uvicorn should stay well under 128 MB; if not, check for concurrent large file downloads |
