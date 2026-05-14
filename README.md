# PyModules

A command-dispatch modular architecture for Python, inspired by [NetModules](https://github.com/netmodules/NetModules).

Build scalable, production-ready applications where components communicate through typed commands — like **lego blocks** that snap together.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-326%20passed-brightgreen.svg)](#testing)

## Features

- **Command Dispatch** — Loose coupling through typed in-process commands
- **Production Ready** — Rate limiting, circuit breaker, retry, health checks
- **Distributed Tracing** — Correlation IDs and OpenTelemetry support
- **FastAPI Integration** — Auto-generated REST endpoints with health/metrics
- **Async Native** — Full async/await support without thread pool overhead
- **Type Safe** — Full type hints and mypy compatibility

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              ModuleHost                                      │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                       Middleware Chain                                │  │
│  │                                                                       │  │
│  │   Command ─► RateLimit ─► CircuitBreaker ─► Retry ─► DLQ ─►          │  │
│  │              Tracing ─► Metrics ─► Terminal ─► Module.@handles ─►    │  │
│  │              Response                                                 │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  Each middleware is opt-in. The terminal looks up type(command) in the      │
│  dispatch table built from each Module's @handles(...) claims and invokes   │
│  the bound handler; the handler returns its typed CommandResponse.          │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Core Concepts

| Concept | Description |
|---------|-------------|
| **Command** | A typed in-process message with a `CommandRequest` payload, dispatched to exactly one handler that returns a `CommandResponse` |
| **Module** | A class whose `@handles(CommandClass)`-decorated methods claim Commands |
| **ModuleHost** | Central dispatcher that routes Commands by type to the claiming Module's handler, threading them through a configurable middleware chain |

## Installation

```bash
# Basic installation
pip install pymodules

# Database layer (SQLAlchemy async)
pip install pymodules[sqlite]      # SQLite with aiosqlite
pip install pymodules[postgres]    # PostgreSQL with asyncpg

# API layer (auto-discovery router)
pip install pymodules[api]         # FastAPI + auto-routing
pip install pymodules[api-db]      # API + database layer

# Full web stack (API + DB + JWT auth)
pip install pymodules[web]

# Development (includes testing tools)
pip install pymodules[dev]

# Everything
pip install pymodules[full]
```

## Quick Start

### 1. Define a Command

```python
from dataclasses import dataclass
from pymodules import Command, CommandRequest, CommandResponse

@dataclass
class GreetRequest(CommandRequest):
    name: str = "World"

@dataclass
class GreetResponse(CommandResponse):
    message: str = ""

class GreetCommand(Command[GreetRequest, GreetResponse]):
    name = "myapp.greet"
```

### 2. Create a Module

```python
from pymodules import Module, handles, module

@module(name="Greeter", description="Handles greeting commands")
class GreeterModule(Module):
    @handles(GreetCommand)
    def greet(self, command: GreetCommand) -> GreetResponse:
        return GreetResponse(message=f"Hello, {command.request.name}!")
```

### 3. Use with ModuleHost

```python
from pymodules import ModuleHost

# Create host and register modules
host = ModuleHost()
host.register(GreeterModule())

# Dispatch a command and read its return value
response = host.dispatch(GreetCommand(request=GreetRequest(name="Alice")))

print(response.message)  # "Hello, Alice!"
```

## Production Configuration

### Basic Configuration

`ModuleHostConfig` holds host-level settings plus an ordered `middleware`
list. Resilience and observability are middleware — opt them in with
`default_middleware(...)` or build the list explicitly.

```python
from pymodules import ModuleHost, ModuleHostConfig
from pymodules.resilience import default_middleware

config = ModuleHostConfig(
    max_workers=8,                  # Thread pool size for sync handlers
    propagate_exceptions=False,     # Don't re-raise handler errors
    middleware=default_middleware(
        enable_metrics=True,        # Add MetricsMiddleware
        enable_tracing=True,        # Add TracingMiddleware
    ),
)

host = ModuleHost(config=config)
```

### Environment Variables

Configure via environment for containerized deployments:

```bash
# Host-level settings (ModuleHostConfig.from_env)
export PYMODULES_MAX_WORKERS=8
export PYMODULES_PROPAGATE_EXCEPTIONS=false
export PYMODULES_LOG_LEVEL=INFO

# Middleware chain settings (default_middleware_from_env)
export PYMODULES_ENABLE_METRICS=true
export PYMODULES_ENABLE_TRACING=true
export PYMODULES_RATE_LIMIT=100          # Commands per second
export PYMODULES_RATE_LIMIT_BURST=10
export PYMODULES_CIRCUIT_BREAKER_THRESHOLD=5
export PYMODULES_RETRY_MAX=3
export PYMODULES_DLQ_SIZE=1000
```

```python
from pymodules import ModuleHost, ModuleHostConfig
from pymodules.resilience import default_middleware_from_env

# Host settings come from PYMODULES_MAX_WORKERS / PROPAGATE_EXCEPTIONS / LOG_LEVEL;
# the middleware chain is owned by default_middleware_from_env.
config = ModuleHostConfig.from_env()
config.middleware = default_middleware_from_env()
host = ModuleHost(config=config)
```

## Resilience Patterns

### Rate Limiting

Prevent command flooding with the token bucket algorithm:

```python
from pymodules import ModuleHost, ModuleHostConfig, RateLimitMiddleware

config = ModuleHostConfig(
    middleware=[
        RateLimitMiddleware(
            rate=100,    # 100 commands per second
            burst=10,    # Allow bursts up to 10
            block=False, # Raise RateLimitExceeded instead of blocking
        ),
    ],
)

host = ModuleHost(config=config)
```

### Circuit Breaker

Prevent cascading failures:

```python
from pymodules import CircuitBreaker, CircuitBreakerMiddleware, ModuleHostConfig

breaker = CircuitBreaker(
    failure_threshold=5,   # Open after 5 failures
    recovery_timeout=30,   # Try again after 30 seconds
    success_threshold=2,   # Close after 2 successes
)
config = ModuleHostConfig(middleware=[CircuitBreakerMiddleware(breaker)])
```

Circuit breaker states:
- **CLOSED**: Normal operation
- **OPEN**: Rejecting requests (after failures)
- **HALF_OPEN**: Testing if service recovered

### Retry with Exponential Backoff

```python
from pymodules import ModuleHostConfig, RetryMiddleware, RetryPolicy

policy = RetryPolicy(
    max_retries=3,
    base_delay=1.0,       # Start with 1 second
    max_delay=60.0,       # Cap at 60 seconds
    exponential_base=2.0, # Double each retry
)
config = ModuleHostConfig(middleware=[RetryMiddleware(policy)])
```

### Dead Letter Queue

Capture failed commands for later inspection:

```python
from pymodules import DeadLetterQueue, DLQMiddleware, ModuleHostConfig, ModuleHost

dlq = DeadLetterQueue(max_size=1000)

config = ModuleHostConfig(
    middleware=[DLQMiddleware(dlq, propagate_exceptions=False)],
    propagate_exceptions=False,
)
host = ModuleHost(config=config)

# Later, inspect failures
for entry in dlq.entries:
    print(f"Failed: {entry.command.name} - {entry.error}")

# Reprocess failed commands by re-dispatching them through the host
successful, failed = dlq.reprocess(host.dispatch)
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

Every command automatically gets a correlation ID when `TracingMiddleware`
is in the chain:

```python
from pymodules import ModuleHost, ModuleHostConfig
from pymodules.resilience import default_middleware

config = ModuleHostConfig(middleware=default_middleware(enable_tracing=True))
host = ModuleHost(config=config)

command = GreetCommand(request=GreetRequest(name="Alice"))
response = host.dispatch(command)

print(command.meta["correlation_id"])  # "a1b2c3d4e5f6..."
```

### Manual Tracing

```python
from pymodules import Tracer, get_tracer, set_tracer

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
from pymodules import Tracer
from pymodules.contrib.tracing.opentelemetry import OpenTelemetryExporter

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
from pymodules.contrib.health import HealthCheck, HealthStatus

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
from pymodules.contrib.health import (
    create_http_check,
    create_tcp_check,
    create_callable_check,
)

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

## Async Handlers

Native async support without thread pool overhead. Decorate the handler
method with `@handles(...)` and declare it `async def`; the host awaits it
directly on the dispatch loop.

```python
from pymodules import Module, handles, module

@module(name="AsyncGreeter")
class AsyncGreeterModule(Module):
    @handles(GreetCommand)
    async def greet(self, command: GreetCommand) -> GreetResponse:
        # Async operations work natively
        user = await fetch_user(command.request.name)
        return GreetResponse(message=f"Hello, {user.display_name}!")

# Async handlers must be invoked via dispatch_async — sync dispatch() raises
# SyncDispatchOnAsyncHandlerError when the resolved handler is a coroutine.
response = await host.dispatch_async(GreetCommand(request=GreetRequest(name="Alice")))
```

## Database Layer

The database layer provides async SQLAlchemy support with useful mixins and a generic repository pattern.

### Model Definitions with Mixins

```python
from sqlalchemy import Column, String
from pymodules.contrib.db import Base, UUIDMixin, TimestampMixin, SoftDeleteMixin

class User(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """User model with UUID, timestamps, and soft delete support."""

    __tablename__ = "users"

    name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False, unique=True)
```

**Available Mixins:**
- `UUIDMixin` - Auto-generated UUID primary key
- `TimestampMixin` - `created_at` and `updated_at` columns
- `SoftDeleteMixin` - `is_deleted` flag with `soft_delete()` and `restore()` methods

### Repository Pattern

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from pymodules.contrib.db import BaseRepository

# Set up async engine
engine = create_async_engine("sqlite+aiosqlite:///app.db")
session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Create repository
user_repo = BaseRepository(session_factory, User)

# CRUD operations
user = await user_repo.create(name="Alice", email="alice@example.com")
user = await user_repo.get_by_id(user.id)
users = await user_repo.get_all(limit=10, offset=0)
alice = await user_repo.find_one(name="Alice")
user = await user_repo.update(user.id, name="Alice Smith")
await user_repo.soft_delete(user.id)  # Soft delete
await user_repo.restore(user.id)      # Restore
await user_repo.delete(user.id)       # Hard delete
count = await user_repo.count()
```

### Database Settings

```python
from pymodules.contrib.db import DatabaseSettings

# Load from environment variables (PYMODULES_DB_* prefix)
settings = DatabaseSettings()

# Or configure directly
settings = DatabaseSettings(
    url="postgresql+asyncpg://user:pass@localhost/db",
    pool_size=10,
    echo=False,
)
```

## API Layer

The API layer maps `@api_endpoint`-decorated Commands to FastAPI routes. There
is no class-name-to-URL convention: each Command declares its HTTP method and
path explicitly. Class names are an internal concern; URLs are an external
contract.

### Declaring endpoints

```python
from pymodules import Command
from pymodules.contrib.api import api_endpoint, exclude_from_api

@api_endpoint(method="POST", path="/users")
class CreateUser(Command[CreateUserRequest, CreateUserResponse]):
    pass

@api_endpoint(method="GET", path="/users/{user_id}")
class GetUser(Command[GetUserRequest, GetUserResponse]):
    pass

@api_endpoint(
    method="POST",
    path="/users/search",
    tags=["Users", "Search"],
    summary="Search users by criteria",
)
class SearchUsers(Command[SearchUsersRequest, SearchUsersResponse]):
    pass

@exclude_from_api  # Not exposed as REST endpoint
class InternalSyncCommand(Command[CommandRequest, CommandResponse]):
    pass
```

### ModuleRouter

```python
from fastapi import FastAPI
from pymodules import ModuleHost
from pymodules.contrib.api import ModuleRouter, register_error_handlers

# Set up host and modules
host = ModuleHost()
host.register(UserModule())

# Create FastAPI app
app = FastAPI()
register_error_handlers(app)

# Register the @api_endpoint-decorated commands
router = ModuleRouter(host)
router.register_command(CreateUser)
router.register_command(GetUser)
router.register_command(SearchUsers)

# Mount on app
router.mount(app, prefix="/api/v1")
```

Commands without `@api_endpoint` are skipped silently — register them only via
`ModuleHost` if you want them to remain internal.

### Authentication

Pluggable authentication with JWT support:

```python
from datetime import UTC, datetime, timedelta
from pymodules.contrib.api.auth import AuthMiddleware, AuthProvider, TokenClaims, JWTAuthProvider

# Use built-in JWT provider
jwt_provider = JWTAuthProvider()  # Reads from PYMODULES_JWT_* env vars

# Or create custom provider
class MyAuthProvider(AuthProvider):
    async def validate_token(self, token: str) -> TokenClaims | None:
        # Your validation logic
        if is_valid(token):
            return TokenClaims(
                sub="user-123",
                exp=datetime.now(UTC) + timedelta(hours=1),
                iat=datetime.now(UTC),
                permissions=["read", "write"],
            )
        return None

    async def create_token(self, claims: dict) -> str:
        # Your token creation logic
        return generate_token(claims)

# Add middleware to FastAPI app
app.add_middleware(AuthMiddleware, provider=jwt_provider)
```

### Complete API Example

```python
from fastapi import FastAPI
from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    Module,
    ModuleHost,
    handles,
    module,
)
from pymodules.contrib.api import ModuleRouter, api_endpoint, register_error_handlers

# Define commands (request/response dataclasses elided for brevity)
@api_endpoint(method="POST", path="/products")
class CreateProduct(Command[CreateProductRequest, CreateProductResponse]):
    pass

@api_endpoint(method="GET", path="/products/{product_id}")
class GetProduct(Command[GetProductRequest, GetProductResponse]):
    pass

# Define module
@module(name="products", description="Product management")
class ProductModule(Module):
    @handles(CreateProduct)
    async def create(self, command: CreateProduct) -> CreateProductResponse:
        return CreateProductResponse(id="123", name=command.request.name)

    @handles(GetProduct)
    async def get(self, command: GetProduct) -> GetProductResponse:
        return GetProductResponse(id=command.request.product_id, name="Widget")

# Set up application
host = ModuleHost()
host.register(ProductModule())

app = FastAPI()
register_error_handlers(app)

router = ModuleRouter(host)
router.register_command(CreateProduct)
router.register_command(GetProduct)
router.mount(app, prefix="/api/v1")
```

## Metrics

`MetricsMiddleware` owns its counters; hold a reference to read them.

```python
from pymodules import MetricsMiddleware, ModuleHost, ModuleHostConfig

metrics = MetricsMiddleware()
config = ModuleHostConfig(middleware=[metrics])
host = ModuleHost(config=config)

# After dispatching some commands...
print(metrics.dispatched)  # Total commands seen by the middleware
print(metrics.succeeded)   # Returned without raising and were handled
print(metrics.failed)      # Inner chain raised
print(metrics.unmatched)   # No module claimed the Command class

# Per-concern counters live on their middleware instances:
#   RateLimitMiddleware.rejected_count
#   DLQMiddleware.dead_lettered_count
```

## Error Handling

`propagate_exceptions=True` re-raises handler exceptions wrapped in
`CommandHandlingError`. Lifecycle hooks (`on_start`, `on_end`, `on_error`)
live on `LifecycleMiddleware`, not on `ModuleHostConfig`.

```python
from pymodules import (
    CommandHandlingError,
    LifecycleMiddleware,
    ModuleHost,
    ModuleHostConfig,
    ModuleRegistrationError,
)

config = ModuleHostConfig(
    propagate_exceptions=True,  # Re-raise handler exceptions
    middleware=[
        LifecycleMiddleware(
            on_error=lambda e, command: logger.error(
                f"Failed: {command.name}", exc_info=e
            ),
        ),
    ],
)
host = ModuleHost(config=config)

try:
    response = host.dispatch(command)
except CommandHandlingError as e:
    print(f"Command: {e.command.name}")
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

# API example (pymodules.contrib.api)
uvicorn examples.api_example:app --reload
# Visit http://localhost:8000/docs for Swagger UI

# Agent primitive demo (one template, three trigger modes)
python -m examples.agent.app

# Full-stack tracer-bullet demo (pymodules.contrib.fullstack)
pip install -e ".[fullstack]"
uvicorn examples.fullstack.app:app --reload --port 8000
# Open http://localhost:8000/ in two tabs to see cross-tenant SSE isolation.
```

### `examples/agent/`

`examples/agent/` is the runnable counterpart to the integration tests
for the **Agent** primitive (ADR-0008): one `OrderProcessor` template
demonstrates explicit spawn, `@subscribes(OrderPlaced, route_by=...)`
per-customer routing, and a `@scheduled` reconciliation tick — all on
the same `self` — driven from a tiny terminal REPL. See
`examples/agent/README.md` for the walkthrough.

### `examples/fullstack/`

`examples/fullstack/` is the runnable tracer-bullet demo for
**`pymodules.contrib.fullstack`** (ADR-0009): one `MessageModule` posts a
Command, publishes a `MessagePosted` Event with a tenant-scoped
`@outbound_policy`, and a vanilla-JS page opens an `EventSource` against
`/__pymodules__/events?subscribe=MessagePosted`. Two browser tabs on
different tenants prove cross-tenant SSE isolation. See
`examples/fullstack/README.md` for the walkthrough.

## License

MIT

## Contributing

Contributions are welcome! Please read our contributing guidelines and submit pull requests.
