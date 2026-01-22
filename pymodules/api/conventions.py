"""Route convention engine.

Maps Event names to HTTP methods and REST paths using conventions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pymodules import Event

    from .discovery import DiscoveredEvent


class HTTPMethod(str, Enum):
    """HTTP methods for REST endpoints."""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


# Default action to HTTP method mappings
ACTION_METHOD_MAP: dict[str, HTTPMethod] = {
    # CRUD operations
    "create": HTTPMethod.POST,
    "get": HTTPMethod.GET,
    "update": HTTPMethod.PUT,
    "patch": HTTPMethod.PATCH,
    "delete": HTTPMethod.DELETE,
    "list": HTTPMethod.GET,
    # Search and query
    "search": HTTPMethod.POST,
    "find": HTTPMethod.GET,
    "query": HTTPMethod.POST,
    # Bulk operations
    "bulk_create": HTTPMethod.POST,
    "bulk_update": HTTPMethod.PUT,
    "bulk_delete": HTTPMethod.DELETE,
    # Import/Export
    "import": HTTPMethod.POST,
    "export": HTTPMethod.POST,
    # State changes
    "refresh": HTTPMethod.POST,
    "start": HTTPMethod.POST,
    "stop": HTTPMethod.POST,
    "pause": HTTPMethod.POST,
    "resume": HTTPMethod.POST,
    "launch": HTTPMethod.POST,
    "complete": HTTPMethod.POST,
    "cancel": HTTPMethod.POST,
    "close": HTTPMethod.POST,
    "move": HTTPMethod.POST,
    "archive": HTTPMethod.POST,
    "restore": HTTPMethod.POST,
    "activate": HTTPMethod.POST,
    "deactivate": HTTPMethod.POST,
    # Relationship operations
    "get_members": HTTPMethod.GET,
    "add_member": HTTPMethod.POST,
    "remove_member": HTTPMethod.DELETE,
    "add_tags": HTTPMethod.POST,
    "remove_tags": HTTPMethod.DELETE,
    # Auth operations
    "register": HTTPMethod.POST,
    "authenticate": HTTPMethod.POST,
    "login": HTTPMethod.POST,
    "logout": HTTPMethod.POST,
    "refresh_token": HTTPMethod.POST,
    "change_password": HTTPMethod.POST,
    "reset_password": HTTPMethod.POST,
    # Test/verification
    "test": HTTPMethod.POST,
    "verify": HTTPMethod.POST,
    "validate": HTTPMethod.POST,
}

# Actions that require an ID in the path
ACTIONS_REQUIRING_ID: set[str] = {
    "get",
    "update",
    "patch",
    "delete",
    "refresh",
    "start",
    "stop",
    "pause",
    "resume",
    "launch",
    "complete",
    "cancel",
    "close",
    "move",
    "archive",
    "restore",
    "activate",
    "deactivate",
    "get_members",
    "add_member",
    "remove_member",
    "add_tags",
    "remove_tags",
    "test",
}

# Actions that get a subpath (action name becomes part of URL)
ACTIONS_WITH_SUBPATH: set[str] = {
    "search",
    "bulk_create",
    "bulk_update",
    "bulk_delete",
    "import",
    "export",
    "refresh",
    "start",
    "stop",
    "pause",
    "resume",
    "launch",
    "complete",
    "cancel",
    "close",
    "move",
    "archive",
    "restore",
    "activate",
    "deactivate",
    "get_members",
    "add_member",
    "remove_member",
    "add_tags",
    "remove_tags",
    "test",
    "verify",
    "validate",
    "register",
    "authenticate",
    "login",
    "logout",
    "refresh_token",
    "change_password",
    "reset_password",
}


@dataclass
class RouteInfo:
    """Information about a generated route."""

    path: str
    method: HTTPMethod
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    deprecated: bool = False
    requires_id: bool = False
    id_param_name: str = "id"


class RouteConvention:
    """Base route convention class.

    Override this class to implement custom routing conventions.
    """

    def get_route(self, event_class: type[Event[Any, Any]]) -> RouteInfo:
        """Get route info for an event class.

        Args:
            event_class: Event class to generate route for

        Returns:
            RouteInfo with path, method, and metadata
        """
        # Parse event name
        event_name = getattr(event_class, "name", "") or event_class.__name__
        if "." in event_name:
            domain, action = event_name.split(".", 1)
        else:
            # Try to parse from class name
            class_name = event_class.__name__
            # e.g., CreateUser -> create, user
            action = ""
            domain = class_name.lower()
            for prefix in ["create", "get", "update", "delete", "list", "search"]:
                if class_name.lower().startswith(prefix):
                    action = prefix
                    domain = class_name[len(prefix):].lower()
                    break

        domain_plural = self._pluralize(domain)
        method = ACTION_METHOD_MAP.get(action, HTTPMethod.POST)

        # Build path
        if action in ("create", "list"):
            path = f"/{domain_plural}"
        elif action in ("get", "update", "delete"):
            path = f"/{domain_plural}/{{id}}"
        elif action in ACTIONS_WITH_SUBPATH:
            if action in ACTIONS_REQUIRING_ID:
                subpath = action.replace("_", "-")
                path = f"/{domain_plural}/{{id}}/{subpath}"
            else:
                subpath = action.replace("_", "-")
                path = f"/{domain_plural}/{subpath}"
        else:
            path = f"/{domain_plural}/{action.replace('_', '-')}"

        return RouteInfo(
            path=path,
            method=method,
            tags=[domain.replace("_", " ").title()],
            summary=f"{action.replace('_', ' ').title()} {domain.replace('_', ' ').title()}",
            requires_id=action in ACTIONS_REQUIRING_ID,
            id_param_name=f"{domain}_id" if f"{domain}_id" in str(event_class) else "id",
        )

    def _pluralize(self, singular: str) -> str:
        """Convert a singular noun to plural."""
        if singular.endswith("y") and len(singular) > 1 and singular[-2] not in "aeiou":
            return singular[:-1] + "ies"
        elif singular.endswith(("s", "x", "z", "ch", "sh")):
            return singular + "es"
        elif singular.endswith("f"):
            return singular[:-1] + "ves"
        elif singular.endswith("fe"):
            return singular[:-2] + "ves"
        else:
            return singular + "s"


class RESTConvention(RouteConvention):
    """RESTful convention-based route generator.

    Converts event names like "contact.create" into REST paths like "POST /contacts".

    Convention Rules:
    - Domain names are pluralized (contact -> contacts)
    - "create" -> POST /domains
    - "get" -> GET /domains/{id}
    - "update" -> PUT /domains/{id}
    - "delete" -> DELETE /domains/{id}
    - "list" -> GET /domains
    - "search" -> POST /domains/search
    - "bulk_*" -> method /domains/bulk
    - State changes (start, stop, etc.) -> POST /domains/{id}/action

    Example:
        convention = RESTConvention()
        route = convention.get_route(CreateUserEvent)
        print(f"{route.method} {route.path}")
    """

    def __init__(
        self,
        custom_plurals: dict[str, str] | None = None,
        path_prefix: str = "",
    ):
        """Initialize the route convention.

        Args:
            custom_plurals: Custom singular->plural mappings
            path_prefix: Prefix to add to all paths
        """
        self._custom_plurals = custom_plurals or {}
        self._path_prefix = path_prefix.rstrip("/")

    def get_route(self, event_class: type[Event[Any, Any]]) -> RouteInfo:
        """Generate route info from an event class.

        Args:
            event_class: Event class to generate route for

        Returns:
            RouteInfo with path, method, and metadata
        """
        from .decorators import get_api_metadata

        # Parse event name
        event_name = getattr(event_class, "name", "") or ""
        if not event_name or "." not in event_name:
            # Fall back to class name parsing
            class_name = event_class.__name__
            domain = ""
            action = ""
            for prefix in ["create", "get", "update", "delete", "list", "search", "activate"]:
                if class_name.lower().startswith(prefix):
                    action = prefix
                    domain = class_name[len(prefix):].lower()
                    break
            if not domain:
                domain = class_name.lower()
                action = "get"
        else:
            parts = event_name.split(".", 1)
            domain = parts[0]
            action = parts[1] if len(parts) > 1 else ""

        # Check for custom API metadata
        api_meta = get_api_metadata(event_class)
        if api_meta:
            custom_path = api_meta.get("path")
            custom_method = api_meta.get("method")
            if custom_path or custom_method:
                return self._create_custom_route(domain, action, api_meta, event_class)

        # Use convention-based routing
        return self._create_convention_route(domain, action, event_class)

    def _create_custom_route(
        self, domain: str, action: str, api_meta: dict, event_class: type
    ) -> RouteInfo:
        """Create a route from custom API metadata."""
        path = api_meta.get("path") or self._build_path(domain, action)
        method_str = api_meta.get("method") or self._get_method(action)
        method = HTTPMethod(method_str) if isinstance(method_str, str) else method_str

        tags = api_meta.get("tags") or [self._format_tag(domain)]
        summary = api_meta.get("summary") or self._generate_summary(domain, action)

        return RouteInfo(
            path=self._path_prefix + path,
            method=method,
            tags=tags,
            summary=summary,
            deprecated=api_meta.get("deprecated", False),
            requires_id="{id}" in path or f"{{{domain}_id}}" in path,
            id_param_name=self._get_id_param_name(domain, event_class),
        )

    def _create_convention_route(
        self, domain: str, action: str, event_class: type
    ) -> RouteInfo:
        """Create a route using conventions."""
        path = self._build_path(domain, action)
        method = self._get_method(action)

        return RouteInfo(
            path=self._path_prefix + path,
            method=method,
            tags=[self._format_tag(domain)],
            summary=self._generate_summary(domain, action),
            deprecated=False,
            requires_id=action in ACTIONS_REQUIRING_ID,
            id_param_name=self._get_id_param_name(domain, event_class),
        )

    def _build_path(self, domain: str, action: str) -> str:
        """Build the REST path for an event."""
        domain_plural = self._pluralize(domain)

        # Base path is always the pluralized domain
        base_path = f"/{domain_plural}"

        # Determine if we need an ID in the path
        needs_id = action in ACTIONS_REQUIRING_ID

        # Determine if we need a subpath (action name in URL)
        needs_subpath = action in ACTIONS_WITH_SUBPATH

        # Handle special actions
        if action in ("create", "list"):
            return base_path
        elif action in ("get", "update", "delete"):
            return f"{base_path}/{{id}}"
        elif action.startswith("bulk_"):
            return f"{base_path}/bulk"
        elif needs_id and needs_subpath:
            subpath = self._action_to_subpath(action)
            return f"{base_path}/{{id}}/{subpath}"
        elif needs_subpath:
            subpath = self._action_to_subpath(action)
            return f"{base_path}/{subpath}"
        elif needs_id:
            return f"{base_path}/{{id}}"
        else:
            # Default: action becomes subpath
            subpath = self._action_to_subpath(action)
            return f"{base_path}/{subpath}"

    def _get_method(self, action: str) -> HTTPMethod:
        """Get HTTP method for an action."""
        return ACTION_METHOD_MAP.get(action, HTTPMethod.POST)

    def _pluralize(self, singular: str) -> str:
        """Convert a singular noun to plural."""
        if singular in self._custom_plurals:
            return self._custom_plurals[singular]

        # Common English pluralization rules
        if singular.endswith("y") and len(singular) > 1 and singular[-2] not in "aeiou":
            return singular[:-1] + "ies"
        elif singular.endswith(("s", "x", "z", "ch", "sh")):
            return singular + "es"
        elif singular.endswith("f"):
            return singular[:-1] + "ves"
        elif singular.endswith("fe"):
            return singular[:-2] + "ves"
        else:
            return singular + "s"

    def _action_to_subpath(self, action: str) -> str:
        """Convert an action name to a URL subpath."""
        # Convert snake_case to kebab-case
        return action.replace("_", "-")

    def _format_tag(self, domain: str) -> str:
        """Format a domain name as an OpenAPI tag."""
        return domain.replace("_", " ").title()

    def _generate_summary(self, domain: str, action: str) -> str:
        """Generate an OpenAPI summary from event metadata."""
        action_title = action.replace("_", " ").title()
        domain_title = domain.replace("_", " ").title()

        if action in ("create", "list", "search"):
            return f"{action_title} {domain_title}s"
        else:
            return f"{action_title} {domain_title}"

    def _get_id_param_name(self, domain: str, event_class: type) -> str:
        """Get the ID parameter name for this event."""
        # Check if input class has a domain-specific ID field
        # This is a simplified version - actual implementation would inspect type hints
        domain_id = f"{domain}_id"
        return domain_id if domain_id in str(event_class) else "id"
