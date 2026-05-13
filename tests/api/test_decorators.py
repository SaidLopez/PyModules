"""Tests for API decorators."""

from __future__ import annotations

from dataclasses import dataclass


class TestApiEndpointDecorator:
    """Tests for @api_endpoint decorator."""

    def test_sets_path_metadata(self) -> None:
        """@api_endpoint should set path metadata on class."""
        from pymodules import Command, CommandRequest, CommandResponse
        from pymodules.contrib.api import api_endpoint, get_api_metadata

        @dataclass
        class TestInput(CommandRequest):
            value: str

        @dataclass
        class TestOutput(CommandResponse):
            result: str

        @api_endpoint(path="/custom/path")
        class CustomCommand(Command[TestInput, TestOutput]):
            pass

        metadata = get_api_metadata(CustomCommand)
        assert metadata is not None
        assert metadata["path"] == "/custom/path"

    def test_sets_method_metadata(self) -> None:
        """@api_endpoint should set HTTP method metadata."""
        from pymodules import Command, CommandRequest, CommandResponse
        from pymodules.contrib.api import HTTPMethod, api_endpoint, get_api_metadata

        @dataclass
        class TestInput(CommandRequest):
            value: str

        @dataclass
        class TestOutput(CommandResponse):
            result: str

        @api_endpoint(method=HTTPMethod.PUT)
        class CustomCommand(Command[TestInput, TestOutput]):
            pass

        metadata = get_api_metadata(CustomCommand)
        assert metadata is not None
        assert metadata["method"] == "PUT"

    def test_sets_tags_metadata(self) -> None:
        """@api_endpoint should set tags metadata."""
        from pymodules import Command, CommandRequest, CommandResponse
        from pymodules.contrib.api import api_endpoint, get_api_metadata

        @dataclass
        class TestInput(CommandRequest):
            value: str

        @dataclass
        class TestOutput(CommandResponse):
            result: str

        @api_endpoint(tags=["custom", "test"])
        class CustomCommand(Command[TestInput, TestOutput]):
            pass

        metadata = get_api_metadata(CustomCommand)
        assert metadata is not None
        assert metadata["tags"] == ["custom", "test"]

    def test_sets_summary_metadata(self) -> None:
        """@api_endpoint should set summary metadata."""
        from pymodules import Command, CommandRequest, CommandResponse
        from pymodules.contrib.api import api_endpoint, get_api_metadata

        @dataclass
        class TestInput(CommandRequest):
            value: str

        @dataclass
        class TestOutput(CommandResponse):
            result: str

        @api_endpoint(summary="Custom summary")
        class CustomCommand(Command[TestInput, TestOutput]):
            pass

        metadata = get_api_metadata(CustomCommand)
        assert metadata is not None
        assert metadata["summary"] == "Custom summary"

    def test_preserves_class_behavior(self) -> None:
        """@api_endpoint should not alter class behavior."""
        from pymodules import Command, CommandRequest, CommandResponse
        from pymodules.contrib.api import api_endpoint

        @dataclass
        class TestInput(CommandRequest):
            value: str

        @dataclass
        class TestOutput(CommandResponse):
            result: str

        @api_endpoint(path="/test")
        class CustomCommand(Command[TestInput, TestOutput]):
            pass

        # Class should still be instantiable and work as Command
        cmd = CustomCommand(input=TestInput(value="test"))
        assert cmd.input.value == "test"


class TestExcludeFromApiDecorator:
    """Tests for @exclude_from_api decorator."""

    def test_marks_command_as_excluded(self) -> None:
        """@exclude_from_api should mark command for exclusion."""
        from pymodules import Command, CommandRequest, CommandResponse
        from pymodules.contrib.api import exclude_from_api, is_excluded_from_api

        @dataclass
        class TestInput(CommandRequest):
            value: str

        @dataclass
        class TestOutput(CommandResponse):
            result: str

        @exclude_from_api
        class InternalCommand(Command[TestInput, TestOutput]):
            pass

        assert is_excluded_from_api(InternalCommand) is True

    def test_excluded_commands_not_discovered(self, app, host) -> None:
        """Excluded commands should not be registered by ModuleRouter."""
        from pymodules import Command, CommandRequest, CommandResponse
        from pymodules.contrib.api import ModuleRouter, exclude_from_api

        @dataclass
        class TestInput(CommandRequest):
            value: str

        @dataclass
        class TestOutput(CommandResponse):
            result: str

        @exclude_from_api
        class InternalCommand(Command[TestInput, TestOutput]):
            pass

        class PublicCommand(Command[TestInput, TestOutput]):
            pass

        router = ModuleRouter(host)

        # Register both - only public should be added
        router.register_command(PublicCommand)
        router.register_command(InternalCommand)

        # Get registered routes
        routes = [r.path for r in router.router.routes]

        # InternalCommand should not have a route
        assert not any("internal" in r.lower() for r in routes)
