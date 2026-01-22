"""Tests for ModuleRouter."""

from __future__ import annotations

import pytest


class TestModuleRouter:
    """Tests for ModuleRouter class."""

    def test_init_with_host(self, host) -> None:
        """ModuleRouter should accept ModuleHost in constructor."""
        from pymodules.api import ModuleRouter

        router = ModuleRouter(host)

        assert router.host is host

    def test_init_with_custom_convention(self, host) -> None:
        """ModuleRouter should accept custom RouteConvention."""
        from pymodules.api import ModuleRouter, RESTConvention

        convention = RESTConvention()
        router = ModuleRouter(host, convention=convention)

        assert router.convention is convention

    def test_register_event_creates_endpoint(self, host, sample_events, app) -> None:
        """register_event should create an HTTP endpoint."""
        from pymodules.api import ModuleRouter

        router = ModuleRouter(host)
        router.register_event(sample_events["CreateUser"])

        # Check that route was added
        routes = [r.path for r in router.router.routes if hasattr(r, "path")]
        assert any("user" in r.lower() for r in routes)

    def test_skips_duplicate_events(self, host, sample_events) -> None:
        """ModuleRouter should not register the same event twice."""
        from pymodules.api import ModuleRouter

        router = ModuleRouter(host)
        router.register_event(sample_events["CreateUser"])
        initial_count = len(router.router.routes)

        router.register_event(sample_events["CreateUser"])
        assert len(router.router.routes) == initial_count

    def test_skips_excluded_events(self, host) -> None:
        """ModuleRouter should skip events with @exclude_from_api."""
        from dataclasses import dataclass

        from pymodules import Event, EventInput, EventOutput
        from pymodules.api import ModuleRouter, exclude_from_api

        @dataclass
        class TestInput(EventInput):
            value: str

        @dataclass
        class TestOutput(EventOutput):
            result: str

        @exclude_from_api
        class ExcludedEvent(Event[TestInput, TestOutput]):
            pass

        router = ModuleRouter(host)
        initial_count = len(router.router.routes)

        router.register_event(ExcludedEvent)
        assert len(router.router.routes) == initial_count

    @pytest.mark.asyncio
    async def test_endpoint_dispatches_to_host(self, host, sample_events, sample_module) -> None:
        """Endpoint should dispatch event to ModuleHost."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api import ModuleRouter

        # Register module with host
        host.register(sample_module)

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_event(sample_events["CreateUser"])
        app.include_router(router.router)

        client = TestClient(app)

        # Find the create user route
        routes = [(r.path, r.methods) for r in app.routes if hasattr(r, "methods")]
        create_route = next((r for r, m in routes if "user" in r.lower() and "POST" in m), None)

        if create_route:
            response = client.post(create_route, json={"name": "Test", "email": "test@test.com"})
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_endpoint_returns_output(self, host, sample_events, sample_module) -> None:
        """Endpoint should return event output as JSON."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api import ModuleRouter

        host.register(sample_module)

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_event(sample_events["CreateUser"])
        app.include_router(router.router)

        client = TestClient(app)
        routes = [(r.path, r.methods) for r in app.routes if hasattr(r, "methods")]
        create_route = next((r for r, m in routes if "user" in r.lower() and "POST" in m), None)

        if create_route:
            response = client.post(create_route, json={"name": "Test", "email": "test@test.com"})
            data = response.json()
            assert "id" in data
            assert data["name"] == "Test"

    def test_endpoint_handles_errors(self, host, sample_events) -> None:
        """Endpoint should handle and return errors appropriately."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules import Module, module
        from pymodules.api import ModuleRouter, register_error_handlers

        @module(name="failing")
        class FailingModule(Module):
            def can_handle(self, event):
                return True

            async def handle(self, event):
                raise ValueError("Something went wrong")

        host.register(FailingModule())

        app = FastAPI()
        register_error_handlers(app)
        router = ModuleRouter(host)
        router.register_event(sample_events["CreateUser"])
        app.include_router(router.router)

        client = TestClient(app, raise_server_exceptions=False)
        routes = [(r.path, r.methods) for r in app.routes if hasattr(r, "methods")]
        create_route = next((r for r, m in routes if "user" in r.lower() and "POST" in m), None)

        if create_route:
            response = client.post(create_route, json={"name": "Test", "email": "test@test.com"})
            assert response.status_code >= 400

    def test_mount_includes_router(self, host, sample_events, app) -> None:
        """mount() should include router in FastAPI app."""
        from pymodules.api import ModuleRouter

        router = ModuleRouter(host)
        router.register_event(sample_events["CreateUser"])
        router.mount(app)

        # App should now have the routes
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("user" in r.lower() for r in routes)

    def test_discover_events_registers_endpoints(self, host, tmp_path) -> None:
        """discover_events should find and register all events."""
        import sys

        # Create a temporary package with events
        pkg_dir = tmp_path / "discover_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "events.py").write_text("""
from dataclasses import dataclass
from pymodules import Event, EventInput, EventOutput

@dataclass
class DiscoverInput(EventInput):
    value: str

@dataclass
class DiscoverOutput(EventOutput):
    result: str

class DiscoverEvent(Event[DiscoverInput, DiscoverOutput]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.api import ModuleRouter

            router = ModuleRouter(host)
            count = router.discover_events("discover_pkg")

            assert count >= 1
            routes = [r.path for r in router.router.routes if hasattr(r, "path")]
            assert any("discover" in r.lower() for r in routes)
        finally:
            sys.path.remove(str(tmp_path))

    def test_discover_returns_count(self, host, tmp_path) -> None:
        """discover_events should return number of registered events."""
        import sys

        pkg_dir = tmp_path / "count_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "events.py").write_text("""
from dataclasses import dataclass
from pymodules import Event, EventInput, EventOutput

@dataclass
class Input1(EventInput):
    value: str

@dataclass
class Output1(EventOutput):
    result: str

class Event1(Event[Input1, Output1]):
    pass

@dataclass
class Input2(EventInput):
    value: str

@dataclass
class Output2(EventOutput):
    result: str

class Event2(Event[Input2, Output2]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.api import ModuleRouter

            router = ModuleRouter(host)
            count = router.discover_events("count_pkg")

            assert count == 2
        finally:
            sys.path.remove(str(tmp_path))
