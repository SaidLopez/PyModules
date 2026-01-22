# PyModules

An event-driven modular architecture for Python, inspired by [NetModules](https://github.com/netmodules/NetModules).

Build scalable, production-ready applications where components communicate through typed events — like **lego blocks** that snap together.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-147%20passed-brightgreen.svg)](#testing)

## Features

- **Event-Driven Architecture** — Loose coupling through typed events
- **Production Ready** — Rate limiting, circuit breaker, retry, health checks
- **Distributed Tracing** — Correlation IDs and OpenTelemetry support
- **FastAPI Integration** — Auto-generated REST endpoints with health/metrics
- **Async Native** — Full async/await support without thread pool overhead
- **Type Safe** — Full type hints and mypy compatibility

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              ModuleHost                                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │ Rate Limiter│  │Circuit Break│  │ Retry Policy│  │    DLQ      │        │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘        │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                         Event Dispatcher                              │  │
│  │                                                                       │  │
│  │   Event ──► can_handle? ──► Module A ──► handled? ──► Response       │  │
│  │                  │                           │                        │  │
│  │                  ▼                           ▼                        │  │
│  │             Module B                    Module C                      │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                         │
│  │   Metrics   │  │   Tracing   │  │Health Check │                         │
│  └─────────────┘  └─────────────┘  └─────────────┘                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Core Concepts

| Concept | Description |
|---------|-------------|
| **Event** | A typed message with `Input` data and `Output` response |
| **Module** | A handler that declares what events it can process |
| **ModuleHost** | Central dispatcher that routes events to modules |

## Installation

```bash
# Basic installation
pip install pymodules

# With FastAPI integration
pip install pymodules[fastapi]

# Development (includes testing tools)
pip install pymodules[dev]

# Everything
pip install pymodules[all]
```

## Quick Start

### 1. Define Events

```python
from dataclasses import dataclass
from pymodules import Event, EventInput, EventOutput

@dataclass
class GreetInput(EventInput):
    name: str = "World"

@dataclass
class GreetOutput(EventOutput):
    message: str = ""

class GreetEvent(Event[GreetInput, GreetOutput]):
    name = "myapp.greet"
```

### 2. Create a Module

```python
from pymodules import Module, module, Event

@module(name="Greeter", description="Handles greeting events")
class GreeterModule(Module):
    def can_handle(self, event: Event) -> bool:
        return isinstance(event, GreetEvent)

    def handle(self, event: Event) -> None:
        if isinstance(event, GreetEvent):
            event.output = GreetOutput(
                message=f"Hello, {event.input.name}!"
            )
            event.handled = True
```

### 3. Use with ModuleHost

```python
from pymodules import ModuleHost

# Create host and register modules
host = ModuleHost()
host.register(GreeterModule())

# Dispatch event
event = GreetEvent(input=GreetInput(name="Alice"))
host.handle(event)

print(event.output.message)  # "Hello, Alice!"
```

## Production Configuration

### Basic Configuration

```python
from pymodules import ModuleHost, ModuleHostConfig

config = ModuleHostConfig(
    max_workers=8,              # Thread pool size
    propagate_exceptions=False, # Don't crash on handler errors
    enable_metrics=True,        # Enable metrics collection
    enable_tracing=True,        # Enable distributed tracing
)

host = ModuleHost(config=config)
```

### Environment Variables

Configure via environment for containerized deployments:

```bash
export PYMODULES_MAX_WORKERS=8
export PYMODULES_PROPAGATE_EXCEPTIONS=false
export PYMODULES_LOG_LEVEL=INFO
export PYMODULES_ENABLE_METRICS=true
export PYMODULES_ENABLE_TRACING=true
export PYMODULES_RATE_LIMIT=100          # Events per second
export PYMODULES_RATE_LIMIT_BURST=10
export PYMODULES_CIRCUIT_BREAKER_THRESHOLD=5
export PYMODULES_RETRY_MAX=3
export PYMODULES_DLQ_SIZE=1000
```

```python
# Load config from environment
config = ModuleHostConfig.from_env()
host = ModuleHost(config=config)
```

## Resilience Patterns

### Rate Limiting

Prevent event flooding with token bucket algorithm:

```python
from pymodules import ModuleHost, ModuleHostConfig
from pymodules.resilience import RateLimiter

config = ModuleHostConfig(
    rate_limiter=RateLimiter(
        rate=100,    # 100 events per second
        burst=10,    # Allow bursts up to 10
        block=False  # Raise RateLimitExceeded instead of blocking
    )
)

host = ModuleHost(config=config)
```

### Circuit Breaker

Prevent cascading failures:

```python
from pymodules.resilience import CircuitBreaker

config = ModuleHostConfig(
    circuit_breaker=CircuitBreaker(
        failure_threshold=5,   # Open after 5 failures
        recovery_timeout=30,   # Try again after 30 seconds
        success_threshold=2    # Close after 2 successes
    )
)
```

Circuit breaker states:
- **CLOSED**: Normal operation
- **OPEN**: Rejecting requests (after failures)
- **HALF_OPEN**: Testing if service recovered

### Retry with Exponential Backoff

```python
from pymodules.resilience import RetryPolicy

config = ModuleHostConfig(
    retry_policy=RetryPolicy(
        max_retries=3,
        base_delay=1.0,       # Start with 1 second
        max_delay=60.0,       # Cap at 60 seconds
        exponential_base=2.0  # Double each retry
    )
)
```

### Dead Letter Queue

Capture failed events for later inspection:

```python
from pymodules.resilience import DeadLetterQueue

dlq = DeadLetterQueue(max_size=1000)

config = ModuleHostConfig(
    dead_letter_queue=dlq,
    propagate_exceptions=False
)

host = ModuleHost(config=config)

# Later, inspect failures
for entry in dlq.entries:
    print(f"Failed: {entry.event.name} - {entry.error}")

# Reprocess failed events
successful, failed = dlq.reprocess(host.handle)
```

### Fallback / Graceful Degradation

```python
from pymodules.resilience import Fallback

fallback = Fallback(
    default_value={"status": "unavailable"},
    log_errors=True
)

@fallback
def get_user_data():
    return external_api.get_user()
```

## Distributed Tracing

### Correlation IDs

Every event automatically gets a correlation ID when tracing is enabled:

```python
config = ModuleHostConfig(enable_tracing=True)
host = ModuleHost(config=config)

event = GreetEvent(input=GreetInput(name="Alice"))
host.handle(event)

print(event.meta["correlation_id"])  # "a1b2c3d4e5f6..."
```

### Manual Tracing

```python
from pymodules.tracing import Tracer, get_tracer, set_tracer

tracer = Tracer(service_name="my-service")
set_tracer(tracer)

with tracer.trace("process_order", correlation_id="order-123") as ctx:
    with ctx.span("validate"):
        validate_order(order)

    with ctx.span("save"):
        save_order(order)

# Access trace data
print(ctx.to_dict())
```

### OpenTelemetry Integration

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from pymodules.tracing import Tracer, OpenTelemetryExporter

# Set up OpenTelemetry
trace.set_tracer_provider(TracerProvider())

# Create exporter
exporter = OpenTelemetryExporter()

# Use with PyModules tracer
tracer = Tracer(export_func=exporter.export)
```

## Health Checks

Kubernetes-compatible liveness and readiness probes:

```python
from pymodules import ModuleHost
from pymodules.health import HealthCheck, HealthStatus

host = ModuleHost()
host.register(MyModule())

health = HealthCheck(host=host, version="1.0.0")

# Add custom checks
health.add_check("database", check_database, liveness=True, readiness=True)
health.add_check("cache", check_redis, readiness=True)

# Run checks
report = health.check()
print(report.status)  # HealthStatus.HEALTHY

# Kubernetes probes
liveness = health.liveness()   # Is the app running?
readiness = health.readiness() # Can it serve traffic?
```

### Built-in Check Helpers

```python
from pymodules.health import create_http_check, create_tcp_check, create_callable_check

# HTTP health check
health.add_check("api", create_http_check("api", "https://api.example.com/health"))

# TCP connectivity check
health.add_check("db", create_tcp_check("db", "localhost", 5432))

# Simple callable check
health.add_check("disk", create_callable_check(
    "disk",
    lambda: get_disk_usage() < 90,
    healthy_message="Disk OK",
    unhealthy_message="Disk full"
))
```

## FastAPI Integration

### Basic Setup

```python
from fastapi import FastAPI
from pymodules import ModuleHost
from pymodules.fastapi import PyModulesAPI

host = ModuleHost()
host.register(GreeterModule())

app = FastAPI()
api = PyModulesAPI(host)

# Auto-generate endpoint from event
api.add_event_endpoint(app, "/greet", GreetEvent, GreetInput, GreetOutput)
```

### With Health & Metrics Endpoints

```python
from pymodules.fastapi import PyModulesAPI

api = PyModulesAPI(host, version="1.0.0")

# Add all standard endpoints
api.add_health_endpoints(app)  # /health, /health/live, /health/ready
api.add_metrics_endpoint(app)  # /metrics

# Or add individually
api.add_event_endpoint(app, "/greet", GreetEvent, GreetInput, GreetOutput)
```

### Tracing Middleware

```python
# Automatically inject correlation IDs into all requests
api.add_tracing_middleware(app)

# Correlation ID available in response headers
# X-Correlation-ID: abc123...
```

### Complete Example

```python
from fastapi import FastAPI
from pymodules import ModuleHost, ModuleHostConfig
from pymodules.fastapi import PyModulesAPI
from pymodules.resilience import RateLimiter, CircuitBreaker

# Configure for production
config = ModuleHostConfig(
    enable_metrics=True,
    enable_tracing=True,
    rate_limiter=RateLimiter(rate=100, burst=20),
    circuit_breaker=CircuitBreaker(failure_threshold=5),
)

host = ModuleHost(config=config)
host.register(GreeterModule())
host.register(CalculatorModule())

app = FastAPI(title="My API", version="1.0.0")
api = PyModulesAPI(host, version="1.0.0")

# Add standard endpoints
api.add_health_endpoints(app)
api.add_metrics_endpoint(app)
api.add_tracing_middleware(app)

# Add event endpoints
api.add_event_endpoint(app, "/greet", GreetEvent, GreetInput, GreetOutput)
api.add_event_endpoint(app, "/calculate", CalculatorEvent, CalculatorInput, CalculatorOutput)
```

## Async Handlers

Native async support without thread pool overhead:

```python
@module(name="AsyncGreeter")
class AsyncGreeterModule(Module):
    def can_handle(self, event: Event) -> bool:
        return isinstance(event, GreetEvent)

    async def handle(self, event: Event) -> None:
        if isinstance(event, GreetEvent):
            # Async operations work natively
            user = await fetch_user(event.input.name)
            event.output = GreetOutput(message=f"Hello, {user.display_name}!")
            event.handled = True

# Use handle_async for best performance
await host.handle_async(event)
```

## Metrics

```python
config = ModuleHostConfig(enable_metrics=True)
host = ModuleHost(config=config)

# After processing events...
metrics = host.metrics.to_dict()
# {
#     "events_dispatched": 1000,
#     "events_handled": 950,
#     "events_unhandled": 30,
#     "events_failed": 20,
#     "events_retried": 15,
#     "events_rate_limited": 5,
#     "events_circuit_broken": 0,
#     "events_dead_lettered": 20,
#     "modules_registered": 3
# }
```

## Error Handling

```python
from pymodules import ModuleHostConfig
from pymodules.exceptions import EventHandlingError, ModuleRegistrationError

config = ModuleHostConfig(
    propagate_exceptions=True,  # Re-raise handler exceptions
    on_error=lambda e, event: logger.error(f"Failed: {event.name}", exc_info=e)
)

try:
    host.handle(event)
except EventHandlingError as e:
    print(f"Event: {e.event.name}")
    print(f"Module: {e.module.metadata.name}")
    print(f"Error: {e.original_error}")
```

## Testing

```bash
# Run tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=pymodules --cov-report=html

# Run specific test file
pytest tests/test_resilience.py -v
```

## Running Examples

```bash
# Clone the repository
git clone https://github.com/pymodules/pymodules
cd pymodules

# Install dependencies
pip install -e ".[dev]"

# Basic demo
python -m examples.demo

# FastAPI app
uvicorn examples.fastapi_app:app --reload
# Visit http://localhost:8000/docs for Swagger UI
```

## License

MIT

## Contributing

Contributions are welcome! Please read our contributing guidelines and submit pull requests.
