"""Read-only filesystem helpers.

All path operations resolve against ROOT_PATH and reject any result
that escapes the root (path traversal protection).  No write, rename,
or delete calls exist anywhere in this module.
"""
from __future__ import annotations

import mimetypes
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from fastapi import HTTPException, status

from .config import settings

ROOT = Path(settings.root_path).resolve()


def _resolve(raw_path: str) -> Path:
    """Resolve *raw_path* relative to ROOT and reject traversal attempts.

    Args:
        raw_path: The path supplied by the caller (may be empty / relative).

    Returns:
        Absolute resolved Path that is guaranteed to be under ROOT.

    Raises:
        HTTP 400 — path escapes the root directory.
        HTTP 404 — resolved path does not exist.
    """
    if not raw_path or raw_path.strip() in ("", "/", "."):
        return ROOT

    try:
        candidate = (ROOT / raw_path.lstrip("/")).resolve()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid path",
        )

    try:
        candidate.relative_to(ROOT)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path traversal rejected",
        )

    if not candidate.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Path not found: {raw_path}",
        )

    return candidate


def _mtime_iso(path: Path) -> str:
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _rel(path: Path) -> str:
    """Return the path string relative to ROOT for API responses."""
    try:
        rel = path.relative_to(ROOT)
        return "/" if str(rel) == "." else "/" + str(rel)
    except ValueError:
        return str(path)


def list_directory(raw_path: str) -> dict:
    """Return directory listing for *raw_path*."""
    resolved = _resolve(raw_path)

    if not resolved.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is a file, not a directory. Use /api/v1/metadata or /api/v1/file.",
        )

    try:
        entries = []
        for child in sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            entry: dict = {
                "name": child.name,
                "path": _rel(child),
                "is_dir": child.is_dir(),
                "modified": _mtime_iso(child),
            }
            if child.is_file():
                entry["size_bytes"] = child.stat().st_size
            else:
                entry["size_bytes"] = None
            entries.append(entry)
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied reading directory",
        )

    return {
        "path": _rel(resolved),
        "entries": entries,
        "count": len(entries),
    }


def get_metadata(raw_path: str) -> dict:
    """Return metadata for a file or directory at *raw_path*."""
    resolved = _resolve(raw_path)

    if resolved.is_dir():
        try:
            children_count = sum(1 for _ in resolved.iterdir())
        except PermissionError:
            children_count = -1

        return {
            "path": _rel(resolved),
            "name": resolved.name or resolved.parts[-1],
            "is_dir": True,
            "children_count": children_count,
            "modified": _mtime_iso(resolved),
        }

    if resolved.is_file():
        extension = resolved.suffix.lower()
        content_type, _ = mimetypes.guess_type(str(resolved))
        content_type = content_type or "application/octet-stream"
        return {
            "path": _rel(resolved),
            "name": resolved.name,
            "is_dir": False,
            "size_bytes": resolved.stat().st_size,
            "extension": extension,
            "content_type": content_type,
            "modified": _mtime_iso(resolved),
        }

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Path is neither a regular file nor a directory",
    )


def open_file(raw_path: str):
    """Return (resolved_path, content_type) for streaming.

    Raises HTTP 400 if the path is a directory.
    """
    resolved = _resolve(raw_path)

    if resolved.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is a directory. Use /api/v1/list instead.",
        )

    if not resolved.is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a regular file",
        )

    content_type, _ = mimetypes.guess_type(str(resolved))
    content_type = content_type or "application/octet-stream"
    return resolved, content_type
