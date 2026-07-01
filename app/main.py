"""NAS Bridge — HTTP API for the Synology Jobs share.

Exposes eight endpoints:
  GET  /health                 — no auth; liveness + root-mount validation
  GET  /api/v1/list            — directory listing (Bearer auth required)
  GET  /api/v1/metadata        — file or directory metadata (Bearer auth required)
  GET  /api/v1/file            — raw file bytes, streamed (Bearer auth required)
  POST /api/v1/write-test      — smoke-test write to /Jobs/TakeoffAssistFiles only
  POST /api/v1/mkdir           — controlled directory creation in /Jobs/TakeoffAssistFiles only
  POST /api/v1/write           — controlled file write into a workspace (NAS-W004 Phase 1)
  GET  /docs | /redoc | /openapi.json — OpenAPI documentation

Write zone:
  Writes are only permitted within /Jobs/TakeoffAssistFiles/.
  All other paths (Bids, Invoices, WorkLoad, ...) are rejected at the code
  level regardless of filesystem permissions.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Annotated, Optional, Union

from fastapi import Depends, FastAPI, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer

from .auth import verify_bearer
from .config import settings
from .filesystem import list_directory, get_metadata, open_file
from .models import (
    HealthResponse,
    ListResponse,
    FileMetadata,
    DirectoryMetadata,
    ErrorResponse,
    WriteTestRequest,
    WriteTestResponse,
    MkdirRequest,
    MkdirResponse,
    ProjectFolderRequest,
    ProjectFolderResponse,
    WriteFileResponse,
)
from .write_test import write_and_verify
from .mkdir import make_dir, create_project_skeleton
from .write import write_file as _write_file

logger = logging.getLogger(__name__)

app = FastAPI(
    title="NAS Bridge",
    version=settings.version,
    summary="HTTP API for the Synology DS423+ Jobs share over Tailscale.",
    description=(
        "Internal server-to-server bridge for the NAS Jobs share.  "
        "**This service is not for direct client use.**  "
        "All `/api/v1/*` endpoints require `Authorization: Bearer <BRIDGE_TOKEN>`.  "
        "The `/health` endpoint is unauthenticated.  "
        "Read endpoints (list, metadata, file) are filesystem-scoped to `/Jobs/`; "
        "the calling API server (TakeoffAssist) enforces an additional application-level "
        "restriction — only paths under `TakeoffAssistFiles/` may be proxied to end users.  "
        "Writes are permitted **only** within `/Jobs/TakeoffAssistFiles/`; "
        "all other subtrees are rejected at the code level regardless of filesystem permissions."
    ),
    openapi_tags=[
        {"name": "health", "description": "Liveness and volume-mount check."},
        {"name": "files", "description": "Read files and directories from the Jobs share. Access is scoped to TakeoffAssistFiles/ by the calling API server."},
        {"name": "write", "description": "Controlled write operations (TakeoffAssistFiles zone only). Includes file writes and directory creation."},
    ],
)


def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(
        title=app.title,
        version=app.version,
        summary=app.summary,
        description=app.description,
        tags=app.openapi_tags,
        routes=app.routes,
    )
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "description": "Pass the static `BRIDGE_TOKEN` value as a Bearer token.",
    }
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi


# ── Health ────────────────────────────────────────────────────────────────────

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["health"],
    summary="Service health and volume-mount status",
    description=(
        "No authentication required.  Returns `status: healthy` when the root "
        "volume is mounted and readable, `status: degraded` otherwise.  "
        "Always returns HTTP 200 so load-balancers and uptime monitors treat "
        "the service as up even in degraded mode — inspect the body for detail."
    ),
    responses={
        200: {
            "description": "Health payload (check `status` field for `degraded`)",
            "model": HealthResponse,
        }
    },
)
def health() -> HealthResponse:
    root = settings.root_path
    exists = os.path.exists(root)
    readable = os.access(root, os.R_OK) if exists else False
    return HealthResponse(
        status="healthy" if (exists and readable) else "degraded",
        root_path=root,
        root_exists=exists,
        root_readable=readable,
        version=settings.version,
        timestamp=datetime.now(timezone.utc).isoformat(),
        # PACKAGE-CURATION-006B — advertise the endpoints callers can rely on so
        # the API/UI can detect a pre-mkdir-path bridge and prompt a redeploy.
        capabilities={"mkdir_path": True},
    )


# ── API v1 ────────────────────────────────────────────────────────────────────

_AUTH = Depends(verify_bearer)
_BEARER_SECURITY = {"security": [{"BearerAuth": []}]}


@app.get(
    "/api/v1/list",
    response_model=ListResponse,
    tags=["files"],
    summary="List directory contents",
    description=(
        "Returns the immediate children of the directory at `path`.  "
        "Entries are sorted: directories first, then files, both alphabetically.  "
        "**Note:** This endpoint accepts any path under the `/Jobs` filesystem root, "
        "but the calling API server restricts proxied access to `TakeoffAssistFiles/` only.  "
        "Direct bridge callers are responsible for enforcing appropriate path scope."
    ),
    responses={
        200: {"description": "Directory listing", "model": ListResponse},
        400: {"description": "Path is a file, or path traversal rejected", "model": ErrorResponse},
        401: {"description": "Missing or invalid Bearer token", "model": ErrorResponse},
        403: {"description": "Permission denied on the filesystem", "model": ErrorResponse},
        404: {"description": "Path does not exist", "model": ErrorResponse},
    },
    openapi_extra=_BEARER_SECURITY,
    dependencies=[_AUTH],
)
def list_dir(
    path: Annotated[str, Query(description="Relative path from the Jobs root, e.g. `2024/JobA`")] = "",
) -> ListResponse:
    return ListResponse(**list_directory(path))


@app.get(
    "/api/v1/metadata",
    response_model=Union[FileMetadata, DirectoryMetadata],
    tags=["files"],
    summary="File or directory metadata",
    description=(
        "Returns metadata for the item at `path`.  "
        "For **files**: `size_bytes`, `extension`, `content_type`, `modified`.  "
        "For **directories**: `children_count`, `modified`."
    ),
    responses={
        200: {
            "description": "File or directory metadata",
            "content": {
                "application/json": {
                    "examples": {
                        "file": {
                            "summary": "File",
                            "value": {
                                "path": "/2024/JobA/plans.pdf",
                                "name": "plans.pdf",
                                "is_dir": False,
                                "size_bytes": 2048576,
                                "extension": ".pdf",
                                "content_type": "application/pdf",
                                "modified": "2024-06-01T12:00:00+00:00",
                            },
                        },
                        "directory": {
                            "summary": "Directory",
                            "value": {
                                "path": "/2024/JobA",
                                "name": "JobA",
                                "is_dir": True,
                                "children_count": 5,
                                "modified": "2024-06-01T12:00:00+00:00",
                            },
                        },
                    }
                }
            },
        },
        400: {"description": "Path traversal rejected or path is a special file", "model": ErrorResponse},
        401: {"description": "Missing or invalid Bearer token", "model": ErrorResponse},
        404: {"description": "Path does not exist", "model": ErrorResponse},
    },
    openapi_extra=_BEARER_SECURITY,
    dependencies=[_AUTH],
)
def metadata(
    path: Annotated[str, Query(description="Relative path from the Jobs root")] = "",
) -> Union[FileMetadata, DirectoryMetadata]:
    data = get_metadata(path)
    if data["is_dir"]:
        return DirectoryMetadata(**data)
    return FileMetadata(**data)


_CHUNK = 64 * 1024  # 64 KB streaming chunk


def _iter_file(file_path, chunk_size: int = _CHUNK):
    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            yield chunk


@app.get(
    "/api/v1/file",
    tags=["files"],
    summary="Stream raw file bytes",
    description=(
        "Streams the raw bytes of the file at `path` with the correct `Content-Type` "
        "header derived from the file extension.  Use this endpoint to download or "
        "proxy files to the browser.  Requesting a directory returns HTTP 400."
    ),
    responses={
        200: {"description": "Raw file bytes with correct Content-Type"},
        400: {"description": "Path is a directory, or path traversal rejected", "model": ErrorResponse},
        401: {"description": "Missing or invalid Bearer token", "model": ErrorResponse},
        404: {"description": "Path does not exist", "model": ErrorResponse},
    },
    openapi_extra=_BEARER_SECURITY,
    dependencies=[_AUTH],
)
def stream_file(
    path: Annotated[str, Query(description="Relative path to the file from the Jobs root")] = "",
) -> StreamingResponse:
    resolved, content_type = open_file(path)
    return StreamingResponse(
        _iter_file(resolved),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{resolved.name}"'},
    )


# ── Write zone ────────────────────────────────────────────────────────────────

@app.post(
    "/api/v1/write-test",
    response_model=WriteTestResponse,
    tags=["write"],
    summary="Write a file to TakeoffAssistFiles/ and verify read-back",
    description=(
        "Writes *contents* to `/Jobs/TakeoffAssistFiles/<filename>`, reads the "
        "file back immediately, and verifies the content matches.  "
        "**This is the only write-capable endpoint in the bridge.**  "
        "\n\n"
        "Hard constraints enforced in code (independent of filesystem permissions):\n"
        "- Target must be within `/Jobs/TakeoffAssistFiles/`\n"
        "- Writes to `Bids/`, `Invoices/`, `WorkLoad/` are rejected with HTTP 403\n"
        "- Path traversal (`../`) is rejected with HTTP 400\n"
        "- Absolute paths outside the write zone are rejected with HTTP 403\n"
    ),
    responses={
        200: {"description": "File written and verified", "model": WriteTestResponse},
        400: {"description": "Invalid filename or path traversal attempt", "model": ErrorResponse},
        401: {"description": "Missing or invalid Bearer token", "model": ErrorResponse},
        403: {"description": "Write to protected zone or path outside write zone", "model": ErrorResponse},
        503: {"description": "Write zone not writable — check Docker mount", "model": ErrorResponse},
    },
    openapi_extra=_BEARER_SECURITY,
    dependencies=[_AUTH],
)
def write_test(body: WriteTestRequest) -> WriteTestResponse:
    result = write_and_verify(body.filename, body.contents)
    return WriteTestResponse(**result)


@app.post(
    "/api/v1/write",
    response_model=WriteFileResponse,
    tags=["write"],
    summary="Write a file into an existing workspace directory",
    description=(
        "Writes a file into `TakeoffAssistFiles/<zone>/Bids/<workspace_path>/<filename>`.  "
        "The target workspace directory **must already exist** — create it first with "
        "`POST /api/v1/mkdir`.  "
        "\n\n"
        "**Security controls (code-level, independent of filesystem permissions):**\n"
        "- `zone` must be `dev` or `prod`\n"
        "- `workspace_path` must be relative — no `../`, no leading `/`\n"
        "- `filename` must be a simple name with no path separators\n"
        "- File extension must be on the allowlist "
        "(`.pdf .docx .doc .xlsx .xls .csv .png .jpg .jpeg .tiff .tif .txt .rtf .zip`)\n"
        "- File must not exceed `MAX_FILE_SIZE_BYTES` (default 200 MB)\n"
        "- Resolved path must remain inside `TakeoffAssistFiles/`\n"
        "\n\n"
        "**Duplicate detection:** If an identical file (same SHA-256) already exists, "
        "the write is skipped and `already_existed: true` is returned — no error.  "
        "If the same filename exists with different content, the new file is written "
        "with a UTC timestamp suffix — the original is never silently overwritten."
    ),
    responses={
        200: {"description": "File written (or identical file already existed)", "model": WriteFileResponse},
        400: {"description": "Invalid zone, path traversal, bad filename, disallowed extension, or workspace not found", "model": ErrorResponse},
        401: {"description": "Missing or invalid Bearer token", "model": ErrorResponse},
        403: {"description": "Resolved path escapes TakeoffAssistFiles write zone", "model": ErrorResponse},
        413: {"description": "File exceeds maximum size limit", "model": ErrorResponse},
        503: {"description": "Write zone not writable — check Docker mount", "model": ErrorResponse},
    },
    openapi_extra=_BEARER_SECURITY,
    dependencies=[_AUTH],
)
async def write_file(
    zone: Annotated[str, Form(description="Target zone — 'dev' or 'prod'")],
    workspace_path: Annotated[str, Form(description="Relative path within zone/Bids/, e.g. 'Project_Name/Plans'")],
    filename: Annotated[str, Form(description="Simple filename with allowed extension, e.g. 'plans.pdf'")],
    file: Annotated[UploadFile, File(description="Binary file content")],
    source: Annotated[str, Form(description="Upload source label — MANUAL | EMAIL | PLANHUB | BUILDINGCONNECTED | PROCORE | GENERATED")] = "MANUAL",
    uploaded_by: Annotated[str, Form(description="Audit identity string, e.g. 'user:alice' or 'system:acquisition'")] = "system",
    object_storage_key: Annotated[Optional[str], Form(description="Object Storage key for the archived copy (optional)")] = None,
) -> WriteFileResponse:
    content = await file.read()
    result = _write_file(
        zone, workspace_path, filename, content,
        source=source,
        uploaded_by=uploaded_by,
        object_storage_key=object_storage_key,
    )
    return WriteFileResponse(**result)


@app.post(
    "/api/v1/mkdir",
    response_model=ProjectFolderResponse,
    tags=["write"],
    summary="Create a project folder skeleton in TakeoffAssistFiles/<zone>/Bids/",
    description=(
        "Creates three directories for a new project inside the TakeoffAssistFiles write zone:\n\n"
        "```\n"
        "TakeoffAssistFiles/<zone>/Bids/<normalised_name>/\n"
        "TakeoffAssistFiles/<zone>/Bids/<normalised_name>/Plans/\n"
        "TakeoffAssistFiles/<zone>/Bids/<normalised_name>/Bids/\n"
        "```\n\n"
        "The `zone` field must be `'dev'` (Replit development) or `'prod'` (production pilot).  "
        "It is set by the API server's `NAS_ZONE` environment variable — never by the client.  "
        "The call is **idempotent** — if any directory already exists, it is reported as "
        "`already_existed: true` and no error is raised.  "
        "\n\n"
        "Hard constraints (code-level, independent of filesystem permissions):\n"
        "- Target must be within `TakeoffAssistFiles/`\n"
        "- `zone` must be `dev` or `prod` — any other value is rejected with HTTP 400\n"
        "- Path traversal (`../`) is rejected with HTTP 400\n"
        "- Absolute paths are rejected with HTTP 400\n"
        "- Empty names are rejected with HTTP 400\n"
    ),
    responses={
        200: {"description": "Folder skeleton created or verified", "model": ProjectFolderResponse},
        400: {"description": "Invalid name, empty name, or path traversal attempt", "model": ErrorResponse},
        401: {"description": "Missing or invalid Bearer token", "model": ErrorResponse},
        403: {"description": "Targets a protected production zone or path escapes write zone", "model": ErrorResponse},
        503: {"description": "Write zone not writable — check Docker mount", "model": ErrorResponse},
    },
    openapi_extra=_BEARER_SECURITY,
    dependencies=[_AUTH],
)
def mkdir_project(body: ProjectFolderRequest) -> ProjectFolderResponse:
    result = create_project_skeleton(
        body.folder_name,
        zone=body.zone,
        bid_id=body.bid_id,
        project_name=body.project_name,
    )
    return ProjectFolderResponse(**result)


@app.post(
    "/api/v1/mkdir-path",
    response_model=MkdirResponse,
    tags=["write"],
    summary="Create an arbitrary nested directory inside TakeoffAssistFiles/",
    description=(
        "Creates a single nested directory (and any missing parents) at "
        "`rel_path`, resolved relative to the `TakeoffAssistFiles/` write zone:\n\n"
        "```\n"
        "TakeoffAssistFiles/<rel_path>/\n"
        "```\n\n"
        "Unlike `POST /api/v1/mkdir` (which creates the fixed project skeleton), this "
        "endpoint creates an operator-independent nested path such as "
        "`<zone>/Bids/<Project>/Generated/TakeoffPackages`.  The calling API server is "
        "responsible for composing a correct `rel_path` (it always prefixes the zone).\n\n"
        "The call is **idempotent** — parents are created with `parents=True, exist_ok=True`, "
        "so an existing directory is reported as `already_existed: true` and no error is raised.\n\n"
        "Hard constraints (code-level, independent of filesystem permissions):\n"
        "- Target must resolve within `TakeoffAssistFiles/`\n"
        "- Path traversal (`../`) is rejected with HTTP 400\n"
        "- Absolute paths and null bytes are rejected with HTTP 400\n"
        "- A file (not directory) already at the target is rejected with HTTP 409\n"
    ),
    responses={
        200: {"description": "Directory created or verified", "model": MkdirResponse},
        400: {"description": "Invalid path, absolute path, or traversal attempt", "model": ErrorResponse},
        401: {"description": "Missing or invalid Bearer token", "model": ErrorResponse},
        403: {"description": "Resolved path escapes the write zone", "model": ErrorResponse},
        409: {"description": "A file already exists at the target path", "model": ErrorResponse},
        503: {"description": "Write zone not writable — check Docker mount", "model": ErrorResponse},
    },
    openapi_extra=_BEARER_SECURITY,
    dependencies=[_AUTH],
)
def mkdir_path(body: MkdirRequest) -> MkdirResponse:
    result = make_dir(body.rel_path)
    return MkdirResponse(**result)
