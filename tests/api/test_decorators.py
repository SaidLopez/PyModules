"""Tests for API decorators."""

from __future__ import annotations

from dataclasses import dataclass


class TestApiEndpointDecorator:
    """Tests for @api_endpoint decorator."""

    def test_sets_path_metadata(self) -> None:
        """@api_endpoint should set path metadata on class."""
        from pymodules import Event, EventInput, EventOutput
        from pymodules.api import api_endpoint, get_api_metadata

        @dataclass
        class TestInput(EventInput):
            value: str

        @dataclass
        class TestOutput(EventOutput):
            result: str

        @api_endpoint(path="/custom/path")
        class CustomEvent(Event[TestInput, TestOutput]):
            pass

        metadata = get_api_metadata(CustomEvent)
        assert metadata is not None
        assert metadata["path"] == "/custom/path"

    def test_sets_method_metadata(self) -> None:
        """@api_endpoint should set HTTP method metadata."""
        from pymodules import Event, EventInput, EventOutput
        from pymodules.api import HTTPMethod, api_endpoint, get_api_metadata

        @dataclass
        class TestInput(EventInput):
            value: str

        @dataclass
        class TestOutput(EventOutput):
            result: str

        @api_endpoint(method=HTTPMethod.PUT)
        class CustomEvent(Event[TestInput, TestOutput]):
            pass

        metadata = get_api_metadata(CustomEvent)
        assert metadata is not None
        assert metadata["method"] == "PUT"

    def test_sets_tags_metadata(self) -> None:
        """@api_endpoint should set tags metadata."""
        from pymodules import Event, EventInput, EventOutput
        from pymodules.api import api_endpoint, get_api_metadata

        @dataclass
        class TestInput(EventInput):
            value: str

        @dataclass
        class TestOutput(EventOutput):
            result: str

        @api_endpoint(tags=["custom", "test"])
        class CustomEvent(Event[TestInput, TestOutput]):
            pass

        metadata = get_api_metadata(CustomEvent)
        assert metadata is not None
        assert metadata["tags"] == ["custom", "test"]

    def test_sets_summary_metadata(self) -> None:
        """@api_endpoint should set summary metadata."""
        from pymodules import Event, EventInput, EventOutput
        from pymodules.api import api_endpoint, get_api_metadata

        @dataclass
        class TestInput(EventInput):
            value: str

        @dataclass
        class TestOutput(EventOutput):
            result: str

        @api_endpoint(summary="Custom summary")
        class CustomEvent(Event[TestInput, TestOutput]):
            pass

        metadata = get_api_metadata(CustomEvent)
        assert metadata is not None
        assert metadata["summary"] == "Custom summary"

    def test_preserves_class_behavior(self) -> None:
        """@api_endpoint should not alter class behavior."""
        from pymodules import Event, EventInput, EventOutput
        from pymodules.api import api_endpoint

        @dataclass
        class TestInput(EventInput):
            value: str

        @dataclass
        class TestOutput(EventOutput):
            result: str

        @api_endpoint(path="/test")
        class CustomEvent(Event[TestInput, TestOutput]):
            pass

        # Class should still be instantiable and work as Event
        event = CustomEvent(input=TestInput(value="test"))
        assert event.input.value == "test"


class TestExcludeFromApiDecorator:
    """Tests for @exclude_from_api decorator."""

    def test_marks_event_as_excluded(self) -> None:
        """@exclude_from_api should mark event for exclusion."""
        from pymodules import Event, EventInput, EventOutput
        from pymodules.api import exclude_from_api, is_excluded_from_api

        @dataclass
        class TestInput(EventInput):
            value: str

        @dataclass
        class TestOutput(EventOutput):
            result: str

        @exclude_from_api
        class InternalEvent(Event[TestInput, TestOutput]):
            pass

        assert is_excluded_from_api(InternalEvent) is True

    def test_excluded_events_not_discovered(self, app, host) -> None:
        """Excluded events should not be registered by ModuleRouter."""
        from pymodules import Event, EventInput, EventOutput
        from pymodules.api import ModuleRouter, exclude_from_api

        @dataclass
        class TestInput(EventInput):
            value: str

        @dataclass
        class TestOutput(EventOutput):
            result: str

        @exclude_from_api
        class InternalEvent(Event[TestInput, TestOutput]):
            pass

        class PublicEvent(Event[TestInput, TestOutput]):
            pass

        router = ModuleRouter(host)

        # Register both - only public should be added
        router.register_event(PublicEvent)
        router.register_event(InternalEvent)

        # Get registered routes
        routes = [r.path for r in router.router.routes]

        # InternalEvent should not have a route
        assert not any("internal" in r.lower() for r in routes)
