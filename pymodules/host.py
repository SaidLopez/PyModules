"""
ModuleHost - Central dispatcher for the PyModules command system.

The ModuleHost manages module registration and routes commands to a single
claiming handler resolved in O(1) by ``type(command)``. Each Module method
decorated with ``@handles(CommandClass)`` contributes one entry to the
host's dispatch table.

The handler **returns** its typed CommandResponse; ``dispatch`` returns
that value to the caller. The Command itself is not mutated.
"""

import asyncio
import inspect
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from .config import Metrics, ModuleHostConfig
from .exceptions import (
    CommandHandlingError,
    DuplicateCommandError,
    ModuleRegistrationError,
)
from .interfaces import Command, CommandRequest, CommandResponse
from .logging import configure_logging, host_logger
from .module import HANDLES_ATTR, Module
from .resilience import CircuitBreakerOpen, RateLimitExceeded
from .tracing import inject_trace_context

# TypeVars for typed dispatch surface. ``Req``/``Resp`` propagate from the
# Command's generic parameters through ``dispatch`` to the caller; their
# bounds match ``Command``'s own declared bounds in ``interfaces.py``.
Req = TypeVar("Req", bound=CommandRequest)
Resp = TypeVar("Resp", bound=CommandResponse)


class ModuleHost:
    """
    Central coordinator that manages modules and dispatches commands.

    The ModuleHost is the core of the PyModules system. It:
    - Registers Module instances and builds a type-routed dispatch table
    - Routes each Command in O(1) to the single Module that claims its type
    - Returns the handler's response to the caller
    - Provides both sync and async dispatch
    - Supports configurable error handling and logging
    - Includes resilience features: rate limiting, circuit breaker, retry, DLQ
    - Supports distributed tracing with correlation IDs

    Example:
        host = ModuleHost()
        host.register(GreeterModule())
        host.register(LoggingModule())

        command = GreetCommand(request=GreetRequest(name="World"))
        response = host.dispatch(command)
        print(response.message)  # "Hello, World!"
    """

    def __init__(self, config: ModuleHostConfig | None = None):
        """
        Initialize the ModuleHost.

        Args:
            config: Optional configuration. If None, uses defaults or
                   environment variables via ModuleHostConfig.from_env().
        """
        self._config = config or ModuleHostConfig.from_env()
        self._modules: list[Module] = []
        # type-routed dispatch table: Command class -> bound handler method
        self._dispatch_table: dict[type, Callable[[Command[Any, Any]], Any]] = {}
        self._commands_in_progress: dict[str, Command[Any, Any]] = {}
        self._executor = ThreadPoolExecutor(max_workers=self._config.max_workers)
        self._metrics = Metrics() if self._config.enable_metrics else None
        self._start_time = time.time()

        # Configure logging based on config
        configure_logging(level=self._config.log_level)
        host_logger.debug(
            "ModuleHost initialized with max_workers=%d, propagate_exceptions=%s",
            self._config.max_workers,
            self._config.propagate_exceptions,
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
    def commands_in_progress(self) -> dict[str, Command[Any, Any]]:
        """Currently processing commands (for monitoring)."""
        return self._commands_in_progress.copy()

    @property
    def metrics(self) -> Metrics | None:
        """Metrics if enabled, None otherwise."""
        return self._metrics

    @property
    def uptime_seconds(self) -> float:
        """Time since host was created, in seconds."""
        return time.time() - self._start_time

    def _collect_handlers(
        self, module: Module
    ) -> list[tuple[type, Callable[[Command[Any, Any]], Any]]]:
        """
        Scan ``module``'s class for ``@handles``-decorated methods.

        Returns a list of ``(command_class, bound_method)`` pairs.
        """
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
        """
        Register a module with this host.

        Scans the module's class for ``@handles``-decorated methods and
        adds each claimed Command class to the dispatch table. Raises
        :class:`DuplicateCommandError` if any claim collides with an
        already-registered module's claim, unless ``override=True``.

        Args:
            module: The module to register
            override: If True, silently overwrite any existing claim for
                the same Command class. Useful for test doubles.

        Returns:
            self (for method chaining)

        Raises:
            ModuleRegistrationError: If registration fails for any reason
                (including duplicate claims when ``override`` is False).
        """
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

            # Commit the dispatch table entries (override semantics: clobber).
            for cmd_class, bound in new_pairs:
                self._dispatch_table[cmd_class] = bound

            module.on_load()

            if self._metrics:
                self._metrics.modules_registered = len(self._modules)

            host_logger.info(
                "Registered module: %s (v%s) claiming %d command type(s)",
                module.metadata.name,
                module.metadata.version,
                len(new_pairs),
            )
        except DuplicateCommandError:
            # Rollback partial state and re-raise unchanged so callers can
            # distinguish duplicate-claim from other registration failures.
            if module in self._modules:
                self._modules.remove(module)
            module._host = None
            raise
        except Exception as e:
            host_logger.error("Failed to register module %s: %s", type(module).__name__, e)
            # Rollback
            if module in self._modules:
                self._modules.remove(module)
            # Remove any partial dispatch table entries that point at this module.
            self._dispatch_table = {
                k: v
                for k, v in self._dispatch_table.items()
                if getattr(v, "__self__", None) is not module
            }
            module._host = None
            raise ModuleRegistrationError(f"Failed to register {type(module).__name__}: {e}") from e

        return self

    def unregister(self, module: Module) -> "ModuleHost":
        """
        Unregister a module from this host.

        Removes every dispatch-table entry whose bound method belongs to
        the given module.

        Args:
            module: The module to unregister

        Returns:
            self (for method chaining)
        """
        if module in self._modules:
            try:
                module.on_unload()
            except Exception as e:
                host_logger.warning(
                    "Error during on_unload for %s: %s",
                    module.metadata.name,
                    e,
                )
            # Drop every dispatch entry bound to this module.
            self._dispatch_table = {
                k: v
                for k, v in self._dispatch_table.items()
                if getattr(v, "__self__", None) is not module
            }
            module._host = None
            self._modules.remove(module)

            if self._metrics:
                self._metrics.modules_registered = len(self._modules)

            host_logger.info("Unregistered module: %s", module.metadata.name)
        return self

    def can_handle(self, command: Command[Any, Any]) -> bool:
        """
        True if a registered Module claims ``type(command)``.

        This is a convenience check around the dispatch table; it never
        runs handler code.
        """
        return type(command) in self._dispatch_table

    def _check_rate_limit(self, command: Command[Any, Any]) -> None:
        """Check rate limit and raise if exceeded."""
        if self._config.rate_limiter:
            try:
                self._config.rate_limiter.acquire()
            except RateLimitExceeded:
                if self._metrics:
                    self._metrics.events_rate_limited += 1
                host_logger.warning("Rate limit exceeded for command %s", command.name)
                raise

    def _check_circuit_breaker(self, command: Command[Any, Any]) -> None:
        """Check circuit breaker and raise if open."""
        if self._config.circuit_breaker:
            if not self._config.circuit_breaker.allow_request():
                if self._metrics:
                    self._metrics.events_circuit_broken += 1
                host_logger.warning("Circuit breaker open for command %s", command.name)
                raise CircuitBreakerOpen("Circuit breaker is open")

    def _prepare_dispatch(self, command: Command[Any, Any]) -> str:
        """
        Common setup for command dispatch.

        Injects trace context, records command in progress, updates metrics,
        and calls on_event_start callback.

        Args:
            command: The command being dispatched

        Returns:
            command_id for tracking the command
        """
        # Inject trace context if tracing enabled
        if self._config.enable_tracing:
            inject_trace_context(command)

        command_id = str(id(command))
        self._commands_in_progress[command_id] = command

        if self._metrics:
            self._metrics.events_dispatched += 1

        if self._config.on_event_start:
            try:
                self._config.on_event_start(command)
            except Exception as e:
                host_logger.warning("on_event_start callback failed: %s", e)

        return command_id

    def _handle_dispatch_error(
        self, command: Command[Any, Any], module: Module, error: Exception
    ) -> None:
        """
        Common error handling for dispatch failures.

        Logs error, updates metrics, sends to DLQ if configured,
        and calls on_error callback.

        Args:
            command: The command that failed
            module: The module that raised the error
            error: The exception that was raised
        """
        host_logger.error(
            "Error in module %s handling command %s: %s",
            module.metadata.name,
            command.name,
            error,
            exc_info=True,
        )

        if self._metrics:
            self._metrics.events_failed += 1

        # Send to DLQ if configured
        if self._config.dead_letter_queue is not None:
            self._config.dead_letter_queue.add(
                command=command,
                error=error,
                module_name=module.metadata.name,
            )
            if self._metrics:
                self._metrics.events_dead_lettered += 1

        if self._config.on_error:
            try:
                self._config.on_error(error, command)
            except Exception as callback_error:
                host_logger.warning("on_error callback failed: %s", callback_error)

    def _finalize_dispatch(
        self,
        command: Command[Any, Any],
        command_id: str,
        error_occurred: Exception | None,
        was_handled: bool,
    ) -> None:
        """
        Common cleanup after command dispatch.

        Removes command from in-progress, updates metrics, and calls
        on_event_end callback.

        Args:
            command: The command that was dispatched
            command_id: The command tracking ID
            error_occurred: Any error that occurred during dispatch
            was_handled: True if ``type(command)`` had a registered handler.
        """
        del self._commands_in_progress[command_id]

        if self._metrics and error_occurred is None:
            if was_handled:
                self._metrics.events_handled += 1
            else:
                self._metrics.events_unhandled += 1

        if self._config.on_event_end:
            try:
                self._config.on_event_end(command, was_handled)
            except Exception as e:
                host_logger.warning("on_event_end callback failed: %s", e)

    def _handle_with_retry(
        self,
        command: Command[Any, Any],
        handler: Callable[[Command[Any, Any]], Any],
        attempt: int = 0,
    ) -> Any:
        """Handle command with retry logic. Returns the handler's response."""
        try:
            # Check if handler is async
            if inspect.iscoroutinefunction(handler):
                # Run async handler - check for existing running loop first
                try:
                    asyncio.get_running_loop()
                    # Cannot safely run async handler from sync dispatch() when loop is running
                    raise RuntimeError(
                        "Cannot call sync dispatch() with async handler from async context. "
                        "Use dispatch_async() instead."
                    )
                except RuntimeError as e:
                    if "Cannot call sync dispatch()" in str(e):
                        raise
                    # No running loop - use asyncio.run() which handles loop lifecycle
                    response = asyncio.run(handler(command))
            else:
                response = handler(command)

            # Record success with circuit breaker
            if self._config.circuit_breaker:
                self._config.circuit_breaker.record_success()

            return response

        except Exception as e:
            # Record failure with circuit breaker
            if self._config.circuit_breaker:
                self._config.circuit_breaker.record_failure()

            # Check if we should retry
            if self._config.retry_policy and self._config.retry_policy.should_retry(e, attempt):
                if self._metrics:
                    self._metrics.events_retried += 1

                delay = self._config.retry_policy.calculate_delay(attempt)
                host_logger.warning(
                    "Retrying command %s (attempt %d) after %.2fs: %s",
                    command.name,
                    attempt + 1,
                    delay,
                    e,
                )
                time.sleep(delay)
                return self._handle_with_retry(command, handler, attempt + 1)

            # No more retries, raise or send to DLQ
            raise

    def dispatch(self, command: Command[Req, Resp]) -> Resp:
        """
        Dispatch a command to the registered handler for its type.

        The handler is resolved in O(1) by ``type(command)``. The handler's
        return value is propagated back to the caller. If no module claims
        the type, this currently returns ``None`` silently (a future commit
        will raise ``UnknownCommandError`` instead).

        Args:
            command: The command to dispatch

        Returns:
            The response value returned by the claiming handler. If no
            handler is registered for ``type(command)``, returns ``None``
            (transitional; this case will raise in 1.0).

        Raises:
            CommandHandlingError: If propagate_exceptions is True and
                               the handler raises an exception.
            RateLimitExceeded: If rate limit is exceeded.
            CircuitBreakerOpen: If circuit breaker is open.
        """
        # Check rate limit (sync version)
        self._check_rate_limit(command)

        # Check circuit breaker
        self._check_circuit_breaker(command)

        # Common setup
        command_id = self._prepare_dispatch(command)

        host_logger.debug("Dispatching command: %s (id=%s)", command.name, command_id)

        handler = self._dispatch_table.get(type(command))
        if handler is None:
            # No handler claims this Command class - silent no-op for now
            # (commit 6 territory: this will raise UnknownCommandError).
            self._finalize_dispatch(command, command_id, None, was_handled=False)
            return None  # type: ignore[return-value]

        module = handler.__self__  # type: ignore[attr-defined]
        error_occurred: Exception | None = None
        response: Any = None

        try:
            host_logger.debug(
                "Module %s handling command %s",
                module.metadata.name,
                command.name,
            )
            try:
                response = self._handle_with_retry(command, handler)
            except Exception as e:
                error_occurred = e
                self._handle_dispatch_error(command, module, e)

                if self._config.propagate_exceptions:
                    raise CommandHandlingError(
                        f"Handler error in {module.metadata.name}",
                        command=command,
                        module=module,
                        original_error=e,
                    ) from e

            if error_occurred is None:
                host_logger.debug(
                    "Command %s handled by %s",
                    command.name,
                    module.metadata.name,
                )
        finally:
            self._finalize_dispatch(command, command_id, error_occurred, was_handled=True)

        return response  # type: ignore[no-any-return]

    async def dispatch_async(self, command: Command[Req, Resp]) -> Resp:
        """
        Async version of dispatch() for use with FastAPI and async modules.

        Supports native async handlers without thread pool overhead. Like
        the sync version, the handler's return value is propagated to the
        caller.

        Args:
            command: The command to dispatch

        Returns:
            The response value returned by the claiming handler. If no
            handler is registered for ``type(command)``, returns ``None``
            (transitional; this case will raise in 1.0).

        Raises:
            CommandHandlingError: If propagate_exceptions is True and
                               the handler raises an exception.
            RateLimitExceeded: If rate limit is exceeded.
            CircuitBreakerOpen: If circuit breaker is open.
        """
        # Check rate limit (async version)
        if self._config.rate_limiter:
            try:
                await self._config.rate_limiter.acquire_async()
            except RateLimitExceeded:
                if self._metrics:
                    self._metrics.events_rate_limited += 1
                host_logger.warning("Rate limit exceeded for command %s", command.name)
                raise

        # Check circuit breaker
        self._check_circuit_breaker(command)

        # Common setup
        command_id = self._prepare_dispatch(command)

        host_logger.debug("Dispatching command async: %s (id=%s)", command.name, command_id)

        handler = self._dispatch_table.get(type(command))
        if handler is None:
            self._finalize_dispatch(command, command_id, None, was_handled=False)
            return None  # type: ignore[return-value]

        module = handler.__self__  # type: ignore[attr-defined]
        error_occurred: Exception | None = None
        response: Any = None

        try:
            host_logger.debug(
                "Module %s handling command %s (async)",
                module.metadata.name,
                command.name,
            )
            try:
                response = await self._handle_with_retry_async(command, handler)
            except Exception as e:
                error_occurred = e
                self._handle_dispatch_error(command, module, e)

                if self._config.propagate_exceptions:
                    raise CommandHandlingError(
                        f"Handler error in {module.metadata.name}",
                        command=command,
                        module=module,
                        original_error=e,
                    ) from e

            if error_occurred is None:
                host_logger.debug(
                    "Command %s handled by %s (async)",
                    command.name,
                    module.metadata.name,
                )
        finally:
            self._finalize_dispatch(command, command_id, error_occurred, was_handled=True)

        return response  # type: ignore[no-any-return]

    async def _handle_with_retry_async(
        self,
        command: Command[Any, Any],
        handler: Callable[[Command[Any, Any]], Any],
        attempt: int = 0,
    ) -> Any:
        """Handle command with retry logic (async version). Returns handler response."""
        try:
            # Check if handler is async
            if inspect.iscoroutinefunction(handler):
                response = await handler(command)
            else:
                # Run sync handler in thread pool
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(self._executor, handler, command)

            # Record success with circuit breaker
            if self._config.circuit_breaker:
                self._config.circuit_breaker.record_success()

            return response

        except Exception as e:
            # Record failure with circuit breaker
            if self._config.circuit_breaker:
                self._config.circuit_breaker.record_failure()

            # Check if we should retry
            if self._config.retry_policy and self._config.retry_policy.should_retry(e, attempt):
                if self._metrics:
                    self._metrics.events_retried += 1

                delay = self._config.retry_policy.calculate_delay(attempt)
                host_logger.warning(
                    "Retrying command %s (attempt %d) after %.2fs: %s",
                    command.name,
                    attempt + 1,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
                return await self._handle_with_retry_async(command, handler, attempt + 1)

            # No more retries, raise
            raise

    def get_module(self, module_type: type[Module]) -> Module | None:
        """
        Find a registered module by type.

        Args:
            module_type: The module class to find

        Returns:
            The module instance, or None if not found
        """
        for module in self._modules:
            if isinstance(module, module_type):
                return module
        return None

    def get_module_by_name(self, name: str) -> Module | None:
        """
        Find a registered module by name.

        Args:
            name: The module name (from @module decorator)

        Returns:
            The module instance, or None if not found
        """
        for module in self._modules:
            if module.metadata.name == name:
                return module
        return None

    def shutdown(self, wait: bool = True) -> None:
        """
        Shutdown the ModuleHost and release resources.

        Args:
            wait: If True, wait for pending tasks to complete.
        """
        host_logger.info("Shutting down ModuleHost")

        # Unregister all modules
        for module in self._modules.copy():
            self.unregister(module)

        # Shutdown executor
        self._executor.shutdown(wait=wait)
        host_logger.debug("ModuleHost shutdown complete")


__all__ = ["ModuleHost"]
