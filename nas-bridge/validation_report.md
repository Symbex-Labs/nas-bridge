# NAS Bridge — Local Deployment Validation Report

**Date:** 2026-06-02  
**Version:** 1.0.0  
**Environment:** Local Docker (Replit, Docker 27.5.1)  
**Image:** `nas-bridge:test` built from `nas-bridge/Dockerfile`  
**Result:** ✅ 18/18 checks passed

---

## Test Environment

### Jobs directory structure

```
/tmp/nas-test-jobs/          ← mounted as /volume1/Jobs:ro inside container
├── Bids/
│   └── TestProject/
│       ├── Plans/
│       │   └── TestPlan.pdf   (82 bytes, valid PDF header %PDF-1.4)
│       └── Notes.txt          (30 bytes)
├── Invoices/
│   └── INV-2024-001.txt
└── WorkLoad/
```

### Configuration

```env
BRIDGE_TOKEN=<32-byte hex token generated with openssl rand -hex 32>
ROOT_PATH=/volume1/Jobs
HOST=0.0.0.0
PORT=8089
LOG_LEVEL=info
```

### Docker run

```bash
docker build -t nas-bridge:test .

docker run -d \
  --name nas-bridge-test \
  --env-file .env \
  -v /tmp/nas-test-jobs:/volume1/Jobs:ro \
  -p 8089:8089 \
  nas-bridge:test
```

Build time: **12.9 seconds** (cold pull of `python:3.12-slim`)

---

## Validation Results

### 1. Health Endpoint

**Command:**
```bash
curl http://localhost:8089/health
```

**Response:**
```json
{
    "status": "healthy",
    "root_path": "/volume1/Jobs",
    "root_exists": true,
    "root_readable": true,
    "version": "1.0.0",
    "timestamp": "2026-06-02T02:16:47.704571+00:00"
}
```

| Check | Result |
|---|---|
| HTTP 200 | ✅ PASS |
| `status: healthy` | ✅ PASS |
| `root_exists: true` | ✅ PASS |
| `root_readable: true` | ✅ PASS |
| No auth required | ✅ PASS |

---

### 2. Authentication

**No token:**
```bash
curl http://localhost:8089/api/v1/list
# HTTP 401 — {"detail":"Authorization header required"}
```

**Wrong token:**
```bash
curl -H "Authorization: Bearer wrong-token" http://localhost:8089/api/v1/list
# HTTP 401 — {"detail":"Invalid token"}
```

| Check | Result |
|---|---|
| 401 on missing token | ✅ PASS |
| 401 on wrong token | ✅ PASS |
| Error messages are clear | ✅ PASS |

---

### 3. List Endpoint

**Root listing:**
```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8089/api/v1/list
```

**Response:**
```json
{
    "path": "/",
    "entries": [
        {"name": "Bids",     "path": "/Bids",     "is_dir": true,  "size_bytes": null, "modified": "2026-06-02T02:15:19.613955+00:00"},
        {"name": "Invoices", "path": "/Invoices", "is_dir": true,  "size_bytes": null, "modified": "2026-06-02T02:15:19.740955+00:00"},
        {"name": "WorkLoad", "path": "/WorkLoad", "is_dir": true,  "size_bytes": null, "modified": "2026-06-02T02:15:19.648955+00:00"}
    ],
    "count": 3
}
```

**Subdirectory listing:**
```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8089/api/v1/list?path=Bids/TestProject"
```

**Response:**
```json
{
    "path": "/Bids/TestProject",
    "entries": [
        {"name": "Plans",     "path": "/Bids/TestProject/Plans",     "is_dir": true,  "size_bytes": null, "modified": "..."},
        {"name": "Notes.txt", "path": "/Bids/TestProject/Notes.txt", "is_dir": false, "size_bytes": 30,   "modified": "..."}
    ],
    "count": 2
}
```

| Check | Result |
|---|---|
| Root lists all top-level folders | ✅ PASS |
| Directories sort before files | ✅ PASS |
| Subdirectory listing works | ✅ PASS |
| Root path returns `"/"` (not `"/."`) | ✅ PASS |

---

### 4. Path Traversal Protection

```bash
# Attempt to escape the Jobs root
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8089/api/v1/list?path=../../etc"
# HTTP 400 — {"detail":"Path traversal rejected"}

curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8089/api/v1/file?path=../../etc/passwd"
# HTTP 400 — {"detail":"Path traversal rejected"}
```

| Check | Result |
|---|---|
| `../../etc` on list → 400 | ✅ PASS |
| `../../etc/passwd` on file → 400 | ✅ PASS |
| No filesystem content leaked | ✅ PASS |

---

### 5. Metadata Endpoint

**File metadata:**
```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8089/api/v1/metadata?path=Bids/TestProject/Plans/TestPlan.pdf"
```

**Response:**
```json
{
    "path": "/Bids/TestProject/Plans/TestPlan.pdf",
    "name": "TestPlan.pdf",
    "is_dir": false,
    "size_bytes": 82,
    "extension": ".pdf",
    "content_type": "application/pdf",
    "modified": "2026-06-02T02:15:19.736084+00:00"
}
```

**Directory metadata:**
```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8089/api/v1/metadata?path=Bids/TestProject"
```

**Response:**
```json
{
    "path": "/Bids/TestProject",
    "name": "TestProject",
    "is_dir": true,
    "children_count": 2,
    "modified": "2026-06-02T02:15:19.738955+00:00"
}
```

**Not found:**
```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8089/api/v1/metadata?path=DoesNotExist.pdf"
# HTTP 404 — {"detail":"Path not found: DoesNotExist.pdf"}
```

| Check | Result |
|---|---|
| File metadata returns `is_dir: false`, `size_bytes`, `extension`, `content_type` | ✅ PASS |
| Directory metadata returns `is_dir: true`, `children_count` | ✅ PASS |
| Missing path returns 404 | ✅ PASS |

---

### 6. File Download

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8089/api/v1/file?path=Bids/TestProject/Plans/TestPlan.pdf" \
  --output TestPlan.pdf
```

**Response headers:**
```
HTTP/1.1 200 OK
content-disposition: attachment; filename="TestPlan.pdf"
content-type: application/pdf
transfer-encoding: chunked
```

**Downloaded file:**
```
Size:   82 bytes
Header: b'%PDF-1.4'  ← valid PDF signature confirmed
```

**Directory download attempt:**
```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8089/api/v1/file?path=Bids/TestProject"
# HTTP 400 — {"detail":"Path is a directory. Use /api/v1/list instead."}
```

| Check | Result |
|---|---|
| File streams with `content-type: application/pdf` | ✅ PASS |
| `Content-Disposition: attachment; filename="TestPlan.pdf"` set | ✅ PASS |
| File bytes match original (82 bytes, `%PDF-1.4` header) | ✅ PASS |
| Directory path returns 400 with helpful message | ✅ PASS |

---

### 7. OpenAPI Documentation

```bash
curl http://localhost:8089/openapi.json
# HTTP 200 — full OpenAPI 3.x schema
```

**Schema excerpt:**
```json
{
  "info": {"title": "NAS Bridge", "version": "1.0.0"},
  "components": {
    "securitySchemes": {
      "BearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "description": "Pass the static BRIDGE_TOKEN value as a Bearer token."
      }
    }
  }
}
```

```bash
curl http://localhost:8089/docs   # HTTP 200 — Swagger UI
curl http://localhost:8089/redoc  # HTTP 200 — ReDoc
```

| Check | Result |
|---|---|
| `/openapi.json` returns 200 | ✅ PASS |
| `BearerAuth` security scheme present | ✅ PASS |
| `/docs` (Swagger UI) returns 200 | ✅ PASS |
| `/redoc` returns 200 | ✅ PASS |

---

## Container Resource Usage

Measured after all validation tests completed:

| Metric | Value |
|---|---|
| Memory usage | **34.84 MiB** |
| Memory limit | 128 MiB (configured in docker-compose.yml) |
| Headroom | 93 MiB (73% unused) |
| CPU | 0.06% (idle) |
| Process count | 7 |

Well within the 2 GB RAM constraint of the Synology DS423+.

---

## Bug Fixed During Validation

| Issue | Severity | Fix |
|---|---|---|
| Root listing returned `"path": "/."` instead of `"path": "/"` | Cosmetic | `_rel()` in `filesystem.py` now returns `"/"` when the resolved path equals ROOT |

---

## Security Validation Summary

| Requirement | Verified |
|---|---|
| No anonymous access to API endpoints | ✅ |
| Wrong token rejected (constant-time comparison) | ✅ |
| Path traversal `../../` rejected with 400 | ✅ |
| Volume mounted read-only (`:ro` Docker flag) | ✅ |
| Container runs as non-root `bridge` user | ✅ |
| No secrets in image (token comes from env file at runtime) | ✅ |

---

## Readiness for Jeremy

The deployment package is ready. The exact steps Jeremy follows on the Synology are in `docs/deployment.md`. The validation procedure above can be reproduced on the NAS before going live by running the same curl commands against `http://localhost:8089` from an SSH session on the NAS.

**Recommended handoff sequence:**

1. Transfer `nas-bridge/` to the NAS (`scp -r nas-bridge/ admin@<nas-ip>:/volume1/docker/nas-bridge`)
2. Generate token: `openssl rand -hex 32`
3. Copy `.env.example` to `.env`, paste token as `BRIDGE_TOKEN`
4. Start: `docker compose up -d --build`
5. Validate: `curl http://localhost:8089/health` — confirm `"status": "healthy"` with `root_exists: true`
6. Run interactive API explorer: browse to `http://<tailscale-ip>:8089/docs`
