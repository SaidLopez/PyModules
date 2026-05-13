"""
ModuleHost - Central dispatcher for the PyModules command system.

Dispatch is a middleware chain. ``ModuleHost`` composes the configured
middleware (from ``ModuleHostConfig.middleware``) plus a built-in terminal
middleware once at construction. ``dispatch_async()`` invokes the chain;
``dispatch()`` is a thin sync wrapper that refuses to bridge:

- If the resolved handler is a coroutine function, sync ``dispatch()``
  raises ``SyncDispatchOnAsyncHandlerError``.
- If a loop is already running in the calling thread, sync ``dispatch()``
  raises ``SyncDispatchInAsyncContextError``.

The terminal middleware looks up ``type(command)`` in the dispatch table.
Sync handlers are bridged to async via the host's ``ThreadPoolExecutor``.
Unmatched dispatches silently return ``None`` (transitional; a future
commit will raise ``UnknownCommandError``).
"""

import asyncio
import inspect
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from .config import ModuleHostConfig
from .exceptions import (
    CommandHandlingError,
    DuplicateCommandError,
    ModuleRegistrationError,
    PyModulesError,
    SyncDispatchInAsyncContextError,
    SyncDispatchOnAsyncHandlerError,
)
from .interfaces import Command, CommandRequest, CommandResponse
from .logging import configure_logging, host_logger
from .middleware import Middleware, NextCall
from .module import HANDLES_ATTR, Module
from .resilience import CircuitBreakerOpen, RateLimitExceeded

# TypeVars for typed dispatch surface. ``Req``/``Resp`` propagate from the
# Command's generic parameters through ``dispatch`` to the caller.
Req = TypeVar("Req", bound=CommandRequest)
Resp = TypeVar("Resp", bound=CommandResponse)


# Sentinel key the terminal middleware writes onto ``command.meta`` to
# signal "no module claimed this Command class". Observability middleware
# (``MetricsMiddleware``, ``LifecycleMiddleware``) reads and clears it.
from .tracing import MetricsMiddleware as _MetricsMiddleware  # noqa: E402

_UNMATCHED_FLAG = _MetricsMiddleware.UNMATCHED_FLAG


class ModuleHost:
    """
    Central coordinator that manages modules and dispatches commands.

    The middleware chain is composed once at construction from
    ``config.middleware + [terminal]``. ``dispatch_async`` runs the chain;
    ``dispatch`` is a thin sync wrapper that does not bridge async.

    Example:
        from pymodules.resilience import default_middleware

        config = ModuleHostConfig(
            middleware=default_middleware(rate_limit=100, retry_max=3),
        )
        host = ModuleHost(config=config)
        host.register(GreeterModule())

        response = host.dispatch(GreetCommand(request=GreetRequest(name="World")))
    """

    def __init__(self, config: ModuleHostConfig | None = None):
        self._config = config or ModuleHostConfig.from_env()
        self._modules: list[Module] = []
        # type-routed dispatch table: Command class -> bound handler method
        self._dispatch_table: dict[type, Callable[[Command[Any, Any]], Any]] = {}
        self._executor = ThreadPoolExecutor(max_workers=self._config.max_workers)
        self._start_time = time.time()

        # Compose the chain once: configured middleware (outermost first),
        # then the host's terminal middleware.
        self._chain = self._build_chain(list(self._config.middleware))

        configure_logging(level=self._config.log_level)
        host_logger.debug(
            "ModuleHost initialized with max_workers=%d, propagate_exceptions=%s, "
            "middleware_count=%d",
            self._config.max_workers,
            self._config.propagate_exceptions,
            len(self._config.middleware),
        )

    @property
    def config(self) -> ModuleHostConfig:
        """Current configuration."""
        return self._config

    @property
    def modules(self) -> list[Module]:
        """List of registered modules."""
        return self._modules.copy()

    @property
    def uptime_seconds(self) -> float:
        """Time since host was created, in seconds."""
        return time.time() - self._start_time

    # ------------------------------------------------------------------
    # Middleware chain composition
    # ------------------------------------------------------------------

    async def _terminal(self, command: Command[Any, Any]) -> Any:
        """
        Built-in terminal of the middleware chain.

        Looks up ``type(command)`` in the dispatch table and invokes the
        claiming handler. Sync handlers run on the host's executor.
        Unmatched types signal via ``command.meta[_UNMATCHED_FLAG]`` and
        return ``None``.

        Signature note: the terminal does not take a ``next`` parameter —
        it is the end of the chain. The chain builder adapts it.
        """
        handler = self._dispatch_table.get(type(command))
        if handler is None:
            command.meta[_UNMATCHED_FLAG] = True
            return None

        module = handler.__self__  # type: ignore[attr-defined]
        host_logger.debug(
            "Module %s handling command %s",
            module.metadata.name,
            command.name,
        )
        # Terminal raises raw exceptions; middleware (retry, DLQ,
        # fallback, ...) sees them directly. The host's outermost wrapper
        # is responsible for honouring ``propagate_exceptions``.
        if inspect.iscoroutinefunction(handler):
            return await handler(command)
        # Sync handler: run on the executor so we don't block the loop.
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, handler, command)

    def _build_chain(self, middlewares: list[Middleware]) -> NextCall:
        """
        Compose ``middlewares + [terminal]`` into a single async callable.

        The first middleware is the outermost wrapper. Composition is
        right-fold: the terminal becomes the innermost ``next``.
        """
        next_call: NextCall = self._terminal

        # We need each wrapped layer to call the layer it was registered
        # to wrap, not the latest ``next_call``. Build from the back.
        for mw in reversed(middlewares):
            inner = next_call

            async def call(
                command: Command[Any, Any], _mw: Middleware = mw, _inner: NextCall = inner
            ) -> Any:
                return await _mw(command, _inner)

            next_call = call

        return next_call

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _collect_handlers(
        self, module: Module
    ) -> list[tuple[type, Callable[[Command[Any, Any]], Any]]]:
        """Scan ``module``'s class for ``@handles``-decorated methods."""
        pairs: list[tuple[type, Callable[[Command[Any, Any]], Any]]] = []
        for name, member in inspect.getmembers(type(module), predicate=inspect.isfunction):
            claims = getattr(member, HANDLES_ATTR, None)
            if not claims:
                continue
            bound = getattr(module, name)
            for cmd_class in claims:
                pairs.append((cmd_class, bound))
        return pairs

    def register(self, module: Module, override: bool = False) -> "ModuleHost":
        """Register a module and add its ``@handles`` claims to the dispatch table."""
        try:
            new_pairs = self._collect_handlers(module)

            if not override:
                for cmd_class, _ in new_pairs:
                    existing = self._dispatch_table.get(cmd_class)
                    if existing is not None and existing.__self__ is not module:  # type: ignore[attr-defined]
                        existing_module = existing.__self__  # type: ignore[attr-defined]
                        raise DuplicateCommandError(
                            f"Command {cmd_class.__name__} is already claimed by "
                            f"{type(existing_module).__name__} "
                            f"(module name: {existing_module.metadata.name}); "
                            f"{type(module).__name__} "
                            f"(module name: {module.metadata.name}) cannot also claim it. "
                            "Pass override=True to ModuleHost.register to replace."
                        )

            module._host = self
            self._modules.append(module)

            for cmd_class, bound in new_pairs:
                self._dispatch_table[cmd_class] = bound

            module.on_load()

            host_logger.info(
                "Registered module: %s (v%s) claiming %d command type(s)",
                module.metadata.name,
                module.metadata.version,
                len(new_pairs),
            )
        except DuplicateCommandError:
            if module in self._modules:
                self._modules.remove(module)
            module._host = None
            raise
        except Exception as e:
            host_logger.error("Failed to register module %s: %s", type(module).__name__, e)
            if module in self._modules:
                self._modules.remove(module)
            self._dispatch_table = {
                k: v
                for k, v in self._dispatch_table.items()
                if getattr(v, "__self__", None) is not module
            }
            module._host = None
            raise ModuleRegistrationError(f"Failed to register {type(module).__name__}: {e}") from e

        return self

    def unregister(self, module: Module) -> "ModuleHost":
        """Unregister a module and remove its dispatch table entries."""
        if module in self._modules:
            try:
                module.on_unload()
            except Exception as e:
                host_logger.warning(
                    "Error during on_unload for %s: %s",
                    module.metadata.name,
                    e,
                )
            self._dispatch_table = {
                k: v
                for k, v in self._dispatch_table.items()
                if getattr(v, "__self__", None) is not module
            }
            module._host = None
            self._modules.remove(module)
            host_logger.info("Unregistered module: %s", module.metadata.name)
        return self

    def can_handle(self, command: Command[Any, Any]) -> bool:
        """True if a registered Module claims ``type(command)``."""
        return type(command) in self._dispatch_table

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _invoke_chain(self, command: Command[Any, Any]) -> Any:
        """
        Run the composed middleware chain, applying the host's
        ``propagate_exceptions`` policy at the outermost layer.

        Exceptions from middleware or the terminal handler are wrapped in
        ``CommandHandlingError`` so callers always see a uniform type.
        With ``propagate_exceptions=False`` the exception is logged and
        dispatch returns ``None``.
        """
        try:
            return await self._chain(command)
        except CommandHandlingError:
            if self._config.propagate_exceptions:
                raise
            host_logger.error("Dispatch of %s failed", command.name, exc_info=True)
            return None
        except (RateLimitExceeded, CircuitBreakerOpen, PyModulesError):
            # Framework-level signals: always propagate as-is, regardless
            # of ``propagate_exceptions`` (which controls whether *handler*
            # exceptions escape).
            raise
        except Exception as e:
            handler = self._dispatch_table.get(type(command))
            module = handler.__self__ if handler is not None else None  # type: ignore[attr-defined]
            wrapped = CommandHandlingError(
                f"Handler error in {module.metadata.name if module else '<unknown>'}",
                command=command,
                module=module,
                original_error=e,
            )
            wrapped.__cause__ = e
            if self._config.propagate_exceptions:
                raise wrapped from e
            host_logger.error("Dispatch of %s failed: %s", command.name, e, exc_info=True)
            return None

    def dispatch(self, command: Command[Req, Resp]) -> Resp:
        """
        Synchronously dispatch ``command`` through the middleware chain.

        Thin wrapper that does not bridge async:

        - If the resolved handler is a coroutine function, raises
          ``SyncDispatchOnAsyncHandlerError``.
        - If an event loop is already running in this thread, raises
          ``SyncDispatchInAsyncContextError``.

        Otherwise runs the async chain to completion via ``asyncio.run``.

        Returns:
            The response value returned by the claiming handler, or
            ``None`` if no handler claims ``type(command)``.
        """
        handler = self._dispatch_table.get(type(command))
        if handler is not None and inspect.iscoroutinefunction(handler):
            raise SyncDispatchOnAsyncHandlerError(
                f"Sync dispatch() cannot run async handler for {type(command).__name__}; "
                "use dispatch_async() instead."
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise SyncDispatchInAsyncContextError(
                "Sync dispatch() cannot run while an event loop is already running "
                "in this thread; use dispatch_async() instead."
            )

        host_logger.debug("Dispatching command: %s", command.name)
        return asyncio.run(self._invoke_chain(command))  # type: ignore[no-any-return]

    async def dispatch_async(self, command: Command[Req, Resp]) -> Resp:
        """
        Asynchronously dispatch ``command`` through the middleware chain.

        Returns:
            The response value returned by the claiming handler, or
            ``None`` if no handler claims ``type(command)``.
        """
        host_logger.debug("Dispatching command async: %s", command.name)
        return await self._invoke_chain(command)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Module lookup
    # ------------------------------------------------------------------

    def get_module(self, module_type: type[Module]) -> Module | None:
        """Find a registered module by type."""
        for module in self._modules:
            if isinstance(module, module_type):
                return module
        return None

    def get_module_by_name(self, name: str) -> Module | None:
        """Find a registered module by name."""
        for module in self._modules:
            if module.metadata.name == name:
                return module
        return None

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the host: unregister modules, shut down the executor."""
        host_logger.info("Shutting down ModuleHost")
        for module in self._modules.copy():
            self.unregister(module)
        self._executor.shutdown(wait=wait)
        host_logger.debug("ModuleHost shutdown complete")


__all__ = ["ModuleHost"]
