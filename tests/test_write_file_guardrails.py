"""NAS-W004 Phase 1 — write.py guardrail tests.

All tests run against a temporary directory — no live NAS or Docker required.
The write module is monkeypatched so ROOT and WRITE_ZONE point to tmp paths.

Run from the nas-bridge/ directory:
    pip install fastapi pydantic pydantic-settings pytest
    pytest tests/test_write_file_guardrails.py -v

Test inventory:
    A  — write a text file (.txt)
    B  — write a PDF (binary content)
    C  — verify SHA-256 checksum in response
    D  — verify written file visible on disk (simulates NAS list)
    E  — traversal attack in workspace_path rejected (400/403)
    F  — traversal attack via absolute workspace_path rejected
    G  — invalid extension rejected (400)
    H  — invalid zone rejected (400)
    I  — oversized file rejected (413)
    J  — target workspace does not exist → 400
    K  — duplicate detection (same SHA-256 → already_existed=True, no write)
    L  — different content at same filename → timestamp suffix, no overwrite
    M  — empty filename rejected (400)
    N  — empty workspace_path rejected (400)
    O  — null byte in workspace_path rejected (400)
    P  — null byte in filename rejected (400)
    Q  — path traversal in filename rejected (400)
    R  — response contains no credentials
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_write():
    import app.write as w
    return w


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def write_env(tmp_path: Path, monkeypatch):
    """Patch write.ROOT and write.WRITE_ZONE to use a tmp directory.

    Creates the standard workspace skeleton:
        tmp/Jobs/TakeoffAssistFiles/dev/Bids/Test_Project/Plans/
        tmp/Jobs/TakeoffAssistFiles/dev/Bids/Test_Project/Bids/
    """
    jobs_root = tmp_path / "Jobs"
    jobs_root.mkdir()
    write_zone = jobs_root / "TakeoffAssistFiles"
    write_zone.mkdir()

    workspace = write_zone / "dev" / "Bids" / "Test_Project" / "Plans"
    workspace.mkdir(parents=True)
    (write_zone / "dev" / "Bids" / "Test_Project" / "Bids").mkdir(parents=True)

    w = _get_write()
    monkeypatch.setattr(w, "ROOT", jobs_root)
    monkeypatch.setattr(w, "WRITE_ZONE", write_zone)
    monkeypatch.setattr(w, "MAX_FILE_SIZE_BYTES", 10 * 1024 * 1024)  # 10 MB for tests

    return {
        "root": jobs_root,
        "write_zone": write_zone,
        "workspace": workspace,
        "w": w,
    }


# ---------------------------------------------------------------------------
# Test A — Write a text file
# ---------------------------------------------------------------------------

def test_A_write_text_file(write_env):
    """A: Write a .txt file into an existing workspace — returns success."""
    w = write_env["w"]
    content = b"Hello from TakeoffAssist NAS-W004 Phase 1"
    result = w.write_file("dev", "Test_Project/Plans", "notes.txt", content)

    assert result["success"] is True
    assert result["already_existed"] is False
    assert result["size_bytes"] == len(content)
    assert "TakeoffAssistFiles" in result["path"]
    assert "notes.txt" in result["path"]

    target = write_env["workspace"] / "notes.txt"
    assert target.exists()
    assert target.read_bytes() == content


# ---------------------------------------------------------------------------
# Test B — Write a PDF (binary content)
# ---------------------------------------------------------------------------

def test_B_write_pdf_binary(write_env):
    """B: Write a PDF with binary content (simulated PDF header bytes)."""
    w = write_env["w"]
    pdf_content = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"
    result = w.write_file("dev", "Test_Project/Plans", "architectural_plans.pdf", pdf_content)

    assert result["success"] is True
    assert result["size_bytes"] == len(pdf_content)
    assert result["path"].endswith("architectural_plans.pdf")

    target = write_env["workspace"] / "architectural_plans.pdf"
    assert target.exists()
    assert target.read_bytes() == pdf_content


# ---------------------------------------------------------------------------
# Test C — Verify SHA-256 checksum
# ---------------------------------------------------------------------------

def test_C_verify_checksum(write_env):
    """C: SHA-256 in response must match SHA-256 of written content."""
    w = write_env["w"]
    content = b"Checksum verification content for NAS-W004"
    expected_sha256 = _sha256(content)

    result = w.write_file("dev", "Test_Project/Plans", "checksum_test.pdf", content)

    assert result["sha256"] == expected_sha256, (
        f"SHA-256 mismatch: expected {expected_sha256}, got {result['sha256']}"
    )
    assert len(result["sha256"]) == 64


# ---------------------------------------------------------------------------
# Test D — Written file is visible on disk (NAS list simulation)
# ---------------------------------------------------------------------------

def test_D_written_file_visible_on_disk(write_env):
    """D: Written file must be discoverable via filesystem listing (simulates NAS /api/v1/list)."""
    w = write_env["w"]
    content = b"Visibility test content"
    result = w.write_file("dev", "Test_Project/Bids", "itb_package.pdf", content)

    bids_dir = write_env["write_zone"] / "dev" / "Bids" / "Test_Project" / "Bids"
    files_on_disk = [f.name for f in bids_dir.iterdir() if f.is_file()]

    assert "itb_package.pdf" in files_on_disk, (
        f"Written file not found on disk. Files present: {files_on_disk}"
    )
    assert result["path"].endswith("itb_package.pdf")


# ---------------------------------------------------------------------------
# Test E — Traversal attack in workspace_path rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("malicious_path", [
    "../../../etc",
    "../../Invoices",
    "Plans/../../../WorkLoad",
    "..\\..\\Invoices",
])
def test_E_traversal_in_workspace_path_rejected(write_env, malicious_path):
    """E: Path traversal in workspace_path must be rejected with 400 or 403."""
    from fastapi import HTTPException
    w = write_env["w"]

    with pytest.raises(HTTPException) as exc_info:
        w.write_file("dev", malicious_path, "evil.txt", b"attack")

    assert exc_info.value.status_code in (400, 403), (
        f"Expected 400/403 for workspace_path='{malicious_path}', "
        f"got {exc_info.value.status_code}"
    )


# ---------------------------------------------------------------------------
# Test F — Absolute workspace_path rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("abs_path", [
    "/etc/passwd",
    "/TakeoffAssistFiles/dev/Bids/Project",
    "\\Windows\\System32",
])
def test_F_absolute_workspace_path_rejected(write_env, abs_path):
    """F: Absolute workspace_path must be rejected with 400."""
    from fastapi import HTTPException
    w = write_env["w"]

    with pytest.raises(HTTPException) as exc_info:
        w.write_file("dev", abs_path, "attack.txt", b"attack")

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Test G — Invalid extension rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_filename", [
    "malware.exe",
    "script.sh",
    "payload.bat",
    "code.py",
    "page.html",
    "app.js",
    "server.php",
    "archive.tar",
    "noextension",
])
def test_G_invalid_extension_rejected(write_env, bad_filename):
    """G: Files with disallowed extensions must be rejected with HTTP 400."""
    from fastapi import HTTPException
    w = write_env["w"]

    with pytest.raises(HTTPException) as exc_info:
        w.write_file("dev", "Test_Project/Plans", bad_filename, b"content")

    assert exc_info.value.status_code == 400, (
        f"Expected 400 for '{bad_filename}', got {exc_info.value.status_code}"
    )
    assert "not permitted" in exc_info.value.detail.lower() or "extension" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# Test H — Invalid zone rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_zone", [
    "staging",
    "production",
    "PROD",
    "DEV",
    "",
    "../prod",
    "dev/../prod",
])
def test_H_invalid_zone_rejected(write_env, bad_zone):
    """H: Any zone value other than 'dev' or 'prod' must be rejected with HTTP 400."""
    from fastapi import HTTPException
    w = write_env["w"]

    with pytest.raises(HTTPException) as exc_info:
        w.write_file(bad_zone, "Test_Project/Plans", "test.pdf", b"content")

    assert exc_info.value.status_code == 400, (
        f"Expected 400 for zone='{bad_zone}', got {exc_info.value.status_code}"
    )


# ---------------------------------------------------------------------------
# Test I — Oversized file rejected
# ---------------------------------------------------------------------------

def test_I_oversized_file_rejected(write_env, monkeypatch):
    """I: File exceeding MAX_FILE_SIZE_BYTES must be rejected with HTTP 413."""
    from fastapi import HTTPException
    w = write_env["w"]

    monkeypatch.setattr(w, "MAX_FILE_SIZE_BYTES", 100)
    oversized = b"x" * 101

    with pytest.raises(HTTPException) as exc_info:
        w.write_file("dev", "Test_Project/Plans", "oversized.pdf", oversized)

    assert exc_info.value.status_code == 413, (
        f"Expected 413 for oversized file, got {exc_info.value.status_code}"
    )


# ---------------------------------------------------------------------------
# Test J — Target workspace does not exist → 400
# ---------------------------------------------------------------------------

def test_J_missing_workspace_directory_rejected(write_env):
    """J: Writing to a workspace that has not been created must return HTTP 400."""
    from fastapi import HTTPException
    w = write_env["w"]

    with pytest.raises(HTTPException) as exc_info:
        w.write_file("dev", "NonExistent_Project/Plans", "plans.pdf", b"content")

    assert exc_info.value.status_code == 400
    assert "does not exist" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# Test K — Duplicate detection: same SHA-256 → already_existed=True
# ---------------------------------------------------------------------------

def test_K_duplicate_file_skipped(write_env):
    """K: Writing an identical file (same SHA-256) must skip write and return already_existed=True."""
    w = write_env["w"]
    content = b"Identical content for duplicate test"
    target = write_env["workspace"] / "duplicate.pdf"

    result1 = w.write_file("dev", "Test_Project/Plans", "duplicate.pdf", content)
    assert result1["already_existed"] is False

    mtime_before = target.stat().st_mtime

    result2 = w.write_file("dev", "Test_Project/Plans", "duplicate.pdf", content)
    assert result2["success"] is True
    assert result2["already_existed"] is True
    assert result2["sha256"] == _sha256(content)

    assert target.stat().st_mtime == mtime_before, "File was re-written when it should have been skipped"


# ---------------------------------------------------------------------------
# Test L — Different content at same filename → timestamp suffix written
# ---------------------------------------------------------------------------

def test_L_different_content_writes_with_suffix(write_env):
    """L: Writing different content to an existing filename must create a new file with a timestamp suffix."""
    w = write_env["w"]
    content_v1 = b"Version 1 of this plan"
    content_v2 = b"Version 2 of this plan - updated scope"

    result1 = w.write_file("dev", "Test_Project/Plans", "scope.pdf", content_v1)
    assert result1["already_existed"] is False

    result2 = w.write_file("dev", "Test_Project/Plans", "scope.pdf", content_v2)
    assert result2["success"] is True
    assert result2["already_existed"] is False
    assert "scope_" in result2["path"]

    original = write_env["workspace"] / "scope.pdf"
    assert original.exists()
    assert original.read_bytes() == content_v1, "Original file must not be overwritten"

    files = [f.name for f in write_env["workspace"].iterdir() if f.is_file()]
    new_files = [f for f in files if f.startswith("scope_") and f.endswith(".pdf")]
    assert len(new_files) == 1, f"Expected exactly one timestamped file, found: {new_files}"
    new_file_path = write_env["workspace"] / new_files[0]
    assert new_file_path.read_bytes() == content_v2


# ---------------------------------------------------------------------------
# Test M — Empty filename rejected
# ---------------------------------------------------------------------------

def test_M_empty_filename_rejected(write_env):
    """M: Empty filename must return HTTP 400."""
    from fastapi import HTTPException
    w = write_env["w"]

    with pytest.raises(HTTPException) as exc_info:
        w.write_file("dev", "Test_Project/Plans", "", b"content")

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Test N — Empty workspace_path rejected
# ---------------------------------------------------------------------------

def test_N_empty_workspace_path_rejected(write_env):
    """N: Empty workspace_path must return HTTP 400."""
    from fastapi import HTTPException
    w = write_env["w"]

    with pytest.raises(HTTPException) as exc_info:
        w.write_file("dev", "", "file.pdf", b"content")

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Test O — Null byte in workspace_path rejected
# ---------------------------------------------------------------------------

def test_O_null_byte_in_workspace_path_rejected(write_env):
    """O: Null byte in workspace_path must return HTTP 400."""
    from fastapi import HTTPException
    w = write_env["w"]

    with pytest.raises(HTTPException) as exc_info:
        w.write_file("dev", "Test_Project/\x00Plans", "file.pdf", b"content")

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Test P — Null byte in filename rejected
# ---------------------------------------------------------------------------

def test_P_null_byte_in_filename_rejected(write_env):
    """P: Null byte in filename must return HTTP 400."""
    from fastapi import HTTPException
    w = write_env["w"]

    with pytest.raises(HTTPException) as exc_info:
        w.write_file("dev", "Test_Project/Plans", "fi\x00le.pdf", b"content")

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Test Q — Path traversal in filename rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_filename", [
    "../escape.pdf",
    "sub/dir/file.pdf",
    "\\..\\escape.pdf",
])
def test_Q_traversal_in_filename_rejected(write_env, bad_filename):
    """Q: Path separators or traversal in filename must return HTTP 400."""
    from fastapi import HTTPException
    w = write_env["w"]

    with pytest.raises(HTTPException) as exc_info:
        w.write_file("dev", "Test_Project/Plans", bad_filename, b"content")

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Test R — Response contains no credentials
# ---------------------------------------------------------------------------

def test_R_response_contains_no_credentials(write_env):
    """R: write_file response must not contain the BRIDGE_TOKEN or any secret."""
    w = write_env["w"]
    content = b"Credential check content"
    result = w.write_file("dev", "Test_Project/Plans", "cred_check.txt", content)

    response_str = str(result)
    bridge_token = os.environ.get("BRIDGE_TOKEN", "test-token-for-unit-tests")
    if bridge_token:
        assert bridge_token not in response_str, "BRIDGE_TOKEN found in write_file response!"

    # Phase 3 (NAS-W004): manifest_updated added to response
    assert set(result.keys()) == {"success", "path", "size_bytes", "sha256", "already_existed", "manifest_updated"}


# ---------------------------------------------------------------------------
# Additional: prod zone accepted
# ---------------------------------------------------------------------------

def test_prod_zone_accepted(write_env):
    """'prod' zone is accepted when the target path exists."""
    w = write_env["w"]
    prod_workspace = (
        write_env["write_zone"] / "prod" / "Bids" / "Prod_Project" / "Plans"
    )
    prod_workspace.mkdir(parents=True)

    content = b"Production file content"
    result = w.write_file("prod", "Prod_Project/Plans", "prod_plans.pdf", content)

    assert result["success"] is True
    assert "/prod/" in result["path"]


# ---------------------------------------------------------------------------
# Additional: allowed extensions all accepted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ext", [
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv",
    ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".txt", ".rtf", ".zip",
])
def test_all_allowed_extensions_accepted(write_env, ext):
    """Every extension on the allowlist must be accepted without error."""
    w = write_env["w"]
    filename = f"test_file{ext}"
    content = b"Extension acceptance test"
    result = w.write_file("dev", "Test_Project/Plans", filename, content)
    assert result["success"] is True
    assert result["path"].endswith(filename)
