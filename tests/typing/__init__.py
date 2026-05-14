"""Type-level assertion tests for PyModules' narrow Protocols.

The files in this package are deliberately split between:

- Static-analysis fixtures (``test_*_type.py``) that use
  ``typing.assert_type`` / ``reveal_type`` and are *meant* to be passed
  through mypy or pyright. They contain no runtime assertions; pytest
  collects them so they are exercised at import time (catches syntax
  drift and stale imports), but the real assertion is the type checker
  succeeding without errors.
- Runtime sibling files (``test_*_runtime.py``) that introspect the
  Protocol's declared surface via ``inspect`` so the same narrowness
  invariant is enforced by pytest alone, without requiring mypy in CI.
"""
