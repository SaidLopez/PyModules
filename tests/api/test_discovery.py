"""Tests for EventDiscovery."""

from __future__ import annotations

import pytest


class TestDiscoveredEvent:
    """Tests for DiscoveredEvent dataclass."""

    def test_stores_event_class(self, sample_events) -> None:
        """DiscoveredEvent should store the event class."""
        from pymodules.api import DiscoveredEvent

        discovered = DiscoveredEvent(
            event_class=sample_events["CreateUser"],
            input_class=sample_events["CreateUserInput"],
            output_class=sample_events["CreateUserOutput"],
        )

        assert discovered.event_class is sample_events["CreateUser"]

    def test_stores_input_type(self, sample_events) -> None:
        """DiscoveredEvent should store the input type."""
        from pymodules.api import DiscoveredEvent

        discovered = DiscoveredEvent(
            event_class=sample_events["CreateUser"],
            input_class=sample_events["CreateUserInput"],
            output_class=sample_events["CreateUserOutput"],
        )

        assert discovered.input_class is sample_events["CreateUserInput"]

    def test_stores_output_type(self, sample_events) -> None:
        """DiscoveredEvent should store the output type."""
        from pymodules.api import DiscoveredEvent

        discovered = DiscoveredEvent(
            event_class=sample_events["CreateUser"],
            input_class=sample_events["CreateUserInput"],
            output_class=sample_events["CreateUserOutput"],
        )

        assert discovered.output_class is sample_events["CreateUserOutput"]


class TestEventDiscovery:
    """Tests for EventDiscovery class."""

    def test_scan_finds_event_classes(self, tmp_path) -> None:
        """EventDiscovery should find Event subclasses in packages."""
        import sys

        # Create a temporary package with events
        pkg_dir = tmp_path / "test_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "events.py").write_text("""
from dataclasses import dataclass
from pymodules import Event, EventInput, EventOutput

@dataclass
class TestInput(EventInput):
    value: str

@dataclass
class TestOutput(EventOutput):
    result: str

class TestEvent(Event[TestInput, TestOutput]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.api import EventDiscovery

            discovery = EventDiscovery()
            events = discovery.discover("test_pkg")

            assert len(events) >= 1
            event_names = [e.event_class.__name__ for e in events]
            assert "TestEvent" in event_names
        finally:
            sys.path.remove(str(tmp_path))

    def test_scan_ignores_non_events(self, tmp_path) -> None:
        """EventDiscovery should ignore non-Event classes."""
        import sys

        pkg_dir = tmp_path / "test_pkg2"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "events.py").write_text("""
from dataclasses import dataclass
from pymodules import Event, EventInput, EventOutput

class NotAnEvent:
    pass

@dataclass
class JustADataclass:
    value: str

@dataclass
class RealInput(EventInput):
    x: int

@dataclass
class RealOutput(EventOutput):
    y: int

class RealEvent(Event[RealInput, RealOutput]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.api import EventDiscovery

            discovery = EventDiscovery()
            events = discovery.discover("test_pkg2")

            event_names = [e.event_class.__name__ for e in events]
            assert "NotAnEvent" not in event_names
            assert "JustADataclass" not in event_names
            assert "RealEvent" in event_names
        finally:
            sys.path.remove(str(tmp_path))

    def test_scan_recursive(self, tmp_path) -> None:
        """EventDiscovery should scan subpackages recursively."""
        import sys

        # Create nested package structure
        pkg_dir = tmp_path / "test_pkg3"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")

        sub_dir = pkg_dir / "submodule"
        sub_dir.mkdir()
        (sub_dir / "__init__.py").write_text("")
        (sub_dir / "events.py").write_text("""
from dataclasses import dataclass
from pymodules import Event, EventInput, EventOutput

@dataclass
class NestedInput(EventInput):
    value: str

@dataclass
class NestedOutput(EventOutput):
    result: str

class NestedEvent(Event[NestedInput, NestedOutput]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.api import EventDiscovery

            discovery = EventDiscovery()
            events = discovery.discover("test_pkg3")

            event_names = [e.event_class.__name__ for e in events]
            assert "NestedEvent" in event_names
        finally:
            sys.path.remove(str(tmp_path))

    def test_scan_respects_exclude_patterns(self, tmp_path) -> None:
        """EventDiscovery should respect exclude patterns."""
        import sys

        pkg_dir = tmp_path / "test_pkg4"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "events.py").write_text("""
from dataclasses import dataclass
from pymodules import Event, EventInput, EventOutput

@dataclass
class IncludedInput(EventInput):
    value: str

@dataclass
class IncludedOutput(EventOutput):
    result: str

class IncludedEvent(Event[IncludedInput, IncludedOutput]):
    pass
""")
        (pkg_dir / "internal_events.py").write_text("""
from dataclasses import dataclass
from pymodules import Event, EventInput, EventOutput

@dataclass
class ExcludedInput(EventInput):
    value: str

@dataclass
class ExcludedOutput(EventOutput):
    result: str

class ExcludedEvent(Event[ExcludedInput, ExcludedOutput]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.api import EventDiscovery

            discovery = EventDiscovery(exclude_patterns=["internal_*"])
            events = discovery.discover("test_pkg4")

            event_names = [e.event_class.__name__ for e in events]
            assert "IncludedEvent" in event_names
            assert "ExcludedEvent" not in event_names
        finally:
            sys.path.remove(str(tmp_path))

    def test_extracts_input_output_types(self, sample_events) -> None:
        """EventDiscovery should extract input/output types from generics."""
        from pymodules.api import EventDiscovery

        discovery = EventDiscovery()
        input_cls, output_cls = discovery._extract_type_params(sample_events["CreateUser"])

        assert input_cls is sample_events["CreateUserInput"]
        assert output_cls is sample_events["CreateUserOutput"]

    def test_respects_exclude_decorator(self, tmp_path) -> None:
        """EventDiscovery should respect @exclude_from_api decorator."""
        import sys

        pkg_dir = tmp_path / "test_pkg5"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "events.py").write_text("""
from dataclasses import dataclass
from pymodules import Event, EventInput, EventOutput
from pymodules.api import exclude_from_api

@dataclass
class PublicInput(EventInput):
    value: str

@dataclass
class PublicOutput(EventOutput):
    result: str

class PublicEvent(Event[PublicInput, PublicOutput]):
    pass

@dataclass
class PrivateInput(EventInput):
    value: str

@dataclass
class PrivateOutput(EventOutput):
    result: str

@exclude_from_api
class PrivateEvent(Event[PrivateInput, PrivateOutput]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.api import EventDiscovery

            discovery = EventDiscovery()
            events = discovery.discover("test_pkg5")

            event_names = [e.event_class.__name__ for e in events]
            assert "PublicEvent" in event_names
            assert "PrivateEvent" not in event_names
        finally:
            sys.path.remove(str(tmp_path))
