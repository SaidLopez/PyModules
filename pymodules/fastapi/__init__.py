"""
FastAPI integration for PyModules.

.. deprecated:: 0.4.0
    This module is deprecated. Use ``pymodules.api`` instead for the new
    auto-discovery router and ``pymodules.api.auth`` for authentication.

The ``pymodules.fastapi`` module provides backward compatibility but new code
should use the improved ``pymodules.api`` module which offers:
- ModuleRouter with auto-discovery
- Convention-based REST routing
- Pluggable authentication
- Better error handling

Example migration:
    # Old way (deprecated)
    from pymodules.fastapi import PyModulesAPI

    # New way (recommended)
    from pymodules.api import ModuleRouter
"""

import warnings

from .integration import PyModulesAPI, event_endpoint

__all__ = [
    # Legacy exports
    "PyModulesAPI",
    "event_endpoint",
    # New API re-exports (with deprecation)
    "ModuleRouter",
    "EventDiscovery",
    "api_endpoint",
    "exclude_from_api",
    "APIError",
    "NotFoundError",
    "register_error_handlers",
]


def __getattr__(name: str):
    """Provide deprecation warnings when accessing new API components via old path."""
    # Re-export from pymodules.api with deprecation warning
    if name in (
        "ModuleRouter",
        "EventDiscovery",
        "api_endpoint",
        "exclude_from_api",
        "APIError",
        "NotFoundError",
        "register_error_handlers",
    ):
        warnings.warn(
            f"Importing {name} from pymodules.fastapi is deprecated. "
            f"Use 'from pymodules.api import {name}' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        from pymodules import api

        return getattr(api, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
