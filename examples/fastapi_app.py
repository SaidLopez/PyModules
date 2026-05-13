"""
Example FastAPI application using PyModules.

Demonstrates production-ready setup with:
- Health check endpoints (/health, /health/live, /health/ready)
- Metrics endpoint (/metrics)
- Correlation ID tracing (X-Correlation-ID header)
- Rate limiting and circuit breaker
- Command-based endpoints

Run with: uvicorn examples.fastapi_app:app --reload
Visit: http://localhost:8000/docs for Swagger UI
"""

try:
    from fastapi import FastAPI
except ImportError as e:
    raise ImportError("FastAPI is required. Install with: pip install pymodules[fastapi]") from e

from examples.calculator_module import (
    CalculatorCommand,
    CalculatorInput,
    CalculatorModule,
    CalculatorOutput,
)
from examples.greet_module import GreetCommand, GreeterModule, GreetInput, GreetOutput
from examples.logging_module import LoggingModule
from pymodules import MetricsMiddleware, ModuleHost, ModuleHostConfig, TracingMiddleware
from pymodules.fastapi import PyModulesAPI
from pymodules.resilience import (
    CircuitBreaker,
    CircuitBreakerMiddleware,
    RateLimitMiddleware,
)

# =============================================================================
# Production Configuration
# =============================================================================

metrics = MetricsMiddleware()
breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30, success_threshold=2)

config = ModuleHostConfig(
    max_workers=4,
    propagate_exceptions=False,  # Don't crash on handler errors
    middleware=[
        # Rate limiting: 100 requests/second with burst of 20
        RateLimitMiddleware(rate=100, burst=20, block=False),
        # Circuit breaker: open after 5 failures, recover after 30s
        CircuitBreakerMiddleware(breaker),
        # Correlation IDs on every command
        TracingMiddleware(),
        # Counters available on ``metrics`` reference
        metrics,
    ],
)

# =============================================================================
# Create Host and Register Modules
# =============================================================================

host = ModuleHost(config=config)
host.register(GreeterModule())
host.register(CalculatorModule())
host.register(LoggingModule())

# =============================================================================
# Create FastAPI App
# =============================================================================

app = FastAPI(
    title="PyModules Example API",
    description="""
    Demonstrates PyModules integration with FastAPI.

    ## Features
    - **Command-based routing**: HTTP endpoints dispatch typed commands
    - **Health checks**: Kubernetes-compatible liveness and readiness probes
    - **Metrics**: Command processing statistics
    - **Tracing**: Automatic correlation ID injection

    ## Endpoints
    - `/greet` - Generate personalized greetings
    - `/calculate` - Perform arithmetic operations
    - `/health` - Health check status
    - `/metrics` - Command processing metrics
    """,
    version="1.0.0",
)

# =============================================================================
# PyModules API Integration
# =============================================================================

api = PyModulesAPI(host, version="1.0.0")

# Add production endpoints: health, metrics, and tracing middleware
api.add_all_endpoints(app)

# Add command-based endpoints
api.add_command_endpoint(
    app=app,
    path="/greet",
    command_class=GreetCommand,
    input_class=GreetInput,
    output_class=GreetOutput,
    summary="Generate a greeting",
    description="Send a name and receive a personalized greeting",
    tags=["Greetings"],
)

api.add_command_endpoint(
    app=app,
    path="/calculate",
    command_class=CalculatorCommand,
    input_class=CalculatorInput,
    output_class=CalculatorOutput,
    summary="Perform calculation",
    description="Perform arithmetic operations (add, subtract, multiply, divide)",
    tags=["Calculator"],
)


# =============================================================================
# Additional Custom Endpoints
# =============================================================================


@app.post("/greet-formal", tags=["Greetings"])
async def greet_formal(name: str = "Guest"):
    """Manual endpoint showing direct command dispatch with formal greeting."""
    command = GreetCommand(request=GreetInput(name=name, formal=True))
    response = await api.dispatch(command)
    return {"message": response.message, "formal": True}


@app.get("/modules", tags=["Info"])
async def list_modules():
    """List all registered modules and their metadata."""
    return {
        "count": len(host.modules),
        "modules": [
            {
                "name": m.metadata.name,
                "description": m.metadata.description,
                "version": m.metadata.version,
            }
            for m in host.modules
        ],
    }


@app.get("/", tags=["Info"])
async def root():
    """API root with service information and endpoint links."""
    return {
        "service": "PyModules FastAPI Example",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "commands": {
                "/greet": "POST - Generate a greeting",
                "/greet-formal": "POST - Generate a formal greeting",
                "/calculate": "POST - Perform calculation",
            },
            "operations": {
                "/health": "GET - Full health check",
                "/health/live": "GET - Liveness probe",
                "/health/ready": "GET - Readiness probe",
                "/metrics": "GET - Command processing metrics",
                "/modules": "GET - List registered modules",
            },
            "documentation": {
                "/docs": "GET - Swagger UI",
                "/redoc": "GET - ReDoc documentation",
            },
        },
    }


# =============================================================================
# Startup/Shutdown Hooks (FastAPI lifecycle)
# =============================================================================


@app.on_event("startup")
async def startup():
    """Log startup information."""
    print("Starting PyModules FastAPI Example")
    print(f"Registered modules: {[m.metadata.name for m in host.modules]}")
    print(f"Middleware count: {len(config.middleware)}")
    print(f"Metrics counter (dispatched): {metrics.dispatched}")
    print(f"Circuit breaker state: {breaker.state.value}")


@app.on_event("shutdown")
async def shutdown():
    """Cleanup on shutdown."""
    print("Shutting down PyModules FastAPI Example")
    host.shutdown(wait=True)
