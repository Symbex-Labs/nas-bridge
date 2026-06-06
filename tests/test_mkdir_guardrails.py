"""NAS-W002 / NAS-W003 — Mkdir guardrail, zone-routing, and manifest unit tests.

Tests 1–8 per the NAS-W002 spec, plus normalization edge cases,
plus zone-routing tests added in NAS zone correction (v1.2.0),
plus manifest tests added in NAS-W003.

Run from the nas-bridge/ directory:
    pip install fastapi pydantic pydantic-settings pytest
    pytest tests/test_mkdir_guardrails.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _get_mkdir():
    import app.mkdir as m  # noqa: PLC0415  (conftest.py adds nas-bridge/ to sys.path)
    return m


@pytest.fixture()
def mkdir_env(tmp_path: Path, monkeypatch):
    """Patch mkdir.ROOT and mkdir.WRITE_ZONE to tmp dirs."""
    jobs_root = tmp_path / "Jobs"
    jobs_root.mkdir()
    write_zone = jobs_root / "TakeoffAssistFiles"
    write_zone.mkdir()

    for protected in ("Bids", "Invoices", "WorkLoad"):
        (jobs_root / protected).mkdir()

    m = _get_mkdir()
    monkeypatch.setattr(m, "ROOT", jobs_root)
    monkeypatch.setattr(m, "WRITE_ZONE", write_zone)
    return {"root": jobs_root, "write_zone": write_zone, "m": m}


# ── Test 1 — Successful skeleton creation ─────────────────────────────────────

def test_1_successful_skeleton_creation(mkdir_env):
    """Test 1: create_project_skeleton creates the six expected directories in the dev zone.

    Phase 3 (NAS-W004): skeleton now creates root + 5 subdirs
    (Plans, Specifications, Bids, Attachments, Generated).
    """
    m = mkdir_env["m"]
    write_zone = mkdir_env["write_zone"]

    result = m.create_project_skeleton("Mike Judson Building", zone="dev")

    assert result["normalized_name"] == "Mike_Judson_Building"
    assert result["zone"] == "dev"
    # 1 root dir + 5 subdirs = 6 total
    assert len(result["dirs"]) == 6

    paths_created = [d["path"] for d in result["dirs"]]
    assert any(p.endswith("Mike_Judson_Building") for p in paths_created), \
        f"Root dir not found in {paths_created}"
    assert any(p.endswith("/Plans") for p in paths_created), \
        f"Plans dir not found in {paths_created}"
    assert any(p.endswith("Mike_Judson_Building/Bids") for p in paths_created), \
        f"Inner Bids dir not found in {paths_created}"

    project_root = write_zone / "dev" / "Bids" / "Mike_Judson_Building"
    assert project_root.is_dir()
    for subdir in ("Plans", "Specifications", "Bids", "Attachments", "Generated"):
        assert (project_root / subdir).is_dir(), f"Subdir '{subdir}' not created"

    for d in result["dirs"]:
        assert d["created"] is True
        assert d["already_existed"] is False


# ── Test 2 — Idempotent re-run ────────────────────────────────────────────────

def test_2_idempotent_rerun(mkdir_env):
    """Test 2: A second call to create_project_skeleton returns already_existed=True."""
    m = mkdir_env["m"]

    m.create_project_skeleton("Starbucks West Allis", zone="dev")
    result2 = m.create_project_skeleton("Starbucks West Allis", zone="dev")

    for d in result2["dirs"]:
        assert d["already_existed"] is True
        assert d["created"] is False


# ── Test 3 — Name normalisation ───────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Mike Judson Building",  "Mike_Judson_Building"),
    ("  Leading Space  ",     "Leading_Space"),
    ("Starbucks / West",      "Starbucks_West"),    # slash→_, spaces→_, collapse
    ("Job: Alpha/Beta",       "Job_Alpha_Beta"),    # colon→_, slash→_, collapse
    ("..leading dots",        "leading_dots"),      # strip leading dots
    ("trailing dots..",       "trailing_dots"),     # strip trailing dots
    ("collapse___underscores","collapse_underscores"),
    ("A" * 100,               "A" * 80),            # truncate at 80
    ("Job Name",              "Job_Name"),
    ("Simple",                "Simple"),
])
def test_3_name_normalisation(raw, expected, mkdir_env):
    """Test 3: normalize_folder_name() produces the expected output."""
    m = mkdir_env["m"]
    assert m.normalize_folder_name(raw) == expected


# ── Test 4 — Empty name rejection ────────────────────────────────────────────

@pytest.mark.parametrize("bad_name", [
    "",
    "   ",
    "...",
    "___",
    "..___",
])
def test_4_empty_name_rejected(bad_name, mkdir_env):
    """Test 4: Empty or whitespace-only names must raise HTTP 400."""
    from fastapi import HTTPException
    m = mkdir_env["m"]

    with pytest.raises(HTTPException) as exc_info:
        m.normalize_folder_name(bad_name)

    assert exc_info.value.status_code == 400


# ── Test 5 — Path traversal rejection ────────────────────────────────────────

@pytest.mark.parametrize("bad_path", [
    "../Bids/pwned",
    "../../etc/passwd",
    "foo/../../secret",
    "../WorkLoad/evil",
])
def test_5_traversal_rejected(bad_path, mkdir_env):
    """Test 5: Path traversal must be rejected with HTTP 400."""
    from fastapi import HTTPException
    m = mkdir_env["m"]

    with pytest.raises(HTTPException) as exc_info:
        m.make_dir(bad_path)

    assert exc_info.value.status_code == 400, (
        f"Expected 400 for '{bad_path}', got {exc_info.value.status_code}: {exc_info.value.detail}"
    )


# ── Test 6 — Attempt to escape TakeoffAssistFiles into production zones ────────

@pytest.mark.parametrize("bad_path", [
    "../Bids/pwned",           # traversal to /Jobs/Bids
    "../Invoices/pwned",       # traversal to /Jobs/Invoices
    "../WorkLoad/pwned",       # traversal to /Jobs/WorkLoad
    "../../volume1/Jobs/Bids", # deeper traversal
])
def test_6_production_zone_not_reachable(bad_path, mkdir_env):
    """Test 6: Traversal attempts targeting production zones (/Jobs/Bids etc.) must be rejected.

    Protection comes from two layers:
      1. The '..' check in _validate_rel_path → HTTP 400
      2. The containment check (candidate.relative_to(WRITE_ZONE)) → HTTP 403
    Either is sufficient; both fire in practice for traversal inputs.
    """
    from fastapi import HTTPException
    m = mkdir_env["m"]

    with pytest.raises(HTTPException) as exc_info:
        m.make_dir(bad_path)

    assert exc_info.value.status_code in (400, 403), (
        f"Expected 400/403 for '{bad_path}', got {exc_info.value.status_code}: {exc_info.value.detail}"
    )


# ── Test 7 — Existing read-only NAS features unaffected ───────────────────────

def test_7_write_test_still_works(mkdir_env, monkeypatch):
    """Test 7: The write_test module still works correctly after mkdir module is loaded."""
    write_zone = mkdir_env["write_zone"]

    import app.write_test as wt  # noqa: PLC0415
    monkeypatch.setattr(wt, "ROOT", mkdir_env["root"])
    monkeypatch.setattr(wt, "WRITE_ZONE", write_zone)

    result = wt.write_and_verify("regression-check.txt", "mkdir regression test")
    assert result["success"] is True
    assert result["read_back_verified"] is True


# ── Test 8 — Browser never receives NAS token ─────────────────────────────────

def test_8_token_not_in_response(mkdir_env):
    """Test 8: create_project_skeleton response contains no credential data."""
    import os
    m = mkdir_env["m"]

    result = m.create_project_skeleton("Token Safety Check", zone="dev")
    response_str = str(result)

    bridge_token = os.environ.get("BRIDGE_TOKEN", "")
    if bridge_token and bridge_token != "test-token-for-unit-tests":
        assert bridge_token not in response_str

    assert set(result.keys()) == {"normalized_name", "zone", "dirs", "manifest_written", "manifest_path"}
    for d in result["dirs"]:
        assert set(d.keys()) == {"path", "created", "already_existed"}


# ── Extra: absolute paths rejected ───────────────────────────────────────────

@pytest.mark.parametrize("abs_path", [
    "/TakeoffAssistFiles/Bids/job",
    "/etc/passwd",
    "\\\\server\\share",
])
def test_absolute_paths_rejected(abs_path, mkdir_env):
    """Absolute paths must be rejected with HTTP 400."""
    from fastapi import HTTPException
    m = mkdir_env["m"]

    with pytest.raises(HTTPException) as exc_info:
        m.make_dir(abs_path)

    assert exc_info.value.status_code in (400, 403)


# ── Extra: deep nesting still contained in write zone ────────────────────────

def test_deep_nesting_contained(mkdir_env):
    """Deep but valid paths must still be within WRITE_ZONE."""
    m = mkdir_env["m"]

    result = m.make_dir("dev/Bids/JobName/Plans")
    assert "TakeoffAssistFiles" in result["path"]
    assert result["created"] is True
    assert (mkdir_env["write_zone"] / "dev" / "Bids" / "JobName" / "Plans").is_dir()


# ── Zone routing tests (v1.2.0) ───────────────────────────────────────────────

def test_zone_dev_writes_to_dev_subfolder(mkdir_env):
    """zone='dev' must create directories under TakeoffAssistFiles/dev/Bids/."""
    m = mkdir_env["m"]
    write_zone = mkdir_env["write_zone"]

    result = m.create_project_skeleton("Zone Dev Project", zone="dev")

    assert result["zone"] == "dev"
    assert all("dev/Bids" in d["path"] for d in result["dirs"]), \
        f"Expected all paths to contain 'dev/Bids', got: {[d['path'] for d in result['dirs']]}"
    assert (write_zone / "dev" / "Bids" / "Zone_Dev_Project").is_dir()
    assert not (write_zone / "prod" / "Bids" / "Zone_Dev_Project").exists(), \
        "dev write must not appear under prod/"
    assert not (write_zone / "Bids" / "Zone_Dev_Project").exists(), \
        "dev write must not appear under flat Bids/"


def test_zone_prod_writes_to_prod_subfolder(mkdir_env):
    """zone='prod' must create directories under TakeoffAssistFiles/prod/Bids/."""
    m = mkdir_env["m"]
    write_zone = mkdir_env["write_zone"]

    result = m.create_project_skeleton("Zone Prod Project", zone="prod")

    assert result["zone"] == "prod"
    assert all("prod/Bids" in d["path"] for d in result["dirs"]), \
        f"Expected all paths to contain 'prod/Bids', got: {[d['path'] for d in result['dirs']]}"
    assert (write_zone / "prod" / "Bids" / "Zone_Prod_Project").is_dir()
    assert not (write_zone / "dev" / "Bids" / "Zone_Prod_Project").exists(), \
        "prod write must not appear under dev/"
    assert not (write_zone / "Bids" / "Zone_Prod_Project").exists(), \
        "prod write must not appear under flat Bids/"


def test_zone_dev_and_prod_are_isolated(mkdir_env):
    """Same project name in dev and prod zones must create separate directories."""
    m = mkdir_env["m"]
    write_zone = mkdir_env["write_zone"]

    m.create_project_skeleton("Shared Name", zone="dev")
    m.create_project_skeleton("Shared Name", zone="prod")

    assert (write_zone / "dev" / "Bids" / "Shared_Name").is_dir()
    assert (write_zone / "prod" / "Bids" / "Shared_Name").is_dir()


def test_zone_invalid_value_rejected(mkdir_env):
    """An invalid zone value must be rejected with HTTP 400."""
    from fastapi import HTTPException
    m = mkdir_env["m"]

    for bad_zone in ("staging", "admin", "", "../dev", "DEV", "PROD"):
        with pytest.raises(HTTPException) as exc_info:
            m.create_project_skeleton("Some Project", zone=bad_zone)
        assert exc_info.value.status_code == 400, \
            f"Expected 400 for zone={bad_zone!r}, got {exc_info.value.status_code}"


def test_zone_default_is_dev(mkdir_env):
    """Omitting zone must default to 'dev' (backward-compat for old API server versions)."""
    m = mkdir_env["m"]
    write_zone = mkdir_env["write_zone"]

    result = m.create_project_skeleton("Default Zone Test")

    assert result["zone"] == "dev"
    assert (write_zone / "dev" / "Bids" / "Default_Zone_Test").is_dir()
    assert not (write_zone / "Bids" / "Default_Zone_Test").exists()


# ── Manifest tests (NAS-W003) ─────────────────────────────────────────────────

def test_manifest_created_with_skeleton(mkdir_env):
    """NAS-W003: manifest file is created inside the project root on first call."""
    import json
    m = mkdir_env["m"]
    write_zone = mkdir_env["write_zone"]

    result = m.create_project_skeleton(
        "Manifest Test Project",
        zone="dev",
        bid_id=42,
        project_name="Manifest Test Project",
    )

    assert result["manifest_written"] is True
    assert result["manifest_path"] is not None
    assert "takeoffassist_manifest.json" in result["manifest_path"]

    manifest_file = write_zone / "dev" / "Bids" / "Manifest_Test_Project" / "takeoffassist_manifest.json"
    assert manifest_file.exists(), "Manifest file must exist on disk"

    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    # Phase 3 (NAS-W004): manifest is now schema v2.0
    assert data["schema_version"] == "2.0"
    assert data["workspace_type"] == "bid"
    assert data["environment"] == "dev"
    assert data["normalized_folder"] == "Manifest_Test_Project"
    assert data["bid_id"] == 42
    assert data["project_name"] == "Manifest Test Project"
    assert data["created_by"] == "TakeoffAssist"
    assert data["status"] == "workspace_created"
    assert "created_at" in data
    assert data["created_at"].endswith("Z"), "created_at must be UTC ISO 8601"
    # v2.0 fields
    assert isinstance(data["files"], list) and data["files"] == []
    assert isinstance(data["file_counts"], dict)
    for subdir in ("Plans", "Specifications", "Bids", "Attachments", "Generated"):
        assert data["file_counts"].get(subdir) == 0
    assert "last_updated" in data


def test_manifest_idempotent_not_overwritten(mkdir_env):
    """NAS-W003: a second call must not overwrite an existing manifest."""
    import json
    m = mkdir_env["m"]
    write_zone = mkdir_env["write_zone"]

    m.create_project_skeleton("Idempotent Manifest", zone="dev", bid_id=1, project_name="First Call")

    manifest_file = write_zone / "dev" / "Bids" / "Idempotent_Manifest" / "takeoffassist_manifest.json"
    mtime_after_first = manifest_file.stat().st_mtime
    content_after_first = manifest_file.read_text(encoding="utf-8")

    result2 = m.create_project_skeleton("Idempotent Manifest", zone="dev", bid_id=99, project_name="Second Call")

    assert result2["manifest_written"] is False, "manifest_written must be False on re-run"
    assert manifest_file.stat().st_mtime == mtime_after_first, "Manifest must not be modified on re-run"

    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert data["bid_id"] == 1, "Original bid_id must be preserved — manifest not overwritten"
    assert data["project_name"] == "First Call", "Original project_name must be preserved"


def test_manifest_failure_does_not_block_folder_creation(mkdir_env, monkeypatch):
    """NAS-W003: if manifest write raises an OSError, folder creation still succeeds."""
    import app.mkdir as m_mod
    m = mkdir_env["m"]
    write_zone = mkdir_env["write_zone"]

    original_create_manifest = m_mod.create_manifest

    def _failing_manifest(*args, **kwargs):
        raise OSError("Simulated disk full")

    monkeypatch.setattr(m_mod, "create_manifest", _failing_manifest)

    result = m.create_project_skeleton("Failure Safe Project", zone="dev")

    assert (write_zone / "dev" / "Bids" / "Failure_Safe_Project").is_dir(), \
        "Project root must be created even when manifest write fails"
    assert (write_zone / "dev" / "Bids" / "Failure_Safe_Project" / "Plans").is_dir()
    assert (write_zone / "dev" / "Bids" / "Failure_Safe_Project" / "Bids").is_dir()

    assert result["manifest_written"] is False
    assert result["manifest_path"] is None


def test_manifest_without_optional_fields(mkdir_env):
    """NAS-W003: manifest is created correctly when bid_id and project_name are omitted."""
    import json
    m = mkdir_env["m"]
    write_zone = mkdir_env["write_zone"]

    result = m.create_project_skeleton("No Optional Fields", zone="prod")

    assert result["manifest_written"] is True

    manifest_file = write_zone / "prod" / "Bids" / "No_Optional_Fields" / "takeoffassist_manifest.json"
    data = json.loads(manifest_file.read_text(encoding="utf-8"))

    assert data["bid_id"] is None
    assert data["project_name"] is None
    assert data["environment"] == "prod"
    # Phase 3 (NAS-W004): manifest is now schema v2.0
    assert data["schema_version"] == "2.0"
    assert isinstance(data["files"], list) and data["files"] == []
    assert isinstance(data["file_counts"], dict)
