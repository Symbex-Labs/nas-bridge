"""NAS-W004 Phase 3 — manifest v2.0 unit and integration tests.

Covers:
  Pure manifest module (app/manifest.py):
    A  read_manifest — missing file → empty dict
    B  read_manifest — corrupt JSON → empty dict, no exception
    C  read_manifest — v1 manifest → upgraded to v2 in memory, file untouched
    D  read_manifest — v2 manifest → returned as-is
    E  write_manifest — creates valid JSON file
    F  write_manifest — sets schema_version "2.0"
    G  write_manifest — sets/refreshes last_updated
    H  write_manifest — failure-safe: returns False, does not raise
    I  write_manifest — no temp (.tmp) files left after success
    J  register_file  — empty workspace → manifest created, 1 file entry
    K  register_file  — file_counts incremented in correct subdir
    L  register_file  — second file in same subdir → count 2, 2 entries
    M  register_file  — files in different subdirs → per-subdir counts correct
    N  register_file  — unknown source coerced to MANUAL
    O  register_file  — valid source PLANHUB stored as-is
    P  register_file  — object_storage_key stored in entry
    Q  register_file  — entry has all required fields
    R  register_file  — v1 manifest upgraded in place on first register
    S  register_file  — corrupt manifest → fresh v2 manifest created, 1 entry

  Write + manifest integration (app/write.py + app/manifest.py):
    T  write_file — manifest_updated=True in response after successful write
    U  write_file — already_existed=True → manifest_updated=False (no update)
    V  write_file — manifest failure does not affect file-write result
    W  write_file — manifest entry has correct filename, subdir, sha256

  Mkdir + manifest (app/mkdir.py — skeleton creation):
    X  create_project_skeleton — manifest is schema v2.0
    Y  create_project_skeleton — manifest has files[] and file_counts
    Z  create_project_skeleton — all five subdirs created

Run from nas-bridge/:
    pytest tests/test_manifest.py -v
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loader helpers (avoid re-importing stale cached modules)
# ---------------------------------------------------------------------------

def _manifest():
    import app.manifest as m
    return m


def _write():
    import app.write as w
    return w


def _mkdir():
    import app.mkdir as mk
    return mk


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def write_env(tmp_path: Path, monkeypatch):
    """Monkeypatch write.ROOT / write.WRITE_ZONE to tmp dir; create skeleton."""
    jobs_root = tmp_path / "Jobs"
    jobs_root.mkdir()
    write_zone = jobs_root / "TakeoffAssistFiles"
    write_zone.mkdir()

    # Create the workspace directories that write_file expects to exist
    for subdir in ("Plans", "Specifications", "Bids", "Attachments", "Generated"):
        (write_zone / "dev" / "Bids" / "Test_Project" / subdir).mkdir(parents=True)

    w = _write()
    monkeypatch.setattr(w, "ROOT", jobs_root)
    monkeypatch.setattr(w, "WRITE_ZONE", write_zone)
    monkeypatch.setattr(w, "MAX_FILE_SIZE_BYTES", 10 * 1024 * 1024)

    return {"root": jobs_root, "write_zone": write_zone, "w": w}


@pytest.fixture()
def mkdir_env(tmp_path: Path, monkeypatch):
    """Monkeypatch mkdir.ROOT / mkdir.WRITE_ZONE to tmp dir."""
    jobs_root = tmp_path / "Jobs"
    jobs_root.mkdir()
    write_zone = jobs_root / "TakeoffAssistFiles"
    write_zone.mkdir()

    mk = _mkdir()
    monkeypatch.setattr(mk, "ROOT", jobs_root)
    monkeypatch.setattr(mk, "WRITE_ZONE", write_zone)

    return {"root": jobs_root, "write_zone": write_zone, "mk": mk}


# ---------------------------------------------------------------------------
# A — read_manifest: missing file → empty dict
# ---------------------------------------------------------------------------

def test_A_read_manifest_missing_file(tmp_path):
    m = _manifest()
    result = m.read_manifest(tmp_path / "no_such_manifest.json")
    assert result == {}


# ---------------------------------------------------------------------------
# B — read_manifest: corrupt JSON → empty dict, no exception
# ---------------------------------------------------------------------------

def test_B_read_manifest_corrupt_json(tmp_path):
    m = _manifest()
    f = tmp_path / "takeoffassist_manifest.json"
    f.write_text("NOT VALID JSON {{{", encoding="utf-8")
    result = m.read_manifest(f)
    assert result == {}


def test_B2_read_manifest_json_array_not_object(tmp_path):
    """JSON root that is an array (not an object) → treated as corrupt."""
    m = _manifest()
    f = tmp_path / "takeoffassist_manifest.json"
    f.write_text("[1, 2, 3]", encoding="utf-8")
    result = m.read_manifest(f)
    assert result == {}


# ---------------------------------------------------------------------------
# C — read_manifest: v1 manifest → upgraded in memory, disk file unchanged
# ---------------------------------------------------------------------------

def test_C_read_v1_manifest_upgrades_in_memory(tmp_path):
    m = _manifest()
    v1 = {
        "schema_version": "1.0",
        "workspace_type": "bid",
        "environment": "dev",
        "project_name": "Test Project",
        "normalized_folder": "Test_Project",
        "bid_id": 42,
        "created_by": "TakeoffAssist",
        "created_at": "2026-01-01T00:00:00Z",
        "status": "workspace_created",
    }
    f = tmp_path / "takeoffassist_manifest.json"
    f.write_text(json.dumps(v1), encoding="utf-8")

    result = m.read_manifest(f)
    assert result["schema_version"] == "2.0"
    assert isinstance(result["files"], list)
    assert isinstance(result["file_counts"], dict)
    assert "last_updated" in result
    # v1 fields preserved
    assert result["project_name"] == "Test Project"
    assert result["bid_id"] == 42

    # Disk file UNCHANGED — upgrade is in-memory only
    on_disk = json.loads(f.read_text())
    assert on_disk.get("schema_version") == "1.0"
    assert "files" not in on_disk


# ---------------------------------------------------------------------------
# D — read_manifest: v2 manifest → returned as-is
# ---------------------------------------------------------------------------

def test_D_read_v2_manifest_returns_as_is(tmp_path):
    m = _manifest()
    v2 = {
        "schema_version": "2.0",
        "workspace_type": "bid",
        "environment": "dev",
        "project_name": "Test Project",
        "normalized_folder": "Test_Project",
        "bid_id": 7,
        "created_by": "TakeoffAssist",
        "created_at": "2026-06-01T00:00:00Z",
        "last_updated": "2026-06-01T00:00:00Z",
        "status": "workspace_created",
        "files": [],
        "file_counts": {"Plans": 0, "Specifications": 0, "Bids": 0, "Attachments": 0, "Generated": 0},
    }
    f = tmp_path / "takeoffassist_manifest.json"
    f.write_text(json.dumps(v2), encoding="utf-8")

    result = m.read_manifest(f)
    assert result["schema_version"] == "2.0"
    assert result["bid_id"] == 7
    assert result["files"] == []


# ---------------------------------------------------------------------------
# E — write_manifest: creates valid JSON file
# ---------------------------------------------------------------------------

def test_E_write_manifest_creates_file(tmp_path):
    m = _manifest()
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    data = {"schema_version": "2.0", "files": [], "file_counts": {}}
    ok = m.write_manifest(manifest_path, data)
    assert ok is True
    assert manifest_path.exists()
    parsed = json.loads(manifest_path.read_text())
    assert parsed["schema_version"] == "2.0"


# ---------------------------------------------------------------------------
# F — write_manifest: sets schema_version "2.0"
# ---------------------------------------------------------------------------

def test_F_write_manifest_sets_schema_version(tmp_path):
    m = _manifest()
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    m.write_manifest(manifest_path, {"schema_version": "2.0", "files": []})
    content = json.loads(manifest_path.read_text())
    assert content["schema_version"] == "2.0"


# ---------------------------------------------------------------------------
# G — write_manifest: sets/refreshes last_updated
# ---------------------------------------------------------------------------

def test_G_write_manifest_sets_last_updated(tmp_path):
    m = _manifest()
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    data = {"schema_version": "2.0", "files": []}
    m.write_manifest(manifest_path, data)
    content = json.loads(manifest_path.read_text())
    assert "last_updated" in content
    assert content["last_updated"].endswith("Z")


# ---------------------------------------------------------------------------
# H — write_manifest: failure-safe, returns False without raising
# ---------------------------------------------------------------------------

def test_H_write_manifest_failure_safe(tmp_path):
    m = _manifest()
    # Point to a non-existent parent inside a read-only-ish path
    # The simplest way: put the manifest_path inside a file (not a dir)
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a directory")
    manifest_path = blocker / "takeoffassist_manifest.json"  # parent is a file → will fail
    ok = m.write_manifest(manifest_path, {"schema_version": "2.0"})
    assert ok is False


# ---------------------------------------------------------------------------
# I — write_manifest: no leftover .tmp files after success
# ---------------------------------------------------------------------------

def test_I_write_manifest_no_tmp_leftovers(tmp_path):
    m = _manifest()
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    m.write_manifest(manifest_path, {"schema_version": "2.0", "files": []})
    leftover_tmp = list(tmp_path.glob("*.tmp"))
    assert leftover_tmp == [], f"Leftover .tmp files found: {leftover_tmp}"


# ---------------------------------------------------------------------------
# J — register_file: empty workspace → manifest created with 1 file entry
# ---------------------------------------------------------------------------

def test_J_register_file_creates_entry(tmp_path):
    m = _manifest()
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    sha = "a" * 64
    result = m.register_file(
        manifest_path,
        filename="plan.pdf",
        subdir="Plans",
        size_bytes=1024,
        sha256=sha,
    )
    assert result["manifest_updated"] is True
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text())
    assert len(data["files"]) == 1
    assert data["files"][0]["filename"] == "plan.pdf"


# ---------------------------------------------------------------------------
# K — register_file: file_counts incremented in correct subdir
# ---------------------------------------------------------------------------

def test_K_register_file_increments_count(tmp_path):
    m = _manifest()
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    m.register_file(manifest_path, filename="spec.pdf", subdir="Specifications",
                    size_bytes=512, sha256="b" * 64)
    data = json.loads(manifest_path.read_text())
    assert data["file_counts"]["Specifications"] == 1
    assert data["file_counts"]["Plans"] == 0


# ---------------------------------------------------------------------------
# L — register_file: second file in same subdir → count 2, 2 entries
# ---------------------------------------------------------------------------

def test_L_register_second_file_accumulates(tmp_path):
    m = _manifest()
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    m.register_file(manifest_path, filename="a.pdf", subdir="Plans",
                    size_bytes=100, sha256="c" * 64)
    m.register_file(manifest_path, filename="b.pdf", subdir="Plans",
                    size_bytes=200, sha256="d" * 64)
    data = json.loads(manifest_path.read_text())
    assert len(data["files"]) == 2
    assert data["file_counts"]["Plans"] == 2


# ---------------------------------------------------------------------------
# M — register_file: files in different subdirs → per-subdir counts correct
# ---------------------------------------------------------------------------

def test_M_register_files_different_subdirs(tmp_path):
    m = _manifest()
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    m.register_file(manifest_path, filename="plan.pdf", subdir="Plans",
                    size_bytes=100, sha256="e" * 64)
    m.register_file(manifest_path, filename="spec.pdf", subdir="Specifications",
                    size_bytes=200, sha256="f" * 64)
    m.register_file(manifest_path, filename="bid.pdf", subdir="Bids",
                    size_bytes=300, sha256="0" * 64)
    data = json.loads(manifest_path.read_text())
    assert data["file_counts"]["Plans"] == 1
    assert data["file_counts"]["Specifications"] == 1
    assert data["file_counts"]["Bids"] == 1
    assert data["file_counts"]["Attachments"] == 0
    assert data["file_counts"]["Generated"] == 0
    assert len(data["files"]) == 3


# ---------------------------------------------------------------------------
# N — register_file: unknown source coerced to MANUAL
# ---------------------------------------------------------------------------

def test_N_unknown_source_coerced_to_MANUAL(tmp_path):
    m = _manifest()
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    result = m.register_file(
        manifest_path,
        filename="x.pdf",
        subdir="Plans",
        size_bytes=1,
        sha256="1" * 64,
        source="INVALID_SOURCE",
    )
    assert result["manifest_updated"] is True
    data = json.loads(manifest_path.read_text())
    assert data["files"][0]["source"] == "MANUAL"


# ---------------------------------------------------------------------------
# O — register_file: valid source PLANHUB stored as-is
# ---------------------------------------------------------------------------

def test_O_valid_source_stored_correctly(tmp_path):
    m = _manifest()
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    m.register_file(manifest_path, filename="p.pdf", subdir="Plans",
                    size_bytes=1, sha256="2" * 64, source="PLANHUB")
    data = json.loads(manifest_path.read_text())
    assert data["files"][0]["source"] == "PLANHUB"


# ---------------------------------------------------------------------------
# P — register_file: object_storage_key stored in entry
# ---------------------------------------------------------------------------

def test_P_object_storage_key_stored(tmp_path):
    m = _manifest()
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    key = "dev/bids/42/Plans/plan.pdf"
    m.register_file(manifest_path, filename="plan.pdf", subdir="Plans",
                    size_bytes=99, sha256="3" * 64, object_storage_key=key)
    data = json.loads(manifest_path.read_text())
    assert data["files"][0]["object_storage_key"] == key


# ---------------------------------------------------------------------------
# Q — register_file: entry has all required fields
# ---------------------------------------------------------------------------

def test_Q_entry_has_all_required_fields(tmp_path):
    m = _manifest()
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    sha = "4" * 64
    result = m.register_file(
        manifest_path,
        filename="report.pdf",
        subdir="Generated",
        size_bytes=2048,
        sha256=sha,
        source="GENERATED",
        uploaded_by="user:mrk_estimator",
    )
    entry = result["entry"]
    assert entry["filename"] == "report.pdf"
    assert entry["path"] == "Generated/report.pdf"
    assert entry["subdir"] == "Generated"
    assert entry["size_bytes"] == 2048
    assert entry["sha256"] == sha
    assert entry["source"] == "GENERATED"
    assert entry["uploaded_by"] == "user:mrk_estimator"
    assert entry["object_storage_key"] is None
    assert entry["duplicate_of"] is None
    assert "uploaded_at" in entry
    assert entry["uploaded_at"].endswith("Z")


# ---------------------------------------------------------------------------
# R — register_file: v1 manifest → upgraded on disk after registration
# ---------------------------------------------------------------------------

def test_R_register_upgrades_v1_manifest_on_disk(tmp_path):
    m = _manifest()
    v1 = {
        "schema_version": "1.0",
        "workspace_type": "bid",
        "environment": "dev",
        "project_name": "Old Project",
        "normalized_folder": "Old_Project",
        "bid_id": 9,
        "created_by": "TakeoffAssist",
        "created_at": "2025-01-01T00:00:00Z",
        "status": "workspace_created",
    }
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    manifest_path.write_text(json.dumps(v1), encoding="utf-8")

    result = m.register_file(manifest_path, filename="x.pdf", subdir="Plans",
                             size_bytes=10, sha256="5" * 64)
    assert result["manifest_updated"] is True

    # Disk now has v2 with the registered file
    data = json.loads(manifest_path.read_text())
    assert data["schema_version"] == "2.0"
    assert len(data["files"]) == 1
    assert data["file_counts"]["Plans"] == 1
    # v1 fields preserved
    assert data["project_name"] == "Old Project"


# ---------------------------------------------------------------------------
# S — register_file: corrupt manifest → fresh v2 manifest, 1 entry
# ---------------------------------------------------------------------------

def test_S_register_on_corrupt_manifest_creates_fresh(tmp_path):
    m = _manifest()
    manifest_path = tmp_path / "takeoffassist_manifest.json"
    manifest_path.write_text("garbage{{{not json", encoding="utf-8")

    result = m.register_file(manifest_path, filename="plan.pdf", subdir="Plans",
                             size_bytes=5, sha256="6" * 64)
    assert result["manifest_updated"] is True
    data = json.loads(manifest_path.read_text())
    assert data["schema_version"] == "2.0"
    assert len(data["files"]) == 1


# ---------------------------------------------------------------------------
# T — write_file integration: manifest_updated=True after successful write
# ---------------------------------------------------------------------------

def test_T_write_file_manifest_updated(write_env):
    w = write_env["w"]
    write_zone = write_env["write_zone"]
    content = b"%PDF-1.4 hello"
    result = w.write_file("dev", "Test_Project/Plans", "plan.pdf", content)
    assert result["success"] is True
    assert result["already_existed"] is False
    assert result["manifest_updated"] is True

    # Manifest should exist with 1 entry
    manifest_path = write_zone / "dev" / "Bids" / "Test_Project" / "takeoffassist_manifest.json"
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text())
    assert len(data["files"]) == 1
    assert data["files"][0]["filename"] == "plan.pdf"
    assert data["files"][0]["subdir"] == "Plans"
    assert data["file_counts"]["Plans"] == 1


# ---------------------------------------------------------------------------
# U — write_file integration: already_existed → manifest_updated=False
# ---------------------------------------------------------------------------

def test_U_write_file_already_existed_no_manifest_update(write_env):
    w = write_env["w"]
    write_zone = write_env["write_zone"]
    content = b"%PDF-1.4 identical"
    # First write
    w.write_file("dev", "Test_Project/Plans", "identical.pdf", content)
    # Second write (identical)
    result = w.write_file("dev", "Test_Project/Plans", "identical.pdf", content)
    assert result["already_existed"] is True
    assert result["manifest_updated"] is False

    # Manifest should still have exactly 1 entry (from the first write only)
    manifest_path = write_zone / "dev" / "Bids" / "Test_Project" / "takeoffassist_manifest.json"
    data = json.loads(manifest_path.read_text())
    assert len(data["files"]) == 1


# ---------------------------------------------------------------------------
# V — write_file integration: manifest failure does not block file write
# ---------------------------------------------------------------------------

def test_V_write_file_manifest_failure_does_not_block_write(write_env, monkeypatch):
    w = write_env["w"]
    write_zone = write_env["write_zone"]

    # Force manifest.register_file to raise an exception
    import app.manifest as m_mod
    monkeypatch.setattr(m_mod, "write_manifest", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

    content = b"%PDF-1.4 blocky"
    result = w.write_file("dev", "Test_Project/Plans", "blocky.pdf", content)

    # File write should have succeeded
    assert result["success"] is True
    assert (write_zone / "dev" / "Bids" / "Test_Project" / "Plans" / "blocky.pdf").exists()
    # Manifest update should have failed gracefully
    assert result["manifest_updated"] is False


# ---------------------------------------------------------------------------
# W — write_file integration: entry has correct filename, subdir, sha256
# ---------------------------------------------------------------------------

def test_W_write_file_manifest_entry_correctness(write_env):
    w = write_env["w"]
    write_zone = write_env["write_zone"]
    content = b"%PDF-1.4 " + b"X" * 200
    sha256 = hashlib.sha256(content).hexdigest()
    w.write_file(
        "dev", "Test_Project/Bids", "proposal.pdf", content,
        source="EMAIL",
        uploaded_by="user:estimator1",
    )
    manifest_path = write_zone / "dev" / "Bids" / "Test_Project" / "takeoffassist_manifest.json"
    data = json.loads(manifest_path.read_text())
    entry = data["files"][0]
    assert entry["filename"] == "proposal.pdf"
    assert entry["subdir"] == "Bids"
    assert entry["sha256"] == sha256
    assert entry["source"] == "EMAIL"
    assert entry["uploaded_by"] == "user:estimator1"
    assert entry["size_bytes"] == len(content)


# ---------------------------------------------------------------------------
# X — mkdir integration: create_project_skeleton writes schema v2 manifest
# ---------------------------------------------------------------------------

def test_X_skeleton_creates_v2_manifest(mkdir_env):
    mk = mkdir_env["mk"]
    write_zone = mkdir_env["write_zone"]
    result = mk.create_project_skeleton("Test Project", zone="dev", bid_id=5, project_name="Test Project")
    assert result["manifest_written"] is True

    manifest_path = write_zone / "dev" / "Bids" / "Test_Project" / "takeoffassist_manifest.json"
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text())
    assert data["schema_version"] == "2.0"
    assert data["bid_id"] == 5
    assert isinstance(data["files"], list)
    assert data["files"] == []


# ---------------------------------------------------------------------------
# Y — mkdir integration: manifest has file_counts for all 5 subdirs zeroed
# ---------------------------------------------------------------------------

def test_Y_skeleton_manifest_file_counts_initialized(mkdir_env):
    mk = mkdir_env["mk"]
    write_zone = mkdir_env["write_zone"]
    mk.create_project_skeleton("Count Project", zone="dev")
    manifest_path = write_zone / "dev" / "Bids" / "Count_Project" / "takeoffassist_manifest.json"
    data = json.loads(manifest_path.read_text())
    for subdir in ("Plans", "Specifications", "Bids", "Attachments", "Generated"):
        assert data["file_counts"][subdir] == 0, f"Expected {subdir}=0, got {data['file_counts']}"


# ---------------------------------------------------------------------------
# Z — mkdir integration: all five subdirs created on disk
# ---------------------------------------------------------------------------

def test_Z_skeleton_creates_five_subdirs(mkdir_env):
    mk = mkdir_env["mk"]
    write_zone = mkdir_env["write_zone"]
    mk.create_project_skeleton("Five Dirs", zone="dev")
    project_root = write_zone / "dev" / "Bids" / "Five_Dirs"
    for subdir in ("Plans", "Specifications", "Bids", "Attachments", "Generated"):
        assert (project_root / subdir).is_dir(), f"Subdir '{subdir}' not created"
