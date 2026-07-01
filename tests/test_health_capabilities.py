"""PACKAGE-CURATION-006B — /health advertises endpoint capabilities.

The API server and NAS Browser detect whether the *deployed* bridge supports
newer endpoints (e.g. mkdir-path) by reading /health's ``capabilities`` map. An
older deployed bridge omits the field entirely, which callers read as "missing"
and surface as a "redeploy required" prompt.

conftest.py supplies BRIDGE_TOKEN + ROOT_PATH=/tmp and puts nas-bridge/ on the
path, so app.main imports cleanly here.

Run from the nas-bridge/ directory:
    pytest tests/test_health_capabilities.py -v
"""
from __future__ import annotations


def test_health_advertises_mkdir_path_capability():
    from app.main import health

    resp = health()
    assert resp.capabilities.get("mkdir_path") is True


def test_health_reports_a_version():
    from app.main import health

    assert health().version


def test_health_response_capabilities_default_is_empty():
    """An older-style construction (no capabilities) yields an empty map, so the
    field is additive and never breaks existing bridge responses/tests."""
    from app.models import HealthResponse

    hr = HealthResponse(
        status="healthy",
        root_path="/x",
        root_exists=True,
        root_readable=True,
        version="1.5.0",
        timestamp="t",
    )
    assert hr.capabilities == {}
