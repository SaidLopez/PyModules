"""
ModuleHost - Central dispatcher for the PyModules command system.

The ModuleHost manages module registration and routes commands to
appropriate handlers based on their can_handle() declarations.
"""

import asyncio
import inspect
import time
from concurrent.futures import ThreadPoolExecutor

from .config import Metrics, ModuleHostConfig
from .exceptions import CommandHandlingError, ModuleRegistrationError
from .interfaces import Command
from .logging import configure_logging, host_logger
from .module import Module
from .resilience import CircuitBreakerOpen, RateLimitExceeded
from .tracing import inject_trace_context


class ModuleHost:
    """
    Central coordinator that manages modules and dispatches commands.

    The ModuleHost is the core of the PyModules system. It:
    - Registers and manages module instances
    - Routes commands to modules that can handle them
    - Provides both sync and async dispatch
    - Supports configurable error handling and logging
    - Includes resilience features: rate limiting, circuit breaker, retry, DLQ
    - Supports distributed tracing with correlation IDs

    Example:
        host = ModuleHost()
        host.register(GreeterModule())
        host.register(LoggingModule())

        command = GreetCommand(input=GreetRequest(name="World"))
        host.dispatch(command)
        print(command.output.message)  # "Hello, World!"

    Example with configuration:
        from pymodules.config import ModuleHostConfig
        from pymodules.resilience import RateLimiter, CircuitBreaker

        config = ModuleHostConfig(
            max_workers=8,
            propagate_exceptions=False,
            rate_limiter=RateLimiter(rate=100, burst=10),
            circuit_breaker=CircuitBreaker(failure_threshold=5),
            enable_metrics=True,
            enable_tracing=True,
        )
        host = ModuleHost(config=config)
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
        self._commands_in_progress: dict[str, Command] = {}
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
    def commands_in_progress(self) -> dict[str, Command]:
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

    def register(self, module: Module) -> "ModuleHost":
        """
        Register a module with this host.

        The module's host property will be set to this host,
        allowing it to dispatch commands to other modules.

        Args:
            module: The module to register

        Returns:
            self (for method chaining)

        Raises:
            ModuleRegistrationError: If registration fails
        """
        try:
            module._host = self
            self._modules.append(module)
            module.on_load()

            if self._metrics:
                self._metrics.modules_registered = len(self._modules)

            host_logger.info(
                "Registered module: %s (v%s)",
                module.metadata.name,
                module.metadata.version,
            )
        except Exception as e:
            host_logger.error("Failed to register module %s: %s", type(module).__name__, e)
            # Rollback
            if module in self._modules:
                self._modules.remove(module)
            module._host = None
            raise ModuleRegistrationError(f"Failed to register {type(module).__name__}: {e}") from e

        return self

    def unregister(self, module: Module) -> "ModuleHost":
        """
        Unregister a module from this host.

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
            module._host = None
            self._modules.remove(module)

            if self._metrics:
                self._metrics.modules_registered = len(self._modules)

            host_logger.info("Unregistered module: %s", module.metadata.name)
        return self

    def can_handle(self, command: Command) -> bool:
        """
        Check if any registered module can handle the command.

        Args:
            command: The command to check

        Returns:
            True if at least one module can handle the command
        """
        return any(m.can_handle(command) for m in self._modules)

    def _check_rate_limit(self, command: Command) -> None:
        """Check rate limit and raise if exceeded."""
        if self._config.rate_limiter:
            try:
                self._config.rate_limiter.acquire()
            except RateLimitExceeded:
                if self._metrics:
                    self._metrics.events_rate_limited += 1
                host_logger.warning("Rate limit exceeded for command %s", command.name)
                raise

    def _check_circuit_breaker(self, command: Command) -> None:
        """Check circuit breaker and raise if open."""
        if self._config.circuit_breaker:
            if not self._config.circuit_breaker.allow_request():
                if self._metrics:
                    self._metrics.events_circuit_broken += 1
                host_logger.warning("Circuit breaker open for command %s", command.name)
                raise CircuitBreakerOpen("Circuit breaker is open")

    def _prepare_dispatch(self, command: Command) -> str:
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

    def _handle_dispatch_error(self, command: Command, module: Module, error: Exception) -> None:
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
        self, command: Command, command_id: str, error_occurred: Exception | None
    ) -> None:
        """
        Common cleanup after command dispatch.

        Removes command from in-progress, updates metrics, and calls
        on_event_end callback.

        Args:
            command: The command that was dispatched
            command_id: The command tracking ID
            error_occurred: Any error that occurred during dispatch
        """
        del self._commands_in_progress[command_id]

        if self._metrics and error_occurred is None:
            if command.handled:
                self._metrics.events_handled += 1
            else:
                self._metrics.events_unhandled += 1

        if self._config.on_event_end:
            try:
                self._config.on_event_end(command, command.handled)
            except Exception as e:
                host_logger.warning("on_event_end callback failed: %s", e)

    def _handle_with_retry(self, command: Command, module: Module, attempt: int = 0) -> bool:
        """Handle command with retry logic."""
        try:
            # Check if handler is async
            if inspect.iscoroutinefunction(module.handle):
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
                    asyncio.run(module.handle(command))
            else:
                module.handle(command)

            # Record success with circuit breaker
            if self._config.circuit_breaker:
                self._config.circuit_breaker.record_success()

            return True

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
                return self._handle_with_retry(command, module, attempt + 1)

            # No more retries, raise or send to DLQ
            raise

    def dispatch(self, command: Command) -> Command:
        """
        Dispatch a command to registered modules.

        The command is passed to each module's can_handle() method.
        If a module can handle it, handle() is called. Processing
        stops when a module sets command.handled = True.

        Args:
            command: The command to dispatch

        Returns:
            The command (with output set if handled)

        Raises:
            CommandHandlingError: If propagate_exceptions is True and
                               a handler raises an exception.
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

        error_occurred = None

        try:
            for module in self._modules:
                if module.can_handle(command):
                    host_logger.debug(
                        "Module %s handling command %s",
                        module.metadata.name,
                        command.name,
                    )
                    try:
                        self._handle_with_retry(command, module)
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
                        break

                    if command.handled:
                        host_logger.debug(
                            "Command %s handled by %s",
                            command.name,
                            module.metadata.name,
                        )
                        break
        finally:
            self._finalize_dispatch(command, command_id, error_occurred)

        return command

    async def dispatch_async(self, command: Command) -> Command:
        """
        Async version of dispatch() for use with FastAPI and async modules.

        Supports native async handlers without thread pool overhead.

        Args:
            command: The command to dispatch

        Returns:
            The command (with output set if handled)

        Raises:
            CommandHandlingError: If propagate_exceptions is True and
                               a handler raises an exception.
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

        error_occurred = None

        try:
            for module in self._modules:
                if module.can_handle(command):
                    host_logger.debug(
                        "Module %s handling command %s (async)",
                        module.metadata.name,
                        command.name,
                    )
                    try:
                        await self._handle_with_retry_async(command, module)
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
                        break

                    if command.handled:
                        host_logger.debug(
                            "Command %s handled by %s (async)",
                            command.name,
                            module.metadata.name,
                        )
                        break
        finally:
            self._finalize_dispatch(command, command_id, error_occurred)

        return command

    async def _handle_with_retry_async(
        self, command: Command, module: Module, attempt: int = 0
    ) -> bool:
        """Handle command with retry logic (async version)."""
        try:
            # Check if handler is async
            if inspect.iscoroutinefunction(module.handle):
                await module.handle(command)
            else:
                # Run sync handler in thread pool
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(self._executor, module.handle, command)

            # Record success with circuit breaker
            if self._config.circuit_breaker:
                self._config.circuit_breaker.record_success()

            return True

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
                return await self._handle_with_retry_async(command, module, attempt + 1)

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
