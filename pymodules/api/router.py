"""ModuleRouter with auto-discovery.

Provides ModuleRouter that automatically discovers Event classes and creates
FastAPI endpoints using convention-based routing.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Body, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, create_model

from pymodules import Event, EventInput, ModuleHost

from .conventions import HTTPMethod, RESTConvention, RouteConvention, RouteInfo
from .decorators import is_excluded_from_api
from .discovery import DiscoveredEvent, EventDiscovery
from .errors import APIError, NotFoundError

T = TypeVar("T")


def _dataclass_to_pydantic(dc_class: type) -> type[BaseModel]:
    """Convert a dataclass to a Pydantic model for request body validation.

    Args:
        dc_class: Dataclass class to convert

    Returns:
        Pydantic model class
    """
    if not dataclasses.is_dataclass(dc_class):
        raise TypeError(f"{dc_class} is not a dataclass")

    fields: dict[str, Any] = {}
    for field in dataclasses.fields(dc_class):
        field_type = field.type
        default = field.default if field.default is not dataclasses.MISSING else ...
        if field.default_factory is not dataclasses.MISSING:
            default = field.default_factory()
        fields[field.name] = (field_type, default)

    model_name = f"{dc_class.__name__}Model"
    return create_model(model_name, **fields)


def _pydantic_to_dataclass(model: BaseModel, dc_class: type[T]) -> T:
    """Convert a Pydantic model instance to a dataclass instance.

    Args:
        model: Pydantic model instance
        dc_class: Target dataclass class

    Returns:
        Dataclass instance
    """
    return dc_class(**model.model_dump())


class ModuleRouter:
    """Auto-discovering router that maps Event classes to FastAPI endpoints.

    The router scans a package for Event classes, uses conventions to map
    them to REST paths and methods, and creates FastAPI endpoints that
    dispatch events through a ModuleHost.

    Example:
        from pymodules import ModuleHost
        from pymodules.api import ModuleRouter

        host = ModuleHost()
        host.register(UserModule())

        router = ModuleRouter(host)
        router.discover_events("myapp.events")

        app = FastAPI()
        router.mount(app)
    """

    def __init__(
        self,
        host: ModuleHost,
        convention: RouteConvention | None = None,
    ):
        """Initialize the module router.

        Args:
            host: ModuleHost for dispatching events
            convention: Route convention engine (uses RESTConvention if not provided)
        """
        self.host = host
        self.convention = convention or RESTConvention()
        self.router = APIRouter()
        self._discovered_events: list[DiscoveredEvent] = []
        self._registered_events: set[str] = set()
        self._request_models: dict[str, type[BaseModel]] = {}

    def discover_events(self, package: str) -> int:
        """Discover events from a package and create endpoints.

        Args:
            package: Package name to scan for Event classes

        Returns:
            Number of events registered
        """
        discovery = EventDiscovery()
        events = discovery.discover(package)

        count = 0
        for event in events:
            if self._register_discovered_event(event):
                count += 1

        return count

    def register_event(self, event_class: type[Event[Any, Any]]) -> ModuleRouter:
        """Manually register a single event class.

        Args:
            event_class: Event class to register

        Returns:
            Self for method chaining
        """
        if is_excluded_from_api(event_class):
            return self

        discovery = EventDiscovery()
        discovered = discovery._extract_event_metadata(event_class, event_class.__module__)
        if discovered:
            self._register_discovered_event(discovered)
        return self

    def _register_discovered_event(self, event: DiscoveredEvent) -> bool:
        """Register a single discovered event as an endpoint.

        Returns:
            True if event was registered, False if skipped
        """
        # Skip duplicates
        event_key = f"{event.event_class.__module__}.{event.event_class.__name__}"
        if event_key in self._registered_events:
            return False

        route = self.convention.get_route(event.event_class)

        # Skip if not included in schema
        api_meta = event.api_metadata
        if api_meta.get("include_in_schema") is False:
            return False

        # Create request model for validation
        try:
            request_model = _dataclass_to_pydantic(event.input_class)
            self._request_models[event.event_name or event_key] = request_model
        except (TypeError, ValueError):
            return False

        # Create the endpoint function
        endpoint = self._create_endpoint(event, route, request_model)

        # Determine response model
        response_model = None
        if event.output_class:
            with contextlib.suppress(TypeError, ValueError):
                response_model = _dataclass_to_pydantic(event.output_class)

        # Add route to router
        self.router.add_api_route(
            path=route.path,
            endpoint=endpoint,
            methods=[route.method.value],
            tags=list(route.tags),
            summary=route.summary or self._generate_summary(event),
            deprecated=route.deprecated,
            response_model=response_model,
            response_model_exclude_none=api_meta.get("response_model_exclude_none", True),
        )

        self._discovered_events.append(event)
        self._registered_events.add(event_key)
        return True

    def _create_endpoint(
        self,
        event: DiscoveredEvent,
        route: RouteInfo,
        request_model: type[BaseModel],
    ) -> Callable[..., Any]:
        """Create an endpoint function for an event."""
        # Capture variables for closure
        host = self.host
        event_class = event.event_class
        input_class = event.input_class
        api_meta = event.api_metadata
        requires_id = route.requires_id
        id_param_name = route.id_param_name

        # Determine if auth is required
        is_public = api_meta.get("public", True)  # Default to public for simpler usage
        required_permissions = api_meta.get("required_permissions", [])

        if requires_id:
            # Endpoint with path parameter
            if route.method == HTTPMethod.GET:

                async def endpoint_get_with_id(
                    request: Request,
                    id: str = Path(..., description="Resource ID"),
                ) -> Any:
                    return await _dispatch_event(
                        host=host,
                        event_class=event_class,
                        input_class=input_class,
                        request=request,
                        path_id=id,
                        id_param_name=id_param_name,
                        body_data=None,
                        is_public=is_public,
                        required_permissions=required_permissions,
                    )

                return endpoint_get_with_id
            else:
                # Create annotated body type for FastAPI to recognize
                BodyType = Annotated[request_model, Body(...)]

                async def endpoint_body_with_id(
                    request: Request,
                    body: BodyType,  # type: ignore[valid-type]
                    id: str = Path(..., description="Resource ID"),
                ) -> Any:
                    return await _dispatch_event(
                        host=host,
                        event_class=event_class,
                        input_class=input_class,
                        request=request,
                        path_id=id,
                        id_param_name=id_param_name,
                        body_data=body.model_dump(),  # type: ignore[attr-defined]
                        is_public=is_public,
                        required_permissions=required_permissions,
                    )

                # Fix annotation for FastAPI
                endpoint_body_with_id.__annotations__["body"] = BodyType
                return endpoint_body_with_id
        else:
            # Endpoint without path parameter
            if route.method == HTTPMethod.GET:

                async def endpoint_get_no_id(request: Request) -> Any:
                    return await _dispatch_event(
                        host=host,
                        event_class=event_class,
                        input_class=input_class,
                        request=request,
                        path_id=None,
                        id_param_name=id_param_name,
                        body_data=None,
                        is_public=is_public,
                        required_permissions=required_permissions,
                    )

                return endpoint_get_no_id
            else:
                # Create annotated body type for FastAPI to recognize
                BodyType = Annotated[request_model, Body(...)]

                async def endpoint_body_no_id(
                    request: Request,
                    body: BodyType,  # type: ignore[valid-type]
                ) -> Any:
                    return await _dispatch_event(
                        host=host,
                        event_class=event_class,
                        input_class=input_class,
                        request=request,
                        path_id=None,
                        id_param_name=id_param_name,
                        body_data=body.model_dump(),  # type: ignore[attr-defined]
                        is_public=is_public,
                        required_permissions=required_permissions,
                    )

                # Fix annotation for FastAPI
                endpoint_body_no_id.__annotations__["body"] = BodyType
                return endpoint_body_no_id

    def _generate_summary(self, event: DiscoveredEvent) -> str:
        """Generate an OpenAPI summary from event metadata."""
        action = (event.action or "handle").replace("_", " ").title()
        domain = (event.domain or "event").replace("_", " ").title()
        return f"{action} {domain}"

    def mount(self, app: Any, prefix: str = "") -> None:
        """Mount the router on a FastAPI application.

        Args:
            app: FastAPI application
            prefix: URL prefix for all routes
        """
        app.include_router(self.router, prefix=prefix)

    @property
    def discovered_events(self) -> list[DiscoveredEvent]:
        """Get list of discovered events."""
        return self._discovered_events


async def _dispatch_event(
    host: ModuleHost,
    event_class: type[Event[Any, Any]],
    input_class: type[EventInput],
    request: Request,
    path_id: str | None,
    id_param_name: str,
    body_data: dict[str, Any] | None,
    is_public: bool,
    required_permissions: list[str],
) -> Any:
    """Dispatch an event through the ModuleHost."""
    # Check authentication if not public
    if not is_public:
        user = getattr(request.state, "user", None)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")

        # Check permissions
        if required_permissions:
            user_perms = getattr(user, "permissions", [])
            if not any(p in user_perms for p in required_permissions):
                raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Build input data
    input_data = body_data or {}

    # Inject ID from path if present
    if path_id:
        # Try to convert to UUID if the field expects it
        hints = getattr(input_class, "__annotations__", {})
        if id_param_name in hints:
            field_type = hints[id_param_name]
            type_name = getattr(field_type, "__name__", str(field_type))
            if "UUID" in type_name:
                from uuid import UUID

                try:
                    path_id = UUID(path_id)  # type: ignore[assignment]
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"Invalid UUID: {path_id}") from e
        input_data[id_param_name] = path_id

    # Create input instance
    try:
        input_instance = input_class(**input_data)
    except TypeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid input: {e}") from e

    # Create and dispatch event
    event = event_class(input=input_instance)

    try:
        await host.handle_async(event)
    except APIError:
        raise
    except Exception as e:
        # Check for common error patterns
        error_msg = str(e).lower()
        if "not found" in error_msg:
            raise NotFoundError(event_class.__name__.replace("Event", ""), str(path_id)) from e
        raise HTTPException(status_code=500, detail=str(e)) from e

    if not event.handled:
        raise HTTPException(status_code=501, detail=f"No handler for event: {event.name}")

    if event.output is None:
        return JSONResponse(content={"success": True}, status_code=200)

    # Convert output to dict
    if dataclasses.is_dataclass(event.output) and not isinstance(event.output, type):
        return dataclasses.asdict(event.output)
    return event.output
