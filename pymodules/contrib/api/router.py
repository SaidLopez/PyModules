"""ModuleRouter — explicit REST routing for Commands.

Maps Command classes to FastAPI endpoints. The path and method come from the
``@api_endpoint(method=..., path=...)`` decorator on each Command class; there
is no class-name-to-URL convention (see CONTEXT.md non-goal #1). A Command
without ``@api_endpoint`` is skipped silently — it is treated as internal.
"""

from __future__ import annotations

import contextlib
import dataclasses
import re
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Body, HTTPException, Path, Request
from pydantic import BaseModel, create_model

from pymodules import Command, CommandRequest, ModuleHost

from .decorators import HTTPMethod, is_excluded_from_api
from .discovery import CommandDiscovery, DiscoveredCommand
from .errors import APIError, NotFoundError

T = TypeVar("T")

# Matches FastAPI-style path parameters like ``{id}`` or ``{user_id}``.
_PATH_PARAM_RE = re.compile(r"\{([^{}]+)\}")


def _dataclass_to_pydantic(dc_class: type) -> type[BaseModel]:
    """Convert a dataclass to a Pydantic model for request body validation."""
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
    """Convert a Pydantic model instance to a dataclass instance."""
    return dc_class(**model.model_dump())


class ModuleRouter:
    """Maps ``@api_endpoint``-decorated Command classes to FastAPI endpoints.

    The router scans a package (or accepts manual registrations), reads the
    ``@api_endpoint(method=..., path=...)`` metadata off each Command, and
    creates a FastAPI endpoint that dispatches the command through a
    ``ModuleHost``. Commands without ``@api_endpoint`` are skipped.

    Example:
        from pymodules import Command, ModuleHost
        from pymodules.contrib.api import ModuleRouter, api_endpoint

        @api_endpoint(method="POST", path="/users")
        class CreateUser(Command[CreateUserRequest, CreateUserResponse]):
            pass

        host = ModuleHost()
        host.register(UserModule())

        router = ModuleRouter(host)
        router.register_command(CreateUser)

        app = FastAPI()
        router.mount(app)
    """

    def __init__(self, host: ModuleHost):
        """Initialize the module router.

        Args:
            host: ModuleHost for dispatching commands
        """
        self.host = host
        self.router = APIRouter()
        self._discovered_commands: list[DiscoveredCommand] = []
        self._registered_commands: set[str] = set()
        self._request_models: dict[str, type[BaseModel]] = {}

    def discover_commands(self, package: str) -> int:
        """Discover commands from a package and create endpoints.

        Only commands decorated with ``@api_endpoint`` are mounted; the rest
        are treated as internal.

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

        The command must carry an ``@api_endpoint(method=..., path=...)``
        decorator; otherwise it is silently skipped.

        Args:
            command_class: Command class to register

        Returns:
            Self for method chaining
        """
        if is_excluded_from_api(command_class):
            return self

        discovery = CommandDiscovery()
        discovered = discovery._extract_command_metadata(command_class)
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

        api_meta = cmd.api_metadata

        # Skip if not included in schema
        if api_meta.get("include_in_schema") is False:
            return False

        path = api_meta.get("path")
        method_str = api_meta.get("method")
        if not path or not method_str:
            # No explicit routing; treat as internal.
            return False

        method = HTTPMethod(method_str) if isinstance(method_str, str) else method_str

        # Create request model for validation
        try:
            request_model = _dataclass_to_pydantic(cmd.input_class)
            self._request_models[cmd.command_name or command_key] = request_model
        except (TypeError, ValueError):
            return False

        path_params = _PATH_PARAM_RE.findall(path)

        # Create the endpoint function
        endpoint = self._create_endpoint(cmd, method, path_params, request_model)

        # Determine response model
        response_model = None
        if cmd.output_class:
            with contextlib.suppress(TypeError, ValueError):
                response_model = _dataclass_to_pydantic(cmd.output_class)

        tags = api_meta.get("tags") or []
        summary = api_meta.get("summary") or ""

        # Add route to router
        self.router.add_api_route(
            path=path,
            endpoint=endpoint,
            methods=[method.value],
            tags=list(tags),
            summary=summary,
            deprecated=api_meta.get("deprecated", False),
            response_model=response_model,
            response_model_exclude_none=api_meta.get("response_model_exclude_none", True),
        )

        self._discovered_commands.append(cmd)
        self._registered_commands.add(command_key)
        return True

    def _create_endpoint(
        self,
        cmd: DiscoveredCommand,
        method: HTTPMethod,
        path_params: list[str],
        request_model: type[BaseModel],
    ) -> Callable[..., Any]:
        """Create an endpoint function for a command."""
        host = self.host
        command_class = cmd.command_class
        input_class = cmd.input_class
        api_meta = cmd.api_metadata

        is_public = api_meta.get("public", True)
        required_permissions = api_meta.get("required_permissions", [])

        has_path_param = bool(path_params)
        # FastAPI requires the path parameter name to match the function arg.
        # We support at most one path parameter today; if the explicit path
        # declares more, the first one wins for ID injection.
        primary_param = path_params[0] if path_params else "id"

        if has_path_param:
            if method == HTTPMethod.GET:

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
                        id_param_name=primary_param,
                        body_data=None,
                        is_public=is_public,
                        required_permissions=required_permissions,
                    )

                # Rename the `id` parameter to match the declared path param so
                # FastAPI binds it correctly (e.g. `{user_id}` -> `user_id`).
                _rename_path_param(endpoint_get_with_id, "id", primary_param)
                return endpoint_get_with_id
            else:
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
                        id_param_name=primary_param,
                        body_data=body.model_dump(),  # type: ignore[attr-defined]
                        is_public=is_public,
                        required_permissions=required_permissions,
                    )

                endpoint_body_with_id.__annotations__["body"] = BodyType
                _rename_path_param(endpoint_body_with_id, "id", primary_param)
                return endpoint_body_with_id
        else:
            if method == HTTPMethod.GET:

                async def endpoint_get_no_id(request: Request) -> Any:
                    return await _dispatch_command(
                        host=host,
                        command_class=command_class,
                        input_class=input_class,
                        request=request,
                        path_id=None,
                        id_param_name=primary_param,
                        body_data=None,
                        is_public=is_public,
                        required_permissions=required_permissions,
                    )

                return endpoint_get_no_id
            else:
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
                        id_param_name=primary_param,
                        body_data=body.model_dump(),  # type: ignore[attr-defined]
                        is_public=is_public,
                        required_permissions=required_permissions,
                    )

                endpoint_body_no_id.__annotations__["body"] = BodyType
                return endpoint_body_no_id

    def mount(self, app: Any, prefix: str = "") -> None:
        """Mount the router on a FastAPI application."""
        app.include_router(self.router, prefix=prefix)

    @property
    def discovered_commands(self) -> list[DiscoveredCommand]:
        """Get list of discovered commands."""
        return self._discovered_commands


def _rename_path_param(fn: Callable[..., Any], old: str, new: str) -> None:
    """Rename a function parameter so FastAPI can match it to a path placeholder.

    No-op if ``old == new``. Updates ``__annotations__`` and the underlying
    ``__code__.co_varnames`` view exposed through ``__signature__``.
    """
    if old == new:
        return
    import inspect

    sig = inspect.signature(fn)
    params = []
    for name, param in sig.parameters.items():
        if name == old:
            params.append(param.replace(name=new))
        else:
            params.append(param)
    fn.__signature__ = sig.replace(parameters=params)  # type: ignore[attr-defined]
    if old in fn.__annotations__:
        fn.__annotations__[new] = fn.__annotations__.pop(old)


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
    if not is_public:
        user = getattr(request.state, "user", None)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")

        if required_permissions:
            user_perms = getattr(user, "permissions", [])
            if not any(p in user_perms for p in required_permissions):
                raise HTTPException(status_code=403, detail="Insufficient permissions")

    input_data = body_data or {}

    if path_id is not None:
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

    try:
        request_instance = input_class(**input_data)
    except TypeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid input: {e}") from e

    command = command_class(request=request_instance)

    try:
        response = await host.dispatch_async(command)
    except APIError:
        raise
    except Exception as e:
        error_msg = str(e).lower()
        if "not found" in error_msg:
            raise NotFoundError(command_class.__name__.replace("Command", ""), str(path_id)) from e
        raise HTTPException(status_code=500, detail=str(e)) from e

    if response is None:
        raise HTTPException(status_code=501, detail=f"No handler for command: {command.name}")

    if dataclasses.is_dataclass(response) and not isinstance(response, type):
        return dataclasses.asdict(response)
    return response
