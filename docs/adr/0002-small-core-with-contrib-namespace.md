# Small core, contrib lives under `pymodules.contrib.*`

The framework had grown to ~9.5k lines spread across `pymodules.{api,db,messaging,discovery,fastapi}` plus core dispatch — every user paid the conceptual cost of Redis, Consul, SQLAlchemy, FastAPI, and JWT even when they only wanted the dispatch core. The "lego blocks" pitch from the README was incompatible with the actual surface area.

We committed to a small dispatch core (registration, dispatch, resilience, tracing, logging, exceptions, protocols) and moved every integration to `pymodules.contrib.{api,db,messaging,discovery,health,auth}`. Each contrib package is gated behind a PyPI extra and is import-isolated — `from pymodules import ModuleHost` must not trigger any optional dependency.

## Consequences

- A single distribution still ships on PyPI; the boundary is enforced by import path and extras, not by repository splits. A future split into separate distributions (`pymodules-redis`, `pymodules-sqlalchemy`, etc.) is left open and made mechanical by this layout.
- `pymodules.health` becomes contrib (it is Kubernetes-shaped, not core to dispatch). `resilience` and `tracing` remain core — a dispatch framework without retries or correlation IDs is a toy. **But concrete exporters that depend on optional packages move to contrib**: `OpenTelemetryExporter` lives at `pymodules.contrib.tracing.opentelemetry`, not in core `pymodules.tracing`. Core `tracing` keeps `Tracer`, `TraceContext`, `Span`, `generate_id`, and the correlation-ID accessors — anything that requires `import opentelemetry` is contrib.
- The deprecated `pymodules.fastapi` is a removal-only shim; new work goes to `pymodules.contrib.api`.
- `ModuleHostConfig` loses the `message_broker` and `service_registry` fields. Brokers and registries are constructed independently by user code or contrib helpers; the host is broker-unaware (see ADR-0004's "no host.publish" note).
- `ModuleHostConfig.from_env()` shrinks to the three core env vars (`PYMODULES_MAX_WORKERS`, `PYMODULES_PROPAGATE_EXCEPTIONS`, `PYMODULES_LOG_LEVEL`). Env reading for everything else lives with the concern: `pymodules.resilience.default_middleware_from_env()` reads the resilience env vars, `pymodules.tracing.middleware_from_env()` reads the tracing env vars, and each contrib's existing `*Config.from_env()` continues to read its own. Core never reads `PYMODULES_REDIS_URL` or `PYMODULES_CONSUL_*` — those are contrib's concern. A user wiring everything from env writes two calls, one core and one per middleware family.
