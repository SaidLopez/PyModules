"""
Unit tests for PyModules core functionality.
"""

from dataclasses import dataclass

import pytest

from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    DuplicateCommandError,
    Module,
    ModuleHost,
    handles,
    module,
)

# Test fixtures - sample commands and modules


@dataclass
class SampleInput(CommandRequest):
    value: str = ""


@dataclass
class SampleOutput(CommandResponse):
    result: str = ""


class SampleCommand(Command[SampleInput, SampleOutput]):
    name = "test.command"


@module(name="SampleModule", description="A test module")
class SampleModule(Module):
    def __init__(self):
        super().__init__()
        self.handle_count = 0

    @handles(SampleCommand)
    def handle_sample(self, command: SampleCommand) -> None:
        self.handle_count += 1
        command.output = SampleOutput(result=f"processed: {command.input.value}")
        command.handled = True


class UnhandledCommand(Command[SampleInput, SampleOutput]):
    name = "test.unhandled"


# Tests


class TestCommandCreation:
    """Tests for Command, CommandRequest, CommandResponse creation."""

    def test_create_command_request(self):
        inp = SampleInput(value="hello")
        assert inp.value == "hello"

    def test_create_command_response(self):
        out = SampleOutput(result="world")
        assert out.result == "world"

    def test_create_command(self):
        command = SampleCommand(input=SampleInput(value="test"))
        assert command.name == "test.command"
        assert command.input.value == "test"
        assert command.output is None
        assert command.handled is False
        assert command.meta == {}

    def test_command_meta(self):
        command = SampleCommand(input=SampleInput(value="test"), meta={"key": "value"})
        assert command.meta["key"] == "value"


class TestModuleClass:
    """Tests for Module base class."""

    def test_module_metadata(self):
        mod = SampleModule()
        assert mod.metadata.name == "SampleModule"
        assert mod.metadata.description == "A test module"

    def test_module_host_initially_none(self):
        mod = SampleModule()
        assert mod.host is None


class TestModuleHostClass:
    """Tests for ModuleHost."""

    def test_register_module(self):
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        assert len(host.modules) == 1
        assert mod.host is host

    def test_unregister_module(self):
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)
        host.unregister(mod)

        assert len(host.modules) == 0
        assert mod.host is None

    def test_dispatch_command(self):
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        command = SampleCommand(input=SampleInput(value="hello"))
        result = host.dispatch(command)

        assert result is command
        assert command.handled is True
        assert command.output.result == "processed: hello"
        assert mod.handle_count == 1

    def test_unhandled_command(self):
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        command = UnhandledCommand(input=SampleInput(value="test"))
        result = host.dispatch(command)

        assert result is command
        assert command.handled is False
        assert command.output is None

    def test_host_can_handle_reflects_dispatch_table(self):
        """host.can_handle returns True for claimed Command classes."""
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        assert host.can_handle(SampleCommand(input=SampleInput())) is True
        assert host.can_handle(UnhandledCommand(input=SampleInput())) is False

    def test_get_module_by_type(self):
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        found = host.get_module(SampleModule)
        assert found is mod

    def test_get_module_by_name(self):
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        found = host.get_module_by_name("SampleModule")
        assert found is mod

    def test_method_chaining(self):
        host = ModuleHost()
        mod1 = SampleModule()
        mod2 = SampleModule()

        # Second module claims same Command class -> use override=True.
        result = host.register(mod1).register(mod2, override=True)

        assert result is host
        assert len(host.modules) == 2


class TestDuplicateClaimGuard:
    """Tests for the type-routed registry's duplicate-claim guard."""

    def test_duplicate_claim_raises(self):
        """Registering two modules that claim the same Command class raises."""
        host = ModuleHost()
        host.register(SampleModule())

        with pytest.raises(DuplicateCommandError):
            host.register(SampleModule())

    def test_override_true_replaces_handler(self):
        """register(..., override=True) silently replaces the previous claim."""
        host = ModuleHost()
        mod1 = SampleModule()
        mod2 = SampleModule()
        host.register(mod1)
        host.register(mod2, override=True)

        command = SampleCommand(input=SampleInput(value="test"))
        host.dispatch(command)

        # The second module wins after override.
        assert mod2.handle_count == 1
        assert mod1.handle_count == 0

    def test_unregister_clears_dispatch_table(self):
        """Unregistering a module removes its dispatch table entries."""
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        host.unregister(mod)

        # Re-registering a new instance must not raise.
        host.register(SampleModule())

    def test_module_can_dispatch_to_host(self):
        """Modules can dispatch commands through their host."""
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        # Simulate a module dispatching a command
        command = SampleCommand(input=SampleInput(value="from-module"))
        mod.host.dispatch(command)

        assert command.handled is True
        assert command.output.result == "processed: from-module"


@pytest.mark.asyncio
class TestAsyncDispatch:
    """Tests for async command dispatch."""

    async def test_dispatch_async(self):
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        command = SampleCommand(input=SampleInput(value="async-test"))
        result = await host.dispatch_async(command)

        assert result is command
        assert command.handled is True
        assert command.output.result == "processed: async-test"
