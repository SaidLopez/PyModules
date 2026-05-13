"""
Tests for the ``default_middleware`` / ``default_middleware_from_env`` factories.
"""

import pytest

from pymodules.resilience import (
    CircuitBreakerMiddleware,
    DLQMiddleware,
    RateLimitMiddleware,
    RetryMiddleware,
    default_middleware,
    default_middleware_from_env,
)


class TestDefaultMiddleware:
    def test_empty_by_default(self):
        assert default_middleware() == []

    def test_rate_limit_only(self):
        chain = default_middleware(rate_limit=100)
        assert len(chain) == 1
        assert isinstance(chain[0], RateLimitMiddleware)
        assert chain[0].rate == 100

    def test_standard_order(self):
        chain = default_middleware(
            rate_limit=10,
            circuit_breaker_threshold=3,
            retry_max=2,
            dlq_size=100,
        )
        assert len(chain) == 4
        assert isinstance(chain[0], RateLimitMiddleware)
        assert isinstance(chain[1], CircuitBreakerMiddleware)
        assert isinstance(chain[2], RetryMiddleware)
        assert isinstance(chain[3], DLQMiddleware)

    def test_tracing_and_metrics_added_after_resilience(self):
        chain = default_middleware(
            rate_limit=10,
            enable_tracing=True,
            enable_metrics=True,
        )
        names = [type(mw).__name__ for mw in chain]
        # Order: rate_limit, tracing, metrics
        assert names == ["RateLimitMiddleware", "TracingMiddleware", "MetricsMiddleware"]


class TestDefaultMiddlewareFromEnv:
    def test_no_env_yields_empty(self, monkeypatch):
        for var in [
            "PYMODULES_RATE_LIMIT",
            "PYMODULES_CIRCUIT_BREAKER_THRESHOLD",
            "PYMODULES_RETRY_MAX",
            "PYMODULES_DLQ_SIZE",
            "PYMODULES_ENABLE_TRACING",
            "PYMODULES_ENABLE_METRICS",
        ]:
            monkeypatch.delenv(var, raising=False)
        assert default_middleware_from_env() == []

    def test_rate_limit_env(self, monkeypatch):
        monkeypatch.setenv("PYMODULES_RATE_LIMIT", "50")
        monkeypatch.setenv("PYMODULES_RATE_LIMIT_BURST", "5")
        chain = default_middleware_from_env()
        assert len(chain) >= 1
        mw = next(m for m in chain if isinstance(m, RateLimitMiddleware))
        assert mw.rate == 50
        assert mw.burst == 5


@pytest.mark.asyncio
async def test_custom_middleware_between_defaults():
    """A user can splice a custom middleware between standard ones."""
    from dataclasses import dataclass

    from pymodules import (
        Command,
        CommandRequest,
        CommandResponse,
        Module,
        ModuleHost,
        ModuleHostConfig,
        handles,
        module,
    )

    @dataclass
    class Req(CommandRequest):
        v: str = ""

    @dataclass
    class Resp(CommandResponse):
        v: str = ""

    class Cmd(Command[Req, Resp]):
        name = "test.factory.splice"

    @module(name="M")
    class M(Module):
        @handles(Cmd)
        def h(self, command: Cmd) -> Resp:
            return Resp(v=command.request.v)

    call_log: list[str] = []

    async def custom_mw(command, next_call):
        call_log.append("custom")
        return await next_call(command)

    chain = default_middleware(
        rate_limit=1000,
        circuit_breaker_threshold=10,
        retry_max=1,
        dlq_size=10,
    )
    # Splice between rate_limit and circuit_breaker.
    chain.insert(2, custom_mw)
    assert isinstance(chain[0], RateLimitMiddleware)
    assert isinstance(chain[1], CircuitBreakerMiddleware)
    assert chain[2] is custom_mw

    host = ModuleHost(config=ModuleHostConfig(middleware=chain))
    host.register(M())

    response = await host.dispatch_async(Cmd(request=Req(v="hi")))
    assert response.v == "hi"
    assert call_log == ["custom"]
