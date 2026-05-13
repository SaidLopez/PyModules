"""REST API generation layer for PyModules.

This module exposes:

- ``ModuleRouter``: maps ``@api_endpoint``-decorated Commands to FastAPI routes
- ``CommandDiscovery`` / ``DiscoveredCommand``: scan a package for Commands
- ``@api_endpoint`` / ``@exclude_from_api``: declare REST routes explicitly
- ``HTTPMethod``: typed sugar for ``method=`` on ``@api_endpoint``
- ``APIError`` hierarchy and FastAPI error handlers

Routing is explicit: there is no class-name-to-URL convention (see CONTEXT.md,
non-goal #1). Every endpoint declares its method and path on the Command class.

Example:
    from fastapi import FastAPI
    from pymodules import ModuleHost
    from pymodules.contrib.api import (
        ModuleRouter,
        api_endpoint,
        register_error_handlers,
    )

    @api_endpoint(method="POST", path="/users")
    class CreateUser(Command[CreateUserRequest, CreateUserResponse]):
        pass

    host = ModuleHost()
    host.register(UserModule())

    app = FastAPI()
    register_error_handlers(app)

    router = ModuleRouter(host)
    router.register_command(CreateUser)
    router.mount(app)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pymodules._imports import require_optional_dependency

# Check for FastAPI dependency at import time
require_optional_dependency("fastapi", "pymodules.contrib.api", "api")

if TYPE_CHECKING:
    from .decorators import (
        HTTPMethod,
        api_endpoint,
        exclude_from_api,
        get_api_metadata,
        is_excluded_from_api,
    )
    from .discovery import CommandDiscovery, DiscoveredCommand
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
    "DiscoveredCommand",
    "CommandDiscovery",
    # Decorators
    "HTTPMethod",
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
    elif name in ("DiscoveredCommand", "CommandDiscovery"):
        from . import discovery

        return getattr(discovery, name)
    elif name in (
        "HTTPMethod",
        "api_endpoint",
        "exclude_from_api",
        "get_api_metadata",
        "is_excluded_from_api",
    ):
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
