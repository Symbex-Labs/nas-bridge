"""Write-Test Zone — strictly scoped to ROOT/TakeoffAssistFiles.

This is the ONLY module in the bridge that performs write operations.
filesystem.py remains entirely read-only.

Approved write zone:
    ROOT / "TakeoffAssistFiles"   →   /volume1/Jobs/TakeoffAssistFiles

Hard-denied zones (defense-in-depth — code-level, independent of filesystem perms):
    Bids, Invoices, WorkLoad
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException, status

from .config import settings

ROOT = Path(settings.root_path).resolve()

WRITE_ZONE_NAME = "TakeoffAssistFiles"
WRITE_ZONE = ROOT / WRITE_ZONE_NAME

_PROTECTED = frozenset({"Bids", "Invoices", "WorkLoad"})

_SAFE_FILENAME = re.compile(r"^[^\x00/\\]{1,255}$")


def _validate_filename(filename: str) -> None:
    """Raise 400/403 if the filename is unsafe or targets a protected zone.

    Checks (in order):
      1. Non-empty
      2. No path separators, null bytes, or control characters
      3. No dotdot component
      4. Not a protected-zone name
    """
    if not filename or not filename.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Filename must not be empty",
        )

    if not _SAFE_FILENAME.match(filename):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Invalid filename: must not contain path separators (/ \\), "
                "null bytes, or control characters"
            ),
        )

    if ".." in filename.split("/") or ".." in filename.split("\\"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path traversal rejected",
        )

    leading = filename.split("/")[0].split("\\")[0]
    if leading in _PROTECTED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Write to protected zone '{leading}' is not permitted",
        )


def _ensure_write_zone() -> None:
    """Ensure TakeoffAssistFiles/ exists; raise 503 if not writable."""
    try:
        WRITE_ZONE.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Write zone '{WRITE_ZONE_NAME}' is not writable. "
                "Ensure /volume1/Jobs/TakeoffAssistFiles is mounted rw and "
                "the bridge process has write permission to that directory."
            ),
        )
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Cannot access write zone: {exc}",
        )


def _resolve_write_target(filename: str) -> Path:
    """Validate *filename* and return the absolute target path inside WRITE_ZONE.

    Unlike filesystem._resolve(), this function does NOT require the target
    to already exist — it is resolving a destination for a new file.
    """
    _validate_filename(filename)

    candidate = (WRITE_ZONE / filename).resolve()

    try:
        candidate.relative_to(WRITE_ZONE)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Resolved path escapes the TakeoffAssistFiles write zone",
        )

    return candidate


def write_and_verify(filename: str, contents: str) -> dict:
    """Write *contents* to WRITE_ZONE/<filename>, read back, and verify.

    Args:
        filename: Simple filename (no path separators). Written under
                  /Jobs/TakeoffAssistFiles/.
        contents: UTF-8 text to write.

    Returns:
        {
            "success": True,
            "bytes_written": int,
            "read_back_verified": bool,
            "path": str   # e.g. "/TakeoffAssistFiles/smoke-test.txt"
        }

    Raises:
        HTTP 400 — invalid filename or path traversal attempt
        HTTP 403 — write to protected zone or zone escape attempt
        HTTP 503 — write zone not writable (Docker mount issue)
        HTTP 500 — unexpected OS error
    """
    _ensure_write_zone()
    target = _resolve_write_target(filename)

    encoded = contents.encode("utf-8")

    try:
        target.write_bytes(encoded)
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Write failed: permission denied. "
                "Mount /volume1/Jobs/TakeoffAssistFiles as rw in docker-compose.yml."
            ),
        )
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Write failed: {exc}",
        )

    bytes_written = len(encoded)

    try:
        read_back = target.read_bytes()
        verified = read_back == encoded
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Read-back failed after write: {exc}",
        )

    try:
        rel = "/" + str(target.relative_to(ROOT))
    except ValueError:
        rel = str(target)

    return {
        "success": True,
        "bytes_written": bytes_written,
        "read_back_verified": verified,
        "path": rel,
    }
