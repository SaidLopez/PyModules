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

    def test_register_command_creates_endpoint(self, host, sample_commands, app) -> None:
        """register_command should create an HTTP endpoint from @api_endpoint metadata."""
        from pymodules.contrib.api import ModuleRouter

        router = ModuleRouter(host)
        router.register_command(sample_commands["CreateUser"])

        routes = [r.path for r in router.router.routes if hasattr(r, "path")]
        assert any("user" in r.lower() for r in routes)

    def test_skips_command_without_api_endpoint(self, host) -> None:
        """Commands without @api_endpoint should be skipped silently."""
        from dataclasses import dataclass

        from pymodules import Command, CommandRequest, CommandResponse
        from pymodules.contrib.api import ModuleRouter

        @dataclass
        class InternalInput(CommandRequest):
            value: str

        @dataclass
        class InternalOutput(CommandResponse):
            result: str

        class InternalCommand(Command[InternalInput, InternalOutput]):
            pass

        router = ModuleRouter(host)
        router.register_command(InternalCommand)

        assert len(router.router.routes) == 0

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
        from pymodules.contrib.api import ModuleRouter, api_endpoint, exclude_from_api

        @dataclass
        class TestInput(CommandRequest):
            value: str

        @dataclass
        class TestOutput(CommandResponse):
            result: str

        @exclude_from_api
        @api_endpoint(method="POST", path="/excluded")
        class ExcludedCommand(Command[TestInput, TestOutput]):
            pass

        router = ModuleRouter(host)
        initial_count = len(router.router.routes)

        router.register_command(ExcludedCommand)
        assert len(router.router.routes) == initial_count

    def test_uses_explicit_method_and_path(self, host) -> None:
        """The route's method and path come from @api_endpoint metadata."""
        from dataclasses import dataclass

        from pymodules import Command, CommandRequest, CommandResponse
        from pymodules.contrib.api import ModuleRouter, api_endpoint

        @dataclass
        class ArchiveInput(CommandRequest):
            id: str = ""

        @dataclass
        class ArchiveOutput(CommandResponse):
            success: bool

        @api_endpoint(method="POST", path="/widgets/{id}/archive")
        class ArchiveWidget(Command[ArchiveInput, ArchiveOutput]):
            pass

        router = ModuleRouter(host)
        router.register_command(ArchiveWidget)

        routes = [(r.path, r.methods) for r in router.router.routes if hasattr(r, "methods")]
        assert ("/widgets/{id}/archive", {"POST"}) in [(p, m) for p, m in routes]

    @pytest.mark.asyncio
    async def test_endpoint_dispatches_to_host(self, host, sample_commands, sample_module) -> None:
        """Endpoint should dispatch command to ModuleHost."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.contrib.api import ModuleRouter

        host.register(sample_module)

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_command(sample_commands["CreateUser"])
        app.include_router(router.router)

        client = TestClient(app)
        response = client.post("/users", json={"name": "Test", "email": "test@test.com"})
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
        response = client.post("/users", json={"name": "Test", "email": "test@test.com"})
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
        response = client.post("/users", json={"name": "Test", "email": "test@test.com"})
        assert response.status_code >= 400

    def test_mount_includes_router(self, host, sample_commands, app) -> None:
        """mount() should include router in FastAPI app."""
        from pymodules.contrib.api import ModuleRouter

        router = ModuleRouter(host)
        router.register_command(sample_commands["CreateUser"])
        router.mount(app)

        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("user" in r.lower() for r in routes)

    def test_discover_commands_registers_endpoints(self, host, tmp_path) -> None:
        """discover_commands should find and register @api_endpoint-decorated commands."""
        import sys

        pkg_dir = tmp_path / "discover_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "commands.py").write_text("""
from dataclasses import dataclass
from pymodules import Command, CommandRequest, CommandResponse
from pymodules.contrib.api import api_endpoint

@dataclass
class DiscoverInput(CommandRequest):
    value: str

@dataclass
class DiscoverOutput(CommandResponse):
    result: str

@api_endpoint(method="POST", path="/discover")
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

    def test_discover_skips_undecorated_commands(self, host, tmp_path) -> None:
        """discover_commands should skip commands without @api_endpoint."""
        import sys

        pkg_dir = tmp_path / "skip_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "commands.py").write_text("""
from dataclasses import dataclass
from pymodules import Command, CommandRequest, CommandResponse
from pymodules.contrib.api import api_endpoint

@dataclass
class PublicInput(CommandRequest):
    value: str

@dataclass
class PublicOutput(CommandResponse):
    result: str

@dataclass
class InternalInput(CommandRequest):
    value: str

@dataclass
class InternalOutput(CommandResponse):
    result: str

@api_endpoint(method="POST", path="/public")
class PublicCommand(Command[PublicInput, PublicOutput]):
    pass

class InternalCommand(Command[InternalInput, InternalOutput]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.contrib.api import ModuleRouter

            router = ModuleRouter(host)
            count = router.discover_commands("skip_pkg")

            assert count == 1
        finally:
            sys.path.remove(str(tmp_path))
