"""
ModuleHost - Central dispatcher for the PyModules event system.

The ModuleHost manages module registration and routes events to
appropriate handlers based on their can_handle() declarations.
"""

import asyncio
import inspect
import time
from concurrent.futures import ThreadPoolExecutor

from .config import Metrics, ModuleHostConfig
from .exceptions import EventHandlingError, ModuleRegistrationError
from .interfaces import Event
from .logging import configure_logging, host_logger
from .module import Module
from .resilience import CircuitBreakerOpen, RateLimitExceeded
from .tracing import inject_trace_context


class ModuleHost:
    """
    Central coordinator that manages modules and dispatches events.

    The ModuleHost is the core of the PyModules system. It:
    - Registers and manages module instances
    - Routes events to modules that can handle them
    - Provides both sync and async event handling
    - Supports configurable error handling and logging
    - Includes resilience features: rate limiting, circuit breaker, retry, DLQ
    - Supports distributed tracing with correlation IDs

    Example:
        host = ModuleHost()
        host.register(GreeterModule())
        host.register(LoggingModule())

        event = GreetEvent(input=GreetInput(name="World"))
        host.handle(event)
        print(event.output.message)  # "Hello, World!"

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
        self._events_in_progress: dict[str, Event] = {}
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
    def events_in_progress(self) -> dict[str, Event]:
        """Currently processing events (for monitoring)."""
        return self._events_in_progress.copy()

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
        allowing it to dispatch events to other modules.

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

    def can_handle(self, event: Event) -> bool:
        """
        Check if any registered module can handle the event.

        Args:
            event: The event to check

        Returns:
            True if at least one module can handle the event
        """
        return any(m.can_handle(event) for m in self._modules)

    def _check_rate_limit(self, event: Event) -> None:
        """Check rate limit and raise if exceeded."""
        if self._config.rate_limiter:
            try:
                self._config.rate_limiter.acquire()
            except RateLimitExceeded:
                if self._metrics:
                    self._metrics.events_rate_limited += 1
                host_logger.warning("Rate limit exceeded for event %s", event.name)
                raise

    def _check_circuit_breaker(self, event: Event) -> None:
        """Check circuit breaker and raise if open."""
        if self._config.circuit_breaker:
            if not self._config.circuit_breaker.allow_request():
                if self._metrics:
                    self._metrics.events_circuit_broken += 1
                host_logger.warning("Circuit breaker open for event %s", event.name)
                raise CircuitBreakerOpen("Circuit breaker is open")

    def _prepare_dispatch(self, event: Event) -> str:
        """
        Common setup for event dispatch.

        Injects trace context, records event in progress, updates metrics,
        and calls on_event_start callback.

        Args:
            event: The event being dispatched

        Returns:
            event_id for tracking the event
        """
        # Inject trace context if tracing enabled
        if self._config.enable_tracing:
            inject_trace_context(event)

        event_id = str(id(event))
        self._events_in_progress[event_id] = event

        if self._metrics:
            self._metrics.events_dispatched += 1

        if self._config.on_event_start:
            try:
                self._config.on_event_start(event)
            except Exception as e:
                host_logger.warning("on_event_start callback failed: %s", e)

        return event_id

    def _handle_dispatch_error(self, event: Event, module: Module, error: Exception) -> None:
        """
        Common error handling for dispatch failures.

        Logs error, updates metrics, sends to DLQ if configured,
        and calls on_error callback.

        Args:
            event: The event that failed
            module: The module that raised the error
            error: The exception that was raised
        """
        host_logger.error(
            "Error in module %s handling event %s: %s",
            module.metadata.name,
            event.name,
            error,
            exc_info=True,
        )

        if self._metrics:
            self._metrics.events_failed += 1

        # Send to DLQ if configured
        if self._config.dead_letter_queue is not None:
            self._config.dead_letter_queue.add(
                event=event,
                error=error,
                module_name=module.metadata.name,
            )
            if self._metrics:
                self._metrics.events_dead_lettered += 1

        if self._config.on_error:
            try:
                self._config.on_error(error, event)
            except Exception as callback_error:
                host_logger.warning("on_error callback failed: %s", callback_error)

    def _finalize_dispatch(
        self, event: Event, event_id: str, error_occurred: Exception | None
    ) -> None:
        """
        Common cleanup after event dispatch.

        Removes event from in-progress, updates metrics, and calls
        on_event_end callback.

        Args:
            event: The event that was dispatched
            event_id: The event tracking ID
            error_occurred: Any error that occurred during dispatch
        """
        del self._events_in_progress[event_id]

        if self._metrics and error_occurred is None:
            if event.handled:
                self._metrics.events_handled += 1
            else:
                self._metrics.events_unhandled += 1

        if self._config.on_event_end:
            try:
                self._config.on_event_end(event, event.handled)
            except Exception as e:
                host_logger.warning("on_event_end callback failed: %s", e)

    def _handle_with_retry(self, event: Event, module: Module, attempt: int = 0) -> bool:
        """Handle event with retry logic."""
        try:
            # Check if handler is async
            if inspect.iscoroutinefunction(module.handle):
                # Run async handler - check for existing running loop first
                try:
                    asyncio.get_running_loop()
                    # Cannot safely run async handler from sync handle() when loop is running
                    raise RuntimeError(
                        "Cannot call sync handle() with async handler from async context. "
                        "Use handle_async() instead."
                    )
                except RuntimeError as e:
                    if "Cannot call sync handle()" in str(e):
                        raise
                    # No running loop - use asyncio.run() which handles loop lifecycle
                    asyncio.run(module.handle(event))
            else:
                module.handle(event)

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
                    "Retrying event %s (attempt %d) after %.2fs: %s",
                    event.name,
                    attempt + 1,
                    delay,
                    e,
                )
                time.sleep(delay)
                return self._handle_with_retry(event, module, attempt + 1)

            # No more retries, raise or send to DLQ
            raise

    def handle(self, event: Event) -> Event:
        """
        Dispatch an event to registered modules.

        The event is passed to each module's can_handle() method.
        If a module can handle it, handle() is called. Processing
        stops when a module sets event.handled = True.

        Args:
            event: The event to dispatch

        Returns:
            The event (with output set if handled)

        Raises:
            EventHandlingError: If propagate_exceptions is True and
                               a handler raises an exception.
            RateLimitExceeded: If rate limit is exceeded.
            CircuitBreakerOpen: If circuit breaker is open.
        """
        # Check rate limit (sync version)
        self._check_rate_limit(event)

        # Check circuit breaker
        self._check_circuit_breaker(event)

        # Common setup
        event_id = self._prepare_dispatch(event)

        host_logger.debug("Dispatching event: %s (id=%s)", event.name, event_id)

        error_occurred = None

        try:
            for module in self._modules:
                if module.can_handle(event):
                    host_logger.debug(
                        "Module %s handling event %s",
                        module.metadata.name,
                        event.name,
                    )
                    try:
                        self._handle_with_retry(event, module)
                    except Exception as e:
                        error_occurred = e
                        self._handle_dispatch_error(event, module, e)

                        if self._config.propagate_exceptions:
                            raise EventHandlingError(
                                f"Handler error in {module.metadata.name}",
                                event=event,
                                module=module,
                                original_error=e,
                            ) from e
                        break

                    if event.handled:
                        host_logger.debug(
                            "Event %s handled by %s",
                            event.name,
                            module.metadata.name,
                        )
                        break
        finally:
            self._finalize_dispatch(event, event_id, error_occurred)

        return event

    async def handle_async(self, event: Event) -> Event:
        """
        Async version of handle() for use with FastAPI and async modules.

        Supports native async handlers without thread pool overhead.

        Args:
            event: The event to dispatch

        Returns:
            The event (with output set if handled)

        Raises:
            EventHandlingError: If propagate_exceptions is True and
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
                host_logger.warning("Rate limit exceeded for event %s", event.name)
                raise

        # Check circuit breaker
        self._check_circuit_breaker(event)

        # Common setup
        event_id = self._prepare_dispatch(event)

        host_logger.debug("Dispatching event async: %s (id=%s)", event.name, event_id)

        error_occurred = None

        try:
            for module in self._modules:
                if module.can_handle(event):
                    host_logger.debug(
                        "Module %s handling event %s (async)",
                        module.metadata.name,
                        event.name,
                    )
                    try:
                        await self._handle_with_retry_async(event, module)
                    except Exception as e:
                        error_occurred = e
                        self._handle_dispatch_error(event, module, e)

                        if self._config.propagate_exceptions:
                            raise EventHandlingError(
                                f"Handler error in {module.metadata.name}",
                                event=event,
                                module=module,
                                original_error=e,
                            ) from e
                        break

                    if event.handled:
                        host_logger.debug(
                            "Event %s handled by %s (async)",
                            event.name,
                            module.metadata.name,
                        )
                        break
        finally:
            self._finalize_dispatch(event, event_id, error_occurred)

        return event

    async def _handle_with_retry_async(
        self, event: Event, module: Module, attempt: int = 0
    ) -> bool:
        """Handle event with retry logic (async version)."""
        try:
            # Check if handler is async
            if inspect.iscoroutinefunction(module.handle):
                await module.handle(event)
            else:
                # Run sync handler in thread pool
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(self._executor, module.handle, event)

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
                    "Retrying event %s (attempt %d) after %.2fs: %s",
                    event.name,
                    attempt + 1,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
                return await self._handle_with_retry_async(event, module, attempt + 1)

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

    async def publish(
        self,
        event_name: str,
        data: dict | None = None,
        headers: dict[str, str] | None = None,
        stream: str | None = None,
    ) -> str | None:
        """
        Publish an event to the external message broker.

        This method allows modules and the host to publish events to
        distributed message queues for inter-service communication.

        Args:
            event_name: Name of the event (used for routing).
            data: Event data payload.
            headers: Optional message headers (trace IDs, etc.).
            stream: Target stream/topic (defaults to event_name).

        Returns:
            Message ID if published successfully, None if no broker configured.

        Raises:
            PublishError: If publishing fails.

        Example:
            await host.publish(
                "user.created",
                data={"user_id": 123, "email": "user@example.com"},
                headers={"source": "auth-service"}
            )
        """
        if self._config.message_broker is None:
            host_logger.debug("No message broker configured, skipping publish")
            return None

        from .messaging import Message

        # Build message
        message = Message(
            data=data or {},
            headers=headers or {},
            event_name=event_name,
        )

        # Add trace context to headers if tracing enabled
        if self._config.enable_tracing:
            from .tracing import get_current_trace
            trace = get_current_trace()
            if trace:
                message.headers["trace_id"] = trace.trace_id
                if trace.current_span:
                    message.headers["span_id"] = trace.current_span.span_id
                if trace.correlation_id:
                    message.headers["correlation_id"] = trace.correlation_id

        # Determine target stream
        target_stream = stream or event_name

        try:
            message_id = await self._config.message_broker.publish(target_stream, message)

            if self._metrics:
                self._metrics.events_published += 1

            host_logger.debug(
                "Published event %s to stream %s (id=%s)",
                event_name,
                target_stream,
                message_id,
            )
            return message_id

        except Exception as e:
            host_logger.error("Failed to publish event %s: %s", event_name, e)
            raise

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
