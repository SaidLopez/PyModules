"""Command auto-discovery module.

Scans packages for Command classes and extracts metadata for API generation.
"""

from __future__ import annotations

import fnmatch
import importlib
import inspect
import pkgutil
from dataclasses import dataclass, field
from typing import Any, get_args, get_origin

from pymodules import Command, CommandRequest, CommandResponse

from .decorators import get_api_metadata, is_excluded_from_api


@dataclass
class DiscoveredCommand:
    """Represents a discovered Command class with its metadata."""

    command_class: type[Command[Any, Any]]
    input_class: type[CommandRequest]
    output_class: type[CommandResponse] | None
    command_name: str = ""
    api_metadata: dict[str, Any] = field(default_factory=dict)


class CommandDiscovery:
    """Discovers Command classes from Python packages.

    Scans a package and its subpackages for Command class definitions,
    extracting metadata needed for API endpoint generation.

    Example:
        discovery = CommandDiscovery()
        commands = discovery.discover("myapp.commands")
        for cmd in commands:
            print(cmd.command_name)
    """

    def __init__(
        self,
        package_name: str | None = None,
        exclude_patterns: list[str] | None = None,
    ):
        """Initialize the discovery engine.

        Args:
            package_name: Root package to scan for Command classes (optional)
            exclude_patterns: Glob patterns for modules to exclude
        """
        self.package_name = package_name
        self.exclude_patterns = exclude_patterns or []
        self._discovered: list[DiscoveredCommand] = []

    def discover(self, package_name: str | None = None) -> list[DiscoveredCommand]:
        """Discover all Command classes in the package.

        Args:
            package_name: Package to scan (overrides constructor value)

        Returns:
            List of discovered commands with their metadata
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
        """Recursively scan a package for Command classes."""
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
        """Check if a module should be scanned for commands."""
        parts = modname.split(".")
        last_part = parts[-1]

        # Check exclude patterns
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(last_part, pattern):
                return False

        # Only scan modules named or ending with "commands"
        return last_part.endswith("commands") or last_part == "commands"

    def _scan_module(self, module: Any) -> None:
        """Scan a single module for Command classes."""
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if self._is_command_class(obj) and obj.__module__ == module.__name__:
                discovered = self._extract_command_metadata(obj)
                if discovered:
                    self._discovered.append(discovered)

    def _is_command_class(self, cls: type) -> bool:
        """Check if a class is a concrete Command subclass."""
        if not isinstance(cls, type):
            return False
        if cls is Command:
            return False
        try:
            if not issubclass(cls, Command):
                return False
        except TypeError:
            return False
        return True

    def _extract_command_metadata(
        self, command_class: type[Command[Any, Any]]
    ) -> DiscoveredCommand | None:
        """Extract metadata from a Command class."""
        if is_excluded_from_api(command_class):
            return None

        command_name = getattr(command_class, "name", "") or ""

        input_class, output_class = self._extract_type_params(command_class)
        if not input_class:
            return None

        api_metadata = get_api_metadata(command_class)

        return DiscoveredCommand(
            command_class=command_class,
            input_class=input_class,
            output_class=output_class,
            command_name=command_name,
            api_metadata=api_metadata,
        )

    def _extract_type_params(
        self, command_class: type[Command[Any, Any]]
    ) -> tuple[type[CommandRequest] | None, type[CommandResponse] | None]:
        """Extract request and response type parameters from a Command class."""
        for base in getattr(command_class, "__orig_bases__", []):
            origin = get_origin(base)
            if origin is Command:
                args = get_args(base)
                if len(args) >= 1:
                    input_class = args[0] if args[0] is not type(None) else None
                    output_class = args[1] if len(args) > 1 and args[1] is not type(None) else None
                    return input_class, output_class

        return None, None


def discover_commands(package_name: str) -> list[DiscoveredCommand]:
    """Convenience function to discover commands from a package.

    Args:
        package_name: Root package to scan

    Returns:
        List of discovered commands
    """
    return CommandDiscovery().discover(package_name)
