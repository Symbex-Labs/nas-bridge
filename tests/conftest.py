"""Test configuration for NAS bridge unit tests.

Sets up sys.path so `from app.xxx import ...` resolves to nas-bridge/app/,
and provides a dummy BRIDGE_TOKEN env var so config.Settings() can initialise
without a real .env file.
"""
import os
import sys
from pathlib import Path

# nas-bridge/ is the parent of this tests/ directory
_BRIDGE_ROOT = str(Path(__file__).parent.parent)
if _BRIDGE_ROOT not in sys.path:
    sys.path.insert(0, _BRIDGE_ROOT)

# Settings() requires BRIDGE_TOKEN; supply a dummy value for tests
os.environ.setdefault("BRIDGE_TOKEN", "test-token-for-unit-tests")
# ROOT_PATH must also exist; use /tmp as a safe default (overridden per-test)
os.environ.setdefault("ROOT_PATH", "/tmp")
