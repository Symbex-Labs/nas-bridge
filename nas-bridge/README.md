# NAS Bridge

A lightweight read-only HTTP API that runs in Docker on a Synology NAS and exposes a file share over Tailscale to a client application.

## Quick start

```bash
# 1. Create your environment file
cp .env.example .env

# 2. Set a strong token (copy the output into .env as BRIDGE_TOKEN)
openssl rand -hex 32

# 3. Edit .env — at minimum set BRIDGE_TOKEN
nano .env

# 4. Build and start
docker compose up -d --build

# 5. Verify it's healthy
curl http://localhost:8080/health
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `BRIDGE_TOKEN` | **Yes** | — | Static Bearer token for all API requests |
| `ROOT_PATH` | No | `/volume1/Jobs` | Absolute path to the file share inside the container |
| `HOST` | No | `0.0.0.0` | Interface to bind (keep `0.0.0.0` inside Docker) |
| `PORT` | No | `8080` | TCP port the service listens on |
| `LOG_LEVEL` | No | `info` | Uvicorn log level (`debug`/`info`/`warning`/`error`) |

## API reference

All `/api/v1/*` endpoints require:

```
Authorization: Bearer <BRIDGE_TOKEN>
```

Anonymous requests receive **HTTP 401**.  Path traversal attempts (e.g. `../../etc/passwd`) receive **HTTP 400** and never leak files outside the configured root.

### GET /health

No auth required.  Returns liveness status **and** root-mount validation.

```bash
curl http://<tailscale-ip>:8080/health
```

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

`status` is `degraded` when the volume is not mounted or not readable.  HTTP is always 200 — check the body.

---

### GET /api/v1/list?path=

List directory contents.  Leave `path` empty to list the root folder.

```bash
TOKEN=your-bridge-token
HOST=100.x.x.x   # your NAS Tailscale IP

curl -H "Authorization: Bearer $TOKEN" \
  "http://$HOST:8080/api/v1/list?path=2024/JobA"
```

```json
{
  "path": "/2024/JobA",
  "entries": [
    { "name": "Drawings", "path": "/2024/JobA/Drawings", "is_dir": true, "size_bytes": null, "modified": "2024-05-01T09:00:00+00:00" },
    { "name": "plans.pdf", "path": "/2024/JobA/plans.pdf", "is_dir": false, "size_bytes": 2048576, "modified": "2024-04-30T14:22:00+00:00" }
  ],
  "count": 2
}
```

---

### GET /api/v1/metadata?path=

Metadata for a file or directory.

```bash
# File
curl -H "Authorization: Bearer $TOKEN" \
  "http://$HOST:8080/api/v1/metadata?path=2024/JobA/plans.pdf"
```

```json
{
  "path": "/2024/JobA/plans.pdf",
  "name": "plans.pdf",
  "is_dir": false,
  "size_bytes": 2048576,
  "extension": ".pdf",
  "content_type": "application/pdf",
  "modified": "2024-04-30T14:22:00+00:00"
}
```

```bash
# Directory
curl -H "Authorization: Bearer $TOKEN" \
  "http://$HOST:8080/api/v1/metadata?path=2024/JobA"
```

```json
{
  "path": "/2024/JobA",
  "name": "JobA",
  "is_dir": true,
  "children_count": 5,
  "modified": "2024-05-01T09:00:00+00:00"
}
```

---

### GET /api/v1/file?path=

Stream raw file bytes.  The response `Content-Type` is derived from the file extension.

```bash
# Download a file
curl -H "Authorization: Bearer $TOKEN" \
  "http://$HOST:8080/api/v1/file?path=2024/JobA/plans.pdf" \
  --output plans.pdf
```

---

## OpenAPI documentation

Once running, open in your browser:

| URL | Description |
|---|---|
| `http://<host>:8080/docs` | Swagger UI — interactive endpoint explorer |
| `http://<host>:8080/redoc` | ReDoc — readable reference |
| `http://<host>:8080/openapi.json` | Machine-readable OpenAPI schema |

---

## Full deployment guide

See [`docs/deployment.md`](docs/deployment.md) for Synology-specific instructions including Docker Compose setup via DSM or SSH, Tailscale network requirements, volume mount configuration, and a security checklist.
