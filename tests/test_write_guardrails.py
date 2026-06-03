"""NAS-W001 Phase 4 — Write guardrail unit tests (Tests A–E).

These tests run against a temporary directory so they do not require a live
NAS or Docker container.  The write_test module is monkeypatched so that
ROOT and WRITE_ZONE point to tmp paths.

Run from the nas-bridge/ directory:
    pip install fastapi pydantic pydantic-settings pytest
    pytest tests/test_write_guardrails.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _get_wt():
    """Return a fresh import of app.write_test (conftest.py adds nas-bridge/ to sys.path)."""
    import app.write_test as wt  # noqa: PLC0415
    return wt


# ---------------------------------------------------------------------------
# Fixtures — patch write_test module to use a temp directory as ROOT/WRITE_ZONE
# ---------------------------------------------------------------------------

@pytest.fixture()
def write_env(tmp_path: Path, monkeypatch):
    """Patch write_test.ROOT and WRITE_ZONE to point at tmp_path/Jobs."""
    jobs_root = tmp_path / "Jobs"
    jobs_root.mkdir()
    write_zone = jobs_root / "TakeoffAssistFiles"
    write_zone.mkdir()

    for protected in ("Bids", "Invoices", "WorkLoad"):
        (jobs_root / protected).mkdir()

    wt = _get_wt()
    monkeypatch.setattr(wt, "ROOT", jobs_root)
    monkeypatch.setattr(wt, "WRITE_ZONE", write_zone)
    return {"root": jobs_root, "write_zone": write_zone, "wt": wt}


# ---------------------------------------------------------------------------
# Test A — Write smoke-test.txt
# ---------------------------------------------------------------------------

def test_A_write_smoke_test(write_env):
    """Test A: Write smoke-test.txt to TakeoffAssistFiles."""
    wt = write_env["wt"]
    result = wt.write_and_verify("smoke-test.txt", "TakeoffAssist write test")

    assert result["success"] is True
    assert result["bytes_written"] > 0
    assert result["read_back_verified"] is True
    assert "TakeoffAssistFiles" in result["path"]
    assert "smoke-test.txt" in result["path"]


# ---------------------------------------------------------------------------
# Test B — Read back contents
# ---------------------------------------------------------------------------

def test_B_read_back_verified(write_env):
    """Test B: Verify read_back_verified is True and content matches."""
    wt = write_env["wt"]
    content = "NAS-W001 read-back verification"
    result = wt.write_and_verify("read-back-test.txt", content)

    assert result["read_back_verified"] is True
    assert result["bytes_written"] == len(content.encode("utf-8"))

    target = write_env["write_zone"] / "read-back-test.txt"
    assert target.exists()
    assert target.read_text("utf-8") == content


# ---------------------------------------------------------------------------
# Test C — Attempt write to Bids / Invoices / WorkLoad → expect 403
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("protected_zone", ["Bids", "Invoices", "WorkLoad"])
def test_C_protected_zone_rejected(write_env, protected_zone):
    """Test C: Write to protected zones must return HTTP 403."""
    from fastapi import HTTPException
    wt = write_env["wt"]

    with pytest.raises(HTTPException) as exc_info:
        wt.write_and_verify(protected_zone, "should be rejected")

    assert exc_info.value.status_code == 403, (
        f"Expected 403 for write to {protected_zone}, got {exc_info.value.status_code}"
    )


# ---------------------------------------------------------------------------
# Test D — Path traversal attacks → expect 400 or 403
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("malicious_filename", [
    "../Bids/pwned.txt",
    "../../etc/passwd",
    "../WorkLoad/evil.xls",
    "..\\Invoices\\pwned.txt",
    "/absolute/path",
    "sub/dir/file.txt",
    "\x00null.txt",
])
def test_D_traversal_rejected(write_env, malicious_filename):
    """Test D: Path traversal and injection attempts must be rejected with 400 or 403."""
    from fastapi import HTTPException
    wt = write_env["wt"]

    with pytest.raises(HTTPException) as exc_info:
        wt.write_and_verify(malicious_filename, "traversal attempt")

    assert exc_info.value.status_code in (400, 403), (
        f"Expected 400 or 403 for '{malicious_filename}', "
        f"got {exc_info.value.status_code}: {exc_info.value.detail}"
    )


# ---------------------------------------------------------------------------
# Test E — Write response contains no credentials
# ---------------------------------------------------------------------------

def test_E_token_not_in_response(write_env):
    """Test E: The write_and_verify response must not contain any credential."""
    import os
    wt = write_env["wt"]

    result = wt.write_and_verify("token-check.txt", "checking for token leakage")
    response_str = str(result)

    bridge_token = os.environ.get("NAS_BRIDGE_TOKEN", "")
    if bridge_token:
        assert bridge_token not in response_str, "NAS_BRIDGE_TOKEN found in response!"

    assert set(result.keys()) == {"success", "bytes_written", "read_back_verified", "path"}


# ---------------------------------------------------------------------------
# Extra: empty filename → 400
# ---------------------------------------------------------------------------

def test_empty_filename_rejected(write_env):
    """An empty filename must return HTTP 400."""
    from fastapi import HTTPException
    wt = write_env["wt"]

    with pytest.raises(HTTPException) as exc_info:
        wt.write_and_verify("", "contents")

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Extra: write zone auto-created if absent
# ---------------------------------------------------------------------------

def test_write_zone_autocreated(tmp_path, monkeypatch):
    """write_and_verify creates TakeoffAssistFiles/ if it does not yet exist."""
    jobs_root = tmp_path / "Jobs"
    jobs_root.mkdir()
    write_zone = jobs_root / "TakeoffAssistFiles"

    wt = _get_wt()
    monkeypatch.setattr(wt, "ROOT", jobs_root)
    monkeypatch.setattr(wt, "WRITE_ZONE", write_zone)

    result = wt.write_and_verify("autocreate-test.txt", "zone auto-created")
    assert result["success"] is True
    assert write_zone.exists()
