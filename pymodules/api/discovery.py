"""Event auto-discovery module.

Scans packages for Event classes and extracts metadata for API generation.
"""

from __future__ import annotations

import fnmatch
import importlib
import inspect
import pkgutil
from dataclasses import dataclass, field
from typing import Any, get_args, get_origin

from pymodules import Event, EventInput, EventOutput

from .decorators import get_api_metadata, is_excluded_from_api


@dataclass
class DiscoveredEvent:
    """Represents a discovered Event class with its metadata."""

    event_class: type[Event[Any, Any]]
    input_class: type[EventInput]
    output_class: type[EventOutput] | None
    event_name: str = ""
    domain: str = ""
    action: str = ""
    module_path: str = ""
    api_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def requires_id(self) -> bool:
        """Check if the input class has an ID field for the primary resource."""
        if not self.input_class:
            return False
        hints = getattr(self.input_class, "__annotations__", {})
        id_field = f"{self.domain}_id"
        return id_field in hints or "id" in hints


class EventDiscovery:
    """Discovers Event classes from Python packages.

    Scans a package and its subpackages for Event class definitions,
    extracting metadata needed for API endpoint generation.

    Example:
        discovery = EventDiscovery()
        events = discovery.discover("myapp.events")
        for event in events:
            print(f"{event.event_name} -> {event.domain}.{event.action}")
    """

    def __init__(
        self,
        package_name: str | None = None,
        exclude_patterns: list[str] | None = None,
    ):
        """Initialize the discovery engine.

        Args:
            package_name: Root package to scan for Event classes (optional)
            exclude_patterns: Glob patterns for modules to exclude
        """
        self.package_name = package_name
        self.exclude_patterns = exclude_patterns or []
        self._discovered: list[DiscoveredEvent] = []

    def discover(self, package_name: str | None = None) -> list[DiscoveredEvent]:
        """Discover all Event classes in the package.

        Args:
            package_name: Package to scan (overrides constructor value)

        Returns:
            List of discovered events with their metadata
        """
        pkg_name = package_name or self.package_name
        if not pkg_name:
            raise ValueError("Package name must be provided")

        self._discovered = []
        package = importlib.import_module(pkg_name)

        if hasattr(package, "__path__"):
            self._scan_package(package)
        else:
            self._scan_module(package)

        return self._discovered

    def _scan_package(self, package: Any) -> None:
        """Recursively scan a package for Event classes."""
        for _importer, modname, _ispkg in pkgutil.walk_packages(
            package.__path__, prefix=package.__name__ + "."
        ):
            if self._should_scan_module(modname):
                try:
                    module = importlib.import_module(modname)
                    self._scan_module(module)
                except ImportError:
                    continue

    def _should_scan_module(self, modname: str) -> bool:
        """Check if a module should be scanned for events."""
        parts = modname.split(".")
        last_part = parts[-1]

        # Check exclude patterns
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(last_part, pattern):
                return False

        # Only scan modules ending with "events"
        return last_part.endswith("events") or last_part == "events"

    def _scan_module(self, module: Any) -> None:
        """Scan a single module for Event classes."""
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if self._is_event_class(obj) and obj.__module__ == module.__name__:
                discovered = self._extract_event_metadata(obj, module.__name__)
                if discovered:
                    self._discovered.append(discovered)

    def _is_event_class(self, cls: type) -> bool:
        """Check if a class is a concrete Event subclass."""
        if not isinstance(cls, type):
            return False
        if cls is Event:
            return False
        try:
            if not issubclass(cls, Event):
                return False
        except TypeError:
            return False
        return True

    def _extract_event_metadata(
        self, event_class: type[Event[Any, Any]], module_path: str
    ) -> DiscoveredEvent | None:
        """Extract metadata from an Event class."""
        if is_excluded_from_api(event_class):
            return None

        event_name = getattr(event_class, "name", "") or ""

        # Parse domain and action from event name
        if event_name and "." in event_name:
            parts = event_name.split(".", 1)
            domain = parts[0]
            action = parts[1] if len(parts) > 1 else ""
        else:
            # Fall back to class name parsing
            class_name = event_class.__name__
            domain = ""
            action = ""
            for prefix in ["create", "get", "update", "delete", "list", "search"]:
                if class_name.lower().startswith(prefix):
                    action = prefix
                    domain = class_name[len(prefix):].lower()
                    break
            if not domain:
                domain = class_name.lower()

        input_class, output_class = self._extract_type_params(event_class)
        if not input_class:
            return None

        api_metadata = get_api_metadata(event_class)

        return DiscoveredEvent(
            event_class=event_class,
            input_class=input_class,
            output_class=output_class,
            event_name=event_name,
            domain=domain,
            action=action,
            module_path=module_path,
            api_metadata=api_metadata,
        )

    def _extract_type_params(
        self, event_class: type[Event[Any, Any]]
    ) -> tuple[type[EventInput] | None, type[EventOutput] | None]:
        """Extract Input and Output type parameters from an Event class."""
        for base in getattr(event_class, "__orig_bases__", []):
            origin = get_origin(base)
            if origin is Event:
                args = get_args(base)
                if len(args) >= 1:
                    input_class = args[0] if args[0] is not type(None) else None
                    output_class = args[1] if len(args) > 1 and args[1] is not type(None) else None
                    return input_class, output_class

        return None, None


def discover_events(package_name: str) -> list[DiscoveredEvent]:
    """Convenience function to discover events from a package.

    Args:
        package_name: Root package to scan

    Returns:
        List of discovered events
    """
    return EventDiscovery().discover(package_name)
