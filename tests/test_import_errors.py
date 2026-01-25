"""Tests for optional dependency import error messages."""

import sys
from unittest.mock import patch


def test_require_optional_dependency_raises_clear_error():
    """Verify the helper gives clear error messages."""
    from pymodules._imports import require_optional_dependency

    # Mock a missing module
    with patch.dict(sys.modules, {"nonexistent_module": None}):
        try:
            require_optional_dependency("nonexistent_module", "pymodules.test", "test")
            assert False, "Should have raised ImportError"
        except ImportError as e:
            assert "pip install pymodules[test]" in str(e)
            assert "nonexistent_module" in str(e)


def test_require_optional_dependency_succeeds_when_installed():
    """Verify the helper passes when module is available."""
    from pymodules._imports import require_optional_dependency

    # This should not raise - os is always available
    require_optional_dependency("os", "pymodules.core", "core")
