"""API endpoint decorators for customizing route generation.

These decorators allow Event classes to override the default convention-based
routing with custom paths, methods, tags, and permissions.
"""

from __future__ import annotations

from typing import Any

from .conventions import HTTPMethod

# Attribute names used to store decorator metadata on Event classes
_API_METADATA_ATTR = "_api_endpoint_metadata"
_API_EXCLUDED_ATTR = "_api_excluded"


def api_endpoint(
    *,
    path: str | None = None,
    method: str | HTTPMethod | None = None,
    tags: list[str] | None = None,
    summary: str | None = None,
    description: str | None = None,
    deprecated: bool = False,
    include_in_schema: bool = True,
    required_permissions: list[str] | None = None,
    public: bool = False,
    response_model_exclude_none: bool = True,
) -> Any:
    """Decorator to customize API endpoint generation for an Event class.

    Use this decorator when the convention-based routing doesn't fit your needs.

    Args:
        path: Custom REST path (e.g., "/users/merge")
        method: HTTP method override (GET, POST, PUT, DELETE, PATCH)
        tags: OpenAPI tags for grouping endpoints
        summary: Short summary for OpenAPI docs
        description: Detailed description for OpenAPI docs
        deprecated: Mark endpoint as deprecated
        include_in_schema: Include in OpenAPI schema (set False to hide)
        required_permissions: Permissions required to access this endpoint
        public: If True, endpoint doesn't require authentication
        response_model_exclude_none: Exclude None values from response

    Example:
        @api_endpoint(
            path="/users/merge",
            method="POST",
            tags=["Users", "Advanced"],
            required_permissions=["users:write"],
        )
        class MergeUsersEvent(Event[MergeUsersInput, MergeUsersOutput]):
            name = "user.merge"
    """

    def decorator(cls: type) -> type:
        method_value = method
        if isinstance(method, HTTPMethod):
            method_value = method.value
        elif isinstance(method, str):
            method_value = method.upper()

        metadata = {
            "path": path,
            "method": method_value,
            "tags": tags,
            "summary": summary,
            "description": description,
            "deprecated": deprecated,
            "include_in_schema": include_in_schema,
            "required_permissions": required_permissions or [],
            "public": public,
            "response_model_exclude_none": response_model_exclude_none,
        }
        setattr(cls, _API_METADATA_ATTR, metadata)
        return cls

    return decorator


def exclude_from_api(cls: type) -> type:
    """Decorator to exclude an Event class from API generation.

    Use this for internal events that should not be exposed via the REST API.

    Example:
        @exclude_from_api
        class UserIndexUpdateEvent(Event[IndexUpdateInput, IndexUpdateOutput]):
            name = "user.index_update"
    """
    setattr(cls, _API_EXCLUDED_ATTR, True)
    return cls


def get_api_metadata(cls: type) -> dict[str, Any]:
    """Get API metadata from an Event class if decorated.

    Args:
        cls: Event class to check

    Returns:
        Dictionary of API metadata or empty dict if not decorated
    """
    return getattr(cls, _API_METADATA_ATTR, {})


def is_excluded_from_api(cls: type) -> bool:
    """Check if an Event class is excluded from API generation.

    Args:
        cls: Event class to check

    Returns:
        True if the class should be excluded from API
    """
    return getattr(cls, _API_EXCLUDED_ATTR, False)
