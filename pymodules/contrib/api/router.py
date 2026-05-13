"""ModuleRouter with auto-discovery.

Provides ModuleRouter that automatically discovers Command classes and creates
FastAPI endpoints using convention-based routing.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Body, HTTPException, Path, Request
from pydantic import BaseModel, create_model

from pymodules import Command, CommandRequest, ModuleHost

from .conventions import HTTPMethod, RESTConvention, RouteConvention, RouteInfo
from .decorators import is_excluded_from_api
from .discovery import CommandDiscovery, DiscoveredCommand
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
    """Auto-discovering router that maps Command classes to FastAPI endpoints.

    The router scans a package for Command classes, uses conventions to map
    them to REST paths and methods, and creates FastAPI endpoints that
    dispatch commands through a ModuleHost.

    Example:
        from pymodules import ModuleHost
        from pymodules.contrib.api import ModuleRouter

        host = ModuleHost()
        host.register(UserModule())

        router = ModuleRouter(host)
        router.discover_commands("myapp.commands")

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
            host: ModuleHost for dispatching commands
            convention: Route convention engine (uses RESTConvention if not provided)
        """
        self.host = host
        self.convention = convention or RESTConvention()
        self.router = APIRouter()
        self._discovered_commands: list[DiscoveredCommand] = []
        self._registered_commands: set[str] = set()
        self._request_models: dict[str, type[BaseModel]] = {}

    def discover_commands(self, package: str) -> int:
        """Discover commands from a package and create endpoints.

        Args:
            package: Package name to scan for Command classes

        Returns:
            Number of commands registered
        """
        discovery = CommandDiscovery()
        commands = discovery.discover(package)

        count = 0
        for cmd in commands:
            if self._register_discovered_command(cmd):
                count += 1

        return count

    def register_command(self, command_class: type[Command[Any, Any]]) -> ModuleRouter:
        """Manually register a single command class.

        Args:
            command_class: Command class to register

        Returns:
            Self for method chaining
        """
        if is_excluded_from_api(command_class):
            return self

        discovery = CommandDiscovery()
        discovered = discovery._extract_command_metadata(command_class, command_class.__module__)
        if discovered:
            self._register_discovered_command(discovered)
        return self

    def _register_discovered_command(self, cmd: DiscoveredCommand) -> bool:
        """Register a single discovered command as an endpoint.

        Returns:
            True if command was registered, False if skipped
        """
        # Skip duplicates
        command_key = f"{cmd.command_class.__module__}.{cmd.command_class.__name__}"
        if command_key in self._registered_commands:
            return False

        route = self.convention.get_route(cmd.command_class)

        # Skip if not included in schema
        api_meta = cmd.api_metadata
        if api_meta.get("include_in_schema") is False:
            return False

        # Create request model for validation
        try:
            request_model = _dataclass_to_pydantic(cmd.input_class)
            self._request_models[cmd.command_name or command_key] = request_model
        except (TypeError, ValueError):
            return False

        # Create the endpoint function
        endpoint = self._create_endpoint(cmd, route, request_model)

        # Determine response model
        response_model = None
        if cmd.output_class:
            with contextlib.suppress(TypeError, ValueError):
                response_model = _dataclass_to_pydantic(cmd.output_class)

        # Add route to router
        self.router.add_api_route(
            path=route.path,
            endpoint=endpoint,
            methods=[route.method.value],
            tags=list(route.tags),
            summary=route.summary or self._generate_summary(cmd),
            deprecated=route.deprecated,
            response_model=response_model,
            response_model_exclude_none=api_meta.get("response_model_exclude_none", True),
        )

        self._discovered_commands.append(cmd)
        self._registered_commands.add(command_key)
        return True

    def _create_endpoint(
        self,
        cmd: DiscoveredCommand,
        route: RouteInfo,
        request_model: type[BaseModel],
    ) -> Callable[..., Any]:
        """Create an endpoint function for a command."""
        # Capture variables for closure
        host = self.host
        command_class = cmd.command_class
        input_class = cmd.input_class
        api_meta = cmd.api_metadata
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
                    return await _dispatch_command(
                        host=host,
                        command_class=command_class,
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
                    return await _dispatch_command(
                        host=host,
                        command_class=command_class,
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
                    return await _dispatch_command(
                        host=host,
                        command_class=command_class,
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
                    return await _dispatch_command(
                        host=host,
                        command_class=command_class,
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

    def _generate_summary(self, cmd: DiscoveredCommand) -> str:
        """Generate an OpenAPI summary from command metadata."""
        action = (cmd.action or "handle").replace("_", " ").title()
        domain = (cmd.domain or "command").replace("_", " ").title()
        return f"{action} {domain}"

    def mount(self, app: Any, prefix: str = "") -> None:
        """Mount the router on a FastAPI application.

        Args:
            app: FastAPI application
            prefix: URL prefix for all routes
        """
        app.include_router(self.router, prefix=prefix)

    @property
    def discovered_commands(self) -> list[DiscoveredCommand]:
        """Get list of discovered commands."""
        return self._discovered_commands


async def _dispatch_command(
    host: ModuleHost,
    command_class: type[Command[Any, Any]],
    input_class: type[CommandRequest],
    request: Request,
    path_id: str | None,
    id_param_name: str,
    body_data: dict[str, Any] | None,
    is_public: bool,
    required_permissions: list[str],
) -> Any:
    """Dispatch a command through the ModuleHost."""
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

    # Create request instance
    try:
        request_instance = input_class(**input_data)
    except TypeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid input: {e}") from e

    # Create and dispatch command
    command = command_class(request=request_instance)

    try:
        response = await host.dispatch_async(command)
    except APIError:
        raise
    except Exception as e:
        # Check for common error patterns
        error_msg = str(e).lower()
        if "not found" in error_msg:
            raise NotFoundError(command_class.__name__.replace("Command", ""), str(path_id)) from e
        raise HTTPException(status_code=500, detail=str(e)) from e

    if response is None:
        # No handler claimed the command (transitional silent no-op).
        raise HTTPException(status_code=501, detail=f"No handler for command: {command.name}")

    # Convert response to dict
    if dataclasses.is_dataclass(response) and not isinstance(response, type):
        return dataclasses.asdict(response)
    return response
