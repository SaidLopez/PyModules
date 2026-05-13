"""Tests for CommandDiscovery."""

from __future__ import annotations


class TestDiscoveredCommand:
    """Tests for DiscoveredCommand dataclass."""

    def test_stores_command_class(self, sample_commands) -> None:
        """DiscoveredCommand should store the command class."""
        from pymodules.contrib.api import DiscoveredCommand

        discovered = DiscoveredCommand(
            command_class=sample_commands["CreateUser"],
            input_class=sample_commands["CreateUserInput"],
            output_class=sample_commands["CreateUserOutput"],
        )

        assert discovered.command_class is sample_commands["CreateUser"]

    def test_stores_input_type(self, sample_commands) -> None:
        """DiscoveredCommand should store the request type."""
        from pymodules.contrib.api import DiscoveredCommand

        discovered = DiscoveredCommand(
            command_class=sample_commands["CreateUser"],
            input_class=sample_commands["CreateUserInput"],
            output_class=sample_commands["CreateUserOutput"],
        )

        assert discovered.input_class is sample_commands["CreateUserInput"]

    def test_stores_output_type(self, sample_commands) -> None:
        """DiscoveredCommand should store the response type."""
        from pymodules.contrib.api import DiscoveredCommand

        discovered = DiscoveredCommand(
            command_class=sample_commands["CreateUser"],
            input_class=sample_commands["CreateUserInput"],
            output_class=sample_commands["CreateUserOutput"],
        )

        assert discovered.output_class is sample_commands["CreateUserOutput"]


class TestCommandDiscovery:
    """Tests for CommandDiscovery class."""

    def test_scan_finds_command_classes(self, tmp_path) -> None:
        """CommandDiscovery should find Command subclasses in packages."""
        import sys

        # Create a temporary package with commands
        pkg_dir = tmp_path / "test_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "commands.py").write_text("""
from dataclasses import dataclass
from pymodules import Command, CommandRequest, CommandResponse

@dataclass
class TestInput(CommandRequest):
    value: str

@dataclass
class TestOutput(CommandResponse):
    result: str

class TestCommand(Command[TestInput, TestOutput]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.contrib.api import CommandDiscovery

            discovery = CommandDiscovery()
            commands = discovery.discover("test_pkg")

            assert len(commands) >= 1
            names = [c.command_class.__name__ for c in commands]
            assert "TestCommand" in names
        finally:
            sys.path.remove(str(tmp_path))

    def test_scan_ignores_non_commands(self, tmp_path) -> None:
        """CommandDiscovery should ignore non-Command classes."""
        import sys

        pkg_dir = tmp_path / "test_pkg2"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "commands.py").write_text("""
from dataclasses import dataclass
from pymodules import Command, CommandRequest, CommandResponse

class NotACommand:
    pass

@dataclass
class JustADataclass:
    value: str

@dataclass
class RealInput(CommandRequest):
    x: int

@dataclass
class RealOutput(CommandResponse):
    y: int

class RealCommand(Command[RealInput, RealOutput]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.contrib.api import CommandDiscovery

            discovery = CommandDiscovery()
            commands = discovery.discover("test_pkg2")

            names = [c.command_class.__name__ for c in commands]
            assert "NotACommand" not in names
            assert "JustADataclass" not in names
            assert "RealCommand" in names
        finally:
            sys.path.remove(str(tmp_path))

    def test_scan_recursive(self, tmp_path) -> None:
        """CommandDiscovery should scan subpackages recursively."""
        import sys

        # Create nested package structure
        pkg_dir = tmp_path / "test_pkg3"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")

        sub_dir = pkg_dir / "submodule"
        sub_dir.mkdir()
        (sub_dir / "__init__.py").write_text("")
        (sub_dir / "commands.py").write_text("""
from dataclasses import dataclass
from pymodules import Command, CommandRequest, CommandResponse

@dataclass
class NestedInput(CommandRequest):
    value: str

@dataclass
class NestedOutput(CommandResponse):
    result: str

class NestedCommand(Command[NestedInput, NestedOutput]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.contrib.api import CommandDiscovery

            discovery = CommandDiscovery()
            commands = discovery.discover("test_pkg3")

            names = [c.command_class.__name__ for c in commands]
            assert "NestedCommand" in names
        finally:
            sys.path.remove(str(tmp_path))

    def test_scan_respects_exclude_patterns(self, tmp_path) -> None:
        """CommandDiscovery should respect exclude patterns."""
        import sys

        pkg_dir = tmp_path / "test_pkg4"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "commands.py").write_text("""
from dataclasses import dataclass
from pymodules import Command, CommandRequest, CommandResponse

@dataclass
class IncludedInput(CommandRequest):
    value: str

@dataclass
class IncludedOutput(CommandResponse):
    result: str

class IncludedCommand(Command[IncludedInput, IncludedOutput]):
    pass
""")
        (pkg_dir / "internal_commands.py").write_text("""
from dataclasses import dataclass
from pymodules import Command, CommandRequest, CommandResponse

@dataclass
class ExcludedInput(CommandRequest):
    value: str

@dataclass
class ExcludedOutput(CommandResponse):
    result: str

class ExcludedCommand(Command[ExcludedInput, ExcludedOutput]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.contrib.api import CommandDiscovery

            discovery = CommandDiscovery(exclude_patterns=["internal_*"])
            commands = discovery.discover("test_pkg4")

            names = [c.command_class.__name__ for c in commands]
            assert "IncludedCommand" in names
            assert "ExcludedCommand" not in names
        finally:
            sys.path.remove(str(tmp_path))

    def test_extracts_input_output_types(self, sample_commands) -> None:
        """CommandDiscovery should extract request/response types from generics."""
        from pymodules.contrib.api import CommandDiscovery

        discovery = CommandDiscovery()
        input_cls, output_cls = discovery._extract_type_params(sample_commands["CreateUser"])

        assert input_cls is sample_commands["CreateUserInput"]
        assert output_cls is sample_commands["CreateUserOutput"]

    def test_respects_exclude_decorator(self, tmp_path) -> None:
        """CommandDiscovery should respect @exclude_from_api decorator."""
        import sys

        pkg_dir = tmp_path / "test_pkg5"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "commands.py").write_text("""
from dataclasses import dataclass
from pymodules import Command, CommandRequest, CommandResponse
from pymodules.contrib.api import exclude_from_api

@dataclass
class PublicInput(CommandRequest):
    value: str

@dataclass
class PublicOutput(CommandResponse):
    result: str

class PublicCommand(Command[PublicInput, PublicOutput]):
    pass

@dataclass
class PrivateInput(CommandRequest):
    value: str

@dataclass
class PrivateOutput(CommandResponse):
    result: str

@exclude_from_api
class PrivateCommand(Command[PrivateInput, PrivateOutput]):
    pass
""")

        sys.path.insert(0, str(tmp_path))
        try:
            from pymodules.contrib.api import CommandDiscovery

            discovery = CommandDiscovery()
            commands = discovery.discover("test_pkg5")

            names = [c.command_class.__name__ for c in commands]
            assert "PublicCommand" in names
            assert "PrivateCommand" not in names
        finally:
            sys.path.remove(str(tmp_path))
