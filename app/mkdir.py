"""Mkdir Zone — controlled directory creation inside ROOT/TakeoffAssistFiles.

This is the ONLY module that creates directories.  filesystem.py remains
read-only.  write_test.py remains file-only.

Approved write zone:
    ROOT / "TakeoffAssistFiles"   →   /volume1/Jobs/TakeoffAssistFiles

Hard-denied production zones (code-level, independent of filesystem perms):
    /Jobs/Bids, /Jobs/Invoices, /Jobs/WorkLoad

Normalization rules (must match API server normalize_folder_name()):
    - Trim whitespace
    - Replace spaces with underscores
    - Strip SMB/macOS/Windows-invalid chars: \\ / : * ? " < > | NUL
    - Collapse repeated underscores
    - Strip leading/trailing dots and underscores
    - Maximum 80 characters
    - Reject if empty after normalization
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, status

from .config import settings

logger = logging.getLogger(__name__)

ROOT = Path(settings.root_path).resolve()

WRITE_ZONE_NAME = "TakeoffAssistFiles"
WRITE_ZONE = ROOT / WRITE_ZONE_NAME

_PROTECTED_TOP = frozenset({"Bids", "Invoices", "WorkLoad"})
_ALLOWED_ZONES = frozenset({"dev", "prod"})

_INVALID_CHARS = re.compile(r'[\x00\\/:"*?<>|]')
_COLLAPSE_UNDERSCORES = re.compile(r"_+")
_STRIP_EDGES = re.compile(r"^[._]+|[._]+$")

_MAX_LEN = 80


def normalize_folder_name(raw: str) -> str:
    """Return a NAS-safe folder name derived from *raw*.

    Raises HTTP 400 if the result would be empty.
    """
    s = raw.strip()
    s = _INVALID_CHARS.sub("_", s)
    s = s.replace(" ", "_")
    s = _COLLAPSE_UNDERSCORES.sub("_", s)
    s = _STRIP_EDGES.sub("", s)
    s = s[:_MAX_LEN]
    s = _STRIP_EDGES.sub("", s)
    if not s:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Folder name is empty after normalization — provide a non-empty project name",
        )
    return s


def _validate_rel_path(rel_path: str) -> None:
    """Validate a path segment that will live inside WRITE_ZONE.

    Allowed: simple names or one level of sub-path (e.g. "Bids/FolderName/Plans").
    Rejected: absolute paths, dotdot traversal, null bytes, backslashes.
    Also rejected: paths whose top-level component matches a protected production zone.
    """
    if not rel_path or not rel_path.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path must not be empty",
        )

    if rel_path.startswith("/") or rel_path.startswith("\\"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Absolute paths are not accepted — provide a path relative to TakeoffAssistFiles/",
        )

    parts = [p for p in re.split(r"[/\\]", rel_path) if p]

    if ".." in parts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path traversal rejected",
        )

    for part in parts:
        if "\x00" in part:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Null byte in path component rejected",
            )

    # NOTE: we do NOT check _PROTECTED_TOP here because this path is always
    # resolved relative to WRITE_ZONE (TakeoffAssistFiles/), so
    # "Bids/JobName" → TakeoffAssistFiles/Bids/JobName (safe).
    # The containment check in make_dir() (candidate.relative_to(WRITE_ZONE))
    # is the real guard against escaping the write zone.


def make_dir(rel_path: str) -> dict:
    """Create WRITE_ZONE/<rel_path> and return a result dict.

    Idempotent — returns already_existed=True if the directory is already there.

    Args:
        rel_path: Path relative to TakeoffAssistFiles/ (e.g. "Bids/JobName/Plans").

    Returns:
        {
            "path": "/TakeoffAssistFiles/Bids/JobName/Plans",
            "created": True,
            "already_existed": False,
        }

    Raises:
        HTTP 400 — invalid path or traversal attempt
        HTTP 403 — targets a protected production zone or escapes the write zone
        HTTP 503 — filesystem not writable
        HTTP 500 — unexpected OS error
    """
    _validate_rel_path(rel_path)

    target = (WRITE_ZONE / rel_path).resolve()

    try:
        target.relative_to(WRITE_ZONE)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Resolved path escapes the TakeoffAssistFiles write zone",
        )

    already_existed = target.exists()
    if already_existed and not target.is_dir():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A file (not a directory) already exists at {rel_path}",
        )

    try:
        target.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "mkdir failed: permission denied. "
                "Ensure /volume1/Jobs/TakeoffAssistFiles is mounted rw in docker-compose.yml."
            ),
        )
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"mkdir failed: {exc}",
        )

    try:
        rel = "/" + str(target.relative_to(ROOT))
    except ValueError:
        rel = str(target)

    return {
        "path": rel,
        "created": not already_existed,
        "already_existed": already_existed,
    }


_MANIFEST_FILENAME = "takeoffassist_manifest.json"
_MANIFEST_SCHEMA_VERSION = "2.0"

_ALL_SUBDIRS: tuple[str, ...] = (
    "Plans", "Specifications", "Bids", "Attachments", "Generated",
)


def create_manifest(
    project_root: Path,
    *,
    zone: str,
    normalized_name: str,
    project_name: Optional[str],
    bid_id: Optional[int],
) -> dict:
    """Write takeoffassist_manifest.json (schema v2.0) into *project_root*.

    Idempotent — if the manifest file already exists, it is not overwritten and
    this function returns manifest_written=False, manifest_path=<existing path>.

    Failure-safe — any OS or serialisation error is caught, logged, and returned
    as manifest_written=False without propagating an exception.  Folder creation
    must never be blocked by a manifest write failure.

    Schema v2.0 adds over v1.0:
        files        — empty list; populated by write.write_file() via manifest.register_file()
        file_counts  — per-subdir integer counters initialised to 0
        last_updated — ISO-8601 UTC timestamp refreshed on every manifest write

    Args:
        project_root:    Absolute Path to the project root directory.
        zone:            Zone the workspace was created in ('dev' or 'prod').
        normalized_name: Normalised folder name used on disk.
        project_name:    Human-readable project name before normalisation, or None.
        bid_id:          InboundBid database ID, or None.

    Returns:
        {
            "manifest_written": bool,
            "manifest_path": str | None,
        }
    """
    manifest_file = project_root / _MANIFEST_FILENAME

    try:
        rel = "/" + str(manifest_file.relative_to(ROOT))
    except ValueError:
        rel = str(manifest_file)

    if manifest_file.exists():
        logger.info("Manifest already exists — skipping write: %s", rel)
        return {"manifest_written": False, "manifest_path": rel}

    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "workspace_type": "bid",
        "environment": zone,
        "project_name": project_name,
        "normalized_folder": normalized_name,
        "bid_id": bid_id,
        "created_by": "TakeoffAssist",
        "created_at": now,
        "last_updated": now,
        "status": "workspace_created",
        "files": [],
        "file_counts": {k: 0 for k in _ALL_SUBDIRS},
    }

    try:
        manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        logger.info("Manifest v2.0 written: %s", rel)
        return {"manifest_written": True, "manifest_path": rel}
    except Exception as exc:
        logger.error("Manifest write failed (folder creation unaffected): %s — %s", rel, exc)
        return {"manifest_written": False, "manifest_path": None}


def create_project_skeleton(
    folder_name: str,
    zone: str = "dev",
    *,
    bid_id: Optional[int] = None,
    project_name: Optional[str] = None,
) -> dict:
    """Create the standard five-directory skeleton for a new project.

    Directories created (idempotent):
        TakeoffAssistFiles/<zone>/Bids/<folder_name>/
        TakeoffAssistFiles/<zone>/Bids/<folder_name>/Plans/
        TakeoffAssistFiles/<zone>/Bids/<folder_name>/Specifications/
        TakeoffAssistFiles/<zone>/Bids/<folder_name>/Bids/
        TakeoffAssistFiles/<zone>/Bids/<folder_name>/Attachments/
        TakeoffAssistFiles/<zone>/Bids/<folder_name>/Generated/

    Also creates (idempotent, failure-safe):
        TakeoffAssistFiles/<zone>/Bids/<folder_name>/takeoffassist_manifest.json

    The manifest is written as schema v2.0 with an empty files[] array and
    zeroed file_counts for all five subdirectories.

    Args:
        folder_name:  Already-normalised folder name (no path separators).
        zone:         Target zone subfolder — must be 'dev' or 'prod'.
                      Defaults to 'dev' so that callers that omit the field
                      (e.g. older API server versions) write to the safe dev zone.
        bid_id:       InboundBid database ID written into the manifest (optional).
        project_name: Human-readable project name written into the manifest (optional).

    Returns:
        {
            "normalized_name": str,
            "zone": str,
            "dirs": [
                {"path": ..., "created": bool, "already_existed": bool},
                ...
            ],
            "manifest_written": bool,
            "manifest_path": str | None,
        }
    """
    if zone not in _ALLOWED_ZONES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid zone '{zone}' — must be one of: {sorted(_ALLOWED_ZONES)}",
        )

    normalized = normalize_folder_name(folder_name)

    root_dir   = make_dir(f"{zone}/Bids/{normalized}")
    dirs = [root_dir]
    for subdir in _ALL_SUBDIRS:
        dirs.append(make_dir(f"{zone}/Bids/{normalized}/{subdir}"))

    project_root = (WRITE_ZONE / zone / "Bids" / normalized).resolve()
    try:
        manifest_result = create_manifest(
            project_root,
            zone=zone,
            normalized_name=normalized,
            project_name=project_name,
            bid_id=bid_id,
        )
    except Exception as exc:
        logger.error("create_manifest raised unexpectedly (folder creation unaffected): %s", exc)
        manifest_result = {"manifest_written": False, "manifest_path": None}

    return {
        "normalized_name": normalized,
        "zone": zone,
        "dirs": dirs,
        "manifest_written": manifest_result["manifest_written"],
        "manifest_path": manifest_result["manifest_path"],
    }
