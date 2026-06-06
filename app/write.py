"""Write Zone — controlled file writes inside ROOT/TakeoffAssistFiles/<zone>/Bids/.

Phase 1 (NAS-W004): adds real file-write capability to the bridge.
write_test.py (smoke test) and mkdir.py (directory creation) remain separate.

Phase 3 (NAS-W004): after every successful file write, the workspace manifest
(takeoffassist_manifest.json) is updated via manifest.register_file().  Manifest
updates are failure-safe — a manifest write failure does NOT block or roll back
the file write.

Approved write zone:
    ROOT / "TakeoffAssistFiles" / <zone> / "Bids" / <workspace_path> / <filename>

Hard constraints (code-level, independent of filesystem permissions):
    - zone must be "dev" or "prod"
    - workspace_path must resolve inside TakeoffAssistFiles/<zone>/Bids/
    - filename must contain no path separators or null bytes
    - file extension must be on the allowlist
    - file size must not exceed MAX_FILE_SIZE_BYTES
    - path traversal (..) is always rejected
    - target parent directory must already exist (workspace must be created first)

Duplicate detection:
    - If an identical file (same SHA-256) already exists, the write is skipped
      and already_existed=True is returned.  The manifest is NOT updated for
      duplicates — the existing entry already represents that file on disk.
    - If the same filename exists but with different content, the new file is
      written with a UTC timestamp suffix (no silent overwrites); the manifest
      IS updated with the final (timestamp-renamed) filename.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, status

from .config import settings
from .manifest import MANIFEST_FILENAME, register_file as _register_manifest_file

logger = logging.getLogger(__name__)

ROOT = Path(settings.root_path).resolve()
WRITE_ZONE_NAME = "TakeoffAssistFiles"
WRITE_ZONE = ROOT / WRITE_ZONE_NAME

_ALLOWED_ZONES: frozenset[str] = frozenset({"dev", "prod"})

_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv",
    ".png", ".jpg", ".jpeg", ".tiff", ".tif",
    ".txt", ".rtf", ".zip",
})

MAX_FILE_SIZE_BYTES: int = settings.max_file_size_bytes

_SAFE_FILENAME = re.compile(r"^[^\x00/\\]{1,255}$")


# ── Validators ────────────────────────────────────────────────────────────────

def _validate_zone(zone: str) -> None:
    if zone not in _ALLOWED_ZONES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid zone '{zone}' — must be one of: {sorted(_ALLOWED_ZONES)}",
        )


def _validate_workspace_path(workspace_path: str) -> None:
    if not workspace_path or not workspace_path.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workspace_path must not be empty",
        )
    if workspace_path.startswith("/") or workspace_path.startswith("\\"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workspace_path must be relative — no leading slash",
        )
    parts = [p for p in re.split(r"[/\\]", workspace_path) if p]
    if ".." in parts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path traversal rejected",
        )
    for part in parts:
        if "\x00" in part:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Null byte in workspace_path component rejected",
            )


def _validate_filename(filename: str) -> None:
    if not filename or not filename.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="filename must not be empty",
        )
    if not _SAFE_FILENAME.match(filename):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="filename must not contain path separators (/ \\) or null bytes",
        )
    if ".." in filename.split("/") or ".." in filename.split("\\"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path traversal in filename rejected",
        )


def _validate_extension(filename: str) -> None:
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"File extension '{ext}' is not permitted. "
                f"Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}"
            ),
        )


def _validate_size(content: bytes) -> None:
    if len(content) > MAX_FILE_SIZE_BYTES:
        mb = MAX_FILE_SIZE_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum size of {mb} MB ({MAX_FILE_SIZE_BYTES} bytes)",
        )


def _resolve_write_target(zone: str, workspace_path: str, filename: str) -> Path:
    """Resolve and zone-containment-check the full write target path.

    Computes: WRITE_ZONE / zone / "Bids" / workspace_path / filename
    Verifies: resolved path is inside WRITE_ZONE.
    """
    candidate = (WRITE_ZONE / zone / "Bids" / workspace_path / filename).resolve()
    try:
        candidate.relative_to(WRITE_ZONE)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Resolved path escapes the TakeoffAssistFiles write zone",
        )
    return candidate


# ── Core write function ───────────────────────────────────────────────────────

def write_file(
    zone: str,
    workspace_path: str,
    filename: str,
    content: bytes,
    *,
    source: str = "MANUAL",
    uploaded_by: str = "system",
    object_storage_key: Optional[str] = None,
) -> dict:
    """Write content to WRITE_ZONE/<zone>/Bids/<workspace_path>/<filename>.

    All security controls are applied before any filesystem operation.

    If an identical file already exists (same SHA-256), the write is skipped,
    ``already_existed=True`` is returned, and the manifest is NOT updated —
    the existing entry already represents that file on disk.

    If the same filename exists with different content, the new file is written
    with a UTC timestamp suffix (e.g. ``plans_20260605T120000.pdf``) — the
    original is never silently overwritten.  The manifest entry records the
    final (possibly timestamp-renamed) filename.

    After a successful write (``already_existed=False``), the workspace manifest
    (takeoffassist_manifest.json) is updated via manifest.register_file().
    Manifest update is failure-safe — any error is logged and does NOT affect
    the file-write result.

    Args:
        zone:               Target zone — ``"dev"`` or ``"prod"``.
        workspace_path:     Relative path within the zone's Bids dir,
                            e.g. ``"Project_Name/Plans"``.
        filename:           Simple filename with an allowed extension,
                            e.g. ``"architectural_plans.pdf"``.
        content:            Raw bytes to write.
        source:             Upload-source label — MANUAL, EMAIL, PLANHUB,
                            BUILDINGCONNECTED, PROCORE, or GENERATED.
                            Defaults to ``"MANUAL"``.
        uploaded_by:        Audit identity string, e.g. ``"user:alice"``.
                            Defaults to ``"system"``.
        object_storage_key: Object Storage key for the archived copy, or None.

    Returns::

        {
            "success":          True,
            "path":             str,   # path relative to Jobs root
            "size_bytes":       int,
            "sha256":           str,   # hex SHA-256 digest of content written
            "already_existed":  bool,  # True when identical file was already present
            "manifest_updated": bool,  # True when manifest was updated successfully
        }

    Raises:
        HTTP 400 — invalid zone, path traversal, bad filename, disallowed extension,
                   or target workspace does not exist
        HTTP 403 — resolved path escapes TakeoffAssistFiles write zone
        HTTP 413 — content exceeds MAX_FILE_SIZE_BYTES
        HTTP 503 — filesystem permission denied
        HTTP 500 — unexpected OS error
    """
    _validate_zone(zone)
    _validate_workspace_path(workspace_path)
    _validate_filename(filename)
    _validate_extension(filename)
    _validate_size(content)

    target = _resolve_write_target(zone, workspace_path, filename)
    sha256 = hashlib.sha256(content).hexdigest()

    # ── Duplicate / collision detection ────────────────────────────────────────
    if target.exists() and target.is_file():
        existing_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        if existing_sha256 == sha256:
            try:
                rel = "/" + str(target.relative_to(ROOT))
            except ValueError:
                rel = str(target)
            logger.info("write_file: identical file already exists — skipping write: %s", rel)
            return {
                "success":          True,
                "path":             rel,
                "size_bytes":       len(content),
                "sha256":           sha256,
                "already_existed":  True,
                "manifest_updated": False,
            }
        # Different content at same filename — timestamp-rename the new file
        stem = Path(filename).stem
        ext = Path(filename).suffix
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        new_filename = f"{stem}_{ts}{ext}"
        logger.info(
            "write_file: different content at '%s' — writing as '%s'",
            filename, new_filename,
        )
        target = _resolve_write_target(zone, workspace_path, new_filename)
        final_filename = new_filename
    else:
        final_filename = filename

    # ── Workspace must exist ──────────────────────────────────────────────────
    if not target.parent.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Target workspace directory does not exist: "
                f"TakeoffAssistFiles/{zone}/Bids/{workspace_path}. "
                "Create the workspace first using POST /api/v1/mkdir."
            ),
        )

    # ── Write file ────────────────────────────────────────────────────────────
    try:
        target.write_bytes(content)
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Write failed: permission denied — check NAS mount permissions.",
        )
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Write failed: {exc}",
        )

    try:
        rel = "/" + str(target.relative_to(ROOT))
    except ValueError:
        rel = str(target)

    logger.info(
        "write_file: wrote %d bytes → %s (sha256=%s)",
        len(content), rel, sha256[:16],
    )

    # ── Manifest registration (failure-safe) ──────────────────────────────────
    manifest_updated = False
    parts = [p for p in re.split(r"[/\\]", workspace_path) if p]
    if parts:
        project_folder = parts[0]
        subdir_name = parts[-1] if len(parts) > 1 else parts[0]
        manifest_path = WRITE_ZONE / zone / "Bids" / project_folder / MANIFEST_FILENAME
        try:
            reg = _register_manifest_file(
                manifest_path,
                filename=final_filename,
                subdir=subdir_name,
                size_bytes=len(content),
                sha256=sha256,
                source=source,
                uploaded_by=uploaded_by,
                object_storage_key=object_storage_key,
            )
            manifest_updated = reg["manifest_updated"]
        except Exception as exc:
            logger.warning(
                "Manifest registration failed (file write unaffected): %s", exc,
            )

    return {
        "success":          True,
        "path":             rel,
        "size_bytes":       len(content),
        "sha256":           sha256,
        "already_existed":  False,
        "manifest_updated": manifest_updated,
    }
