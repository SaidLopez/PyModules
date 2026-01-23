"""REST API generation layer for PyModules.

This module provides API utilities including:
- ModuleRouter: Auto-discovery router for Event classes
- EventDiscovery: Scans packages for Event classes
- RouteConvention: Convention-based REST path generation
- Decorators: @api_endpoint, @exclude_from_api
- Errors: APIError hierarchy and error handlers

Example:
    from pymodules import ModuleHost
    from pymodules.api import ModuleRouter, register_error_handlers
    from fastapi import FastAPI

    host = ModuleHost()
    # ... register modules

    app = FastAPI()
    register_error_handlers(app)

    router = ModuleRouter(host)
    router.discover_events("myapp.events")
    router.mount(app)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pymodules._imports import require_optional_dependency

# Check for FastAPI dependency at import time
require_optional_dependency("fastapi", "pymodules.api", "api")

if TYPE_CHECKING:
    from .conventions import HTTPMethod, RESTConvention, RouteConvention, RouteInfo
    from .decorators import api_endpoint, exclude_from_api, get_api_metadata, is_excluded_from_api
    from .discovery import DiscoveredEvent, EventDiscovery
    from .errors import (
        APIError,
        AuthenticationError,
        AuthorizationError,
        ErrorCode,
        NotFoundError,
        ValidationError,
        register_error_handlers,
    )
    from .router import ModuleRouter

__all__ = [
    # Router
    "ModuleRouter",
    # Discovery
    "DiscoveredEvent",
    "EventDiscovery",
    # Conventions
    "HTTPMethod",
    "RouteConvention",
    "RESTConvention",
    "RouteInfo",
    # Decorators
    "api_endpoint",
    "exclude_from_api",
    "get_api_metadata",
    "is_excluded_from_api",
    # Errors
    "APIError",
    "AuthenticationError",
    "AuthorizationError",
    "ErrorCode",
    "NotFoundError",
    "ValidationError",
    "register_error_handlers",
]


def __getattr__(name: str):
    """Lazy load API components."""
    if name == "ModuleRouter":
        from .router import ModuleRouter

        return ModuleRouter
    elif name in ("DiscoveredEvent", "EventDiscovery"):
        from . import discovery

        return getattr(discovery, name)
    elif name in ("HTTPMethod", "RouteConvention", "RESTConvention", "RouteInfo"):
        from . import conventions

        return getattr(conventions, name)
    elif name in ("api_endpoint", "exclude_from_api", "get_api_metadata", "is_excluded_from_api"):
        from . import decorators

        return getattr(decorators, name)
    elif name in (
        "APIError",
        "AuthenticationError",
        "AuthorizationError",
        "ErrorCode",
        "NotFoundError",
        "ValidationError",
        "register_error_handlers",
    ):
        from . import errors

        return getattr(errors, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
