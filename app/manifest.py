"""Manifest v2.0 — workspace file catalog for TakeoffAssist NAS workspaces.

NAS-W004 Phase 3: the manifest evolves from a workspace-creation record into
the authoritative catalog of every file stored in a workspace directory.

Schema v2.0 additions over v1.0:
    files        — list of file-entry dicts, one per file written to the workspace
    file_counts  — per-subdir integer counters (Plans, Specifications, Bids,
                   Attachments, Generated)
    last_updated — ISO-8601 UTC timestamp refreshed on every write

Backward compatibility:
    read_manifest() transparently upgrades v1.x manifests to v2.0 in memory.
    The upgrade is NOT written to disk until register_file() is called — lazy
    upgrade avoids touching manifests for workspaces that never receive a file.

Duplicate-write policy:
    When write.py detects an identical file (same SHA-256, already_existed=True),
    no NAS write is performed and register_file() is NOT called.  The manifest
    is the catalog of files on disk; it does not record duplicate-upload attempts.

Allowed source labels:
    MANUAL, EMAIL, PLANHUB, BUILDINGCONNECTED, PROCORE, GENERATED

Atomic write:
    write_manifest() writes to a sibling tmp file then calls Path.replace() so
    the manifest is never left in a partially-written state.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "takeoffassist_manifest.json"
SCHEMA_VERSION = "2.0"

_ALLOWED_SOURCES: frozenset[str] = frozenset({
    "MANUAL", "EMAIL", "PLANHUB", "BUILDINGCONNECTED", "PROCORE", "GENERATED",
})

_ALL_SUBDIRS: tuple[str, ...] = (
    "Plans", "Specifications", "Bids", "Attachments", "Generated",
)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_file_counts() -> dict:
    return {k: 0 for k in _ALL_SUBDIRS}


def _upgrade_v1_to_v2(data: dict) -> dict:
    """Return a v2.0 manifest dict derived from a v1.x (or version-less) dict.

    The original dict is not mutated — a new dict is returned.
    """
    upgraded = dict(data)
    upgraded["schema_version"] = SCHEMA_VERSION
    if "files" not in upgraded or not isinstance(upgraded["files"], list):
        upgraded["files"] = []
    if "file_counts" not in upgraded or not isinstance(upgraded["file_counts"], dict):
        upgraded["file_counts"] = _empty_file_counts()
    else:
        for subdir in _ALL_SUBDIRS:
            upgraded["file_counts"].setdefault(subdir, 0)
    if "last_updated" not in upgraded:
        upgraded["last_updated"] = _now_utc()
    return upgraded


# ── Public API ─────────────────────────────────────────────────────────────────

def read_manifest(manifest_path: Path) -> dict:
    """Read and parse the workspace manifest from disk.

    Behaviours:
    - Missing file  → returns empty dict  (no error)
    - Corrupt JSON  → returns empty dict  (logged as WARNING)
    - Schema v1.x   → upgrades to v2.0 in memory; nothing written to disk
    - Schema v2.0   → returned as-is

    Never raises.
    """
    if not manifest_path.exists():
        return {}

    try:
        raw = manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Manifest root is not a JSON object")
    except Exception as exc:
        logger.warning(
            "Manifest corrupt or unreadable at %s — treating as empty: %s",
            manifest_path, exc,
        )
        return {}

    version = data.get("schema_version", "1.0")
    if version != SCHEMA_VERSION:
        logger.info(
            "Manifest schema v%s at %s — upgrading to v%s in memory",
            version, manifest_path, SCHEMA_VERSION,
        )
        data = _upgrade_v1_to_v2(data)

    return data


def write_manifest(manifest_path: Path, data: dict) -> bool:
    """Atomically write the manifest dict to disk (temp-file + rename).

    Sets ``last_updated`` on the dict before serialising.

    Failure-safe: any OS or serialisation error is caught, logged, and
    False is returned.  Never raises.

    Returns:
        True  — manifest written successfully.
        False — write failed (file write is unaffected).
    """
    try:
        data = dict(data)
        data["last_updated"] = _now_utc()
        content = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        parent = manifest_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
        try:
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        Path(tmp_path).replace(manifest_path)
        logger.debug("Manifest written: %s", manifest_path)
        return True
    except Exception as exc:
        logger.error("Manifest write failed at %s: %s", manifest_path, exc)
        return False


def register_file(
    manifest_path: Path,
    *,
    filename: str,
    subdir: str,
    size_bytes: int,
    sha256: str,
    source: str = "MANUAL",
    uploaded_by: str = "system",
    object_storage_key: Optional[str] = None,
) -> dict:
    """Register a newly-written file in the workspace manifest.

    Reads the current manifest (upgrading v1→v2 if needed), appends a file
    entry, increments the subdir counter, and writes back atomically.

    Should NOT be called when ``already_existed=True`` — duplicate writes leave
    the manifest unchanged (the existing entry already represents that file).

    Args:
        manifest_path:      Absolute Path to takeoffassist_manifest.json.
        filename:           Actual filename on disk (may be timestamp-renamed).
        subdir:             Workspace sub-directory label (Plans, Bids, etc.).
        size_bytes:         File size in bytes.
        sha256:             Hex SHA-256 digest of the written content.
        source:             Upload-source label — one of _ALLOWED_SOURCES.
                            Unknown values are silently coerced to "MANUAL".
        uploaded_by:        Audit identity string, e.g. ``"user:alice"``.
        object_storage_key: Object Storage key for the archived copy, or None.

    Returns::

        {
            "manifest_updated": bool,  # True if manifest was written successfully
            "entry": dict,             # The file-entry dict that was appended
        }
    """
    if source not in _ALLOWED_SOURCES:
        logger.warning("Unknown source '%s' — coercing to MANUAL", source)
        source = "MANUAL"

    entry: dict = {
        "filename":           filename,
        "path":               f"{subdir}/{filename}",
        "subdir":             subdir,
        "size_bytes":         size_bytes,
        "sha256":             sha256,
        "uploaded_at":        _now_utc(),
        "source":             source,
        "uploaded_by":        uploaded_by,
        "object_storage_key": object_storage_key,
        "duplicate_of":       None,
    }

    data = read_manifest(manifest_path)

    if data.get("schema_version") != SCHEMA_VERSION:
        data = _upgrade_v1_to_v2(data)

    if not isinstance(data.get("files"), list):
        data["files"] = []
    if not isinstance(data.get("file_counts"), dict):
        data["file_counts"] = _empty_file_counts()
    for s in _ALL_SUBDIRS:
        data["file_counts"].setdefault(s, 0)

    data["files"].append(entry)
    data["file_counts"][subdir] = data["file_counts"].get(subdir, 0) + 1

    ok = write_manifest(manifest_path, data)
    return {"manifest_updated": ok, "entry": entry}
