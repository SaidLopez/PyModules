"""Tests for ModuleRouter."""

from __future__ import annotations

import pytest


class TestModuleRouter:
    """Tests for ModuleRouter class."""

    def test_init_with_host(self, host) -> None:
        """ModuleRouter should accept ModuleHost in constructor."""
        from pymodules.contrib.api import ModuleRouter

        router = ModuleRouter(host)

        assert router.host is host

    def test_init_with_custom_convention(self, host) -> None:
        """ModuleRouter should accept custom RouteConvention."""
        from pymodules.contrib.api import ModuleRouter, RESTConvention

        convention = RESTConvention()
        router = ModuleRouter(host, convention=convention)

        assert router.convention is convention

    def test_register_command_creates_endpoint(self, host, sample_commands, app) -> None:
        """register_command should create an HTTP endpoint."""
        from pymodules.contrib.api import ModuleRouter

        router = ModuleRouter(host)
        router.register_command(sample_commands["CreateUser"])

        # Check that route was added
        routes = [r.path for r in router.router.routes if hasattr(r, "path")]
        assert any("user" in r.lower() for r in routes)

    def test_skips_duplicate_commands(self, host, sample_commands) -> None:
        """ModuleRouter should not register the same command twice."""
        from pymodules.contrib.api import ModuleRouter

        router = ModuleRouter(host)
        router.register_command(sample_commands["CreateUser"])
        initial_count = len(router.router.routes)

        router.register_command(sample_commands["CreateUser"])
        assert len(router.router.routes) == initial_count

    def test_skips_excluded_commands(self, host) -> None:
        """ModuleRouter should skip commands with @exclude_from_api."""
        from dataclasses import dataclass

        from pymodules import Command, CommandRequest, CommandResponse
        from pymodules.contrib.api import ModuleRouter, exclude_from_api

        @dataclass
        class TestInput(CommandRequest):
            value: str

        @dataclass
        class TestOutput(CommandResponse):
            result: str

        @exclude_from_api
        class ExcludedCommand(Command[TestInput, TestOutput]):
            pass

        router = ModuleRouter(host)
        initial_count = len(router.router.routes)

        router.register_command(ExcludedCommand)
        assert len(router.router.routes) == initial_count

    @pytest.mark.asyncio
    async def test_endpoint_dispatches_to_host(self, host, sample_commands, sample_module) -> None:
        """Endpoint should dispatch command to ModuleHost."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.contrib.api import ModuleRouter

        # Register module with host
        host.register(sample_module)

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_command(sample_commands["CreateUser"])
        app.include_router(router.router)

        client = TestClient(app)

        # Find the create user route
        routes = [(r.path, r.methods) for r in app.routes if hasattr(r, "methods")]
        create_route = next((r for r, m in routes if "user" in r.lower() and "POST" in m), None)

        if create_route:
            response = client.post(create_route, json={"name": "Test", "email": "test@test.com"})
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_endpoint_returns_output(self, host, sample_commands, sample_module) -> None:
        """Endpoint should return command output as JSON."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.contrib.api import ModuleRouter

        host.register(sample_module)

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_command(sample_commands["CreateUser"])
        app.include_router(router.router)

        client = TestClient(app)
        routes = [(r.path, r.methods) for r in app.routes if hasattr(r, "methods")]
        create_route = next((r for r, m in routes if "user" in r.lower() and "POST" in m), None)

        if create_route:
            response = client.post(create_route, json={"name": "Test", "email": "test@test.com"})
            data = response.json()
            assert "id" in data
            assert data["name"] == "Test"

    def test_endpoint_handles_errors(self, host, sample_commands) -> None:
        """Endpoint should handle and return errors appropriately."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules import Module, handles, module
        from pymodules.contrib.api import ModuleRouter, register_error_handlers

        CreateUser = sample_commands["CreateUser"]

        @module(name="failing")
        class FailingModule(Module):
            @handles(CreateUser)
            async def fail(self, command):
                raise ValueError("Something went wrong")

        host.register(FailingModule())

        app = FastAPI()
        register_error_handlers(app)
        router = ModuleRouter(host)
        router.register_command(sample_commands["CreateUser"])
        app.include_router(router.router)

        client = TestClient(app, raise_server_exceptions=False)
        routes = [(r.path, r.methods) for r in app.routes if hasattr(r, "methods")]
        create_route = next((r for r, m in routes if "user" in r.lower() and "POST" in m), None)

        if create_route:
            response = client.post(create_route, json={"name": "Test", "email": "test@test.com"})
            assert response.status_code >= 400

    def test_mount_includes_router(self, host, sample_commands, app) -> None:
        """mount() should include router in FastAPI app."""
        from pymodules.contrib.api import ModuleRouter

        router = ModuleRouter(host)
        router.register_command(sample_commands["CreateUser"])
        router.mount(app)

        # App should now have the routes
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("user" in r.lower() for r in routes)

    def test_discover_commands_registers_endpoints(self, host, tmp_path) -> None:
        """discover_commands should find and register all commands."""
        import sys

        # Create a temporary package with commands
        pkg_dir = tmp_path / "discover_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "commands.py").write_text("""
from dataclasses import dataclass
from pymodules import Command, CommandRequest, CommandResponse

@dataclass
class DiscoverInput(CommandRequest):
    value: str

@dataclass
class DiscoverOutput(CommandResponse):
    result: str

class DiscoverCommand(Command[DiscoverInput, DiscoverOutput]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.contrib.api import ModuleRouter

            router = ModuleRouter(host)
            count = router.discover_commands("discover_pkg")

            assert count >= 1
            routes = [r.path for r in router.router.routes if hasattr(r, "path")]
            assert any("discover" in r.lower() for r in routes)
        finally:
            sys.path.remove(str(tmp_path))

    def test_discover_returns_count(self, host, tmp_path) -> None:
        """discover_commands should return number of registered commands."""
        import sys

        pkg_dir = tmp_path / "count_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "commands.py").write_text("""
from dataclasses import dataclass
from pymodules import Command, CommandRequest, CommandResponse

@dataclass
class Input1(CommandRequest):
    value: str

@dataclass
class Output1(CommandResponse):
    result: str

class Command1(Command[Input1, Output1]):
    pass

@dataclass
class Input2(CommandRequest):
    value: str

@dataclass
class Output2(CommandResponse):
    result: str

class Command2(Command[Input2, Output2]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.contrib.api import ModuleRouter

            router = ModuleRouter(host)
            count = router.discover_commands("count_pkg")

            assert count == 2
        finally:
            sys.path.remove(str(tmp_path))
