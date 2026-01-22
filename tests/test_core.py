"""
Unit tests for PyModules core functionality.
"""

from dataclasses import dataclass

import pytest

from pymodules import Event, EventInput, EventOutput, Module, ModuleHost, module

# Test fixtures - sample events and modules


@dataclass
class SampleInput(EventInput):
    value: str = ""


@dataclass
class SampleOutput(EventOutput):
    result: str = ""


class SampleEvent(Event[SampleInput, SampleOutput]):
    name = "test.event"


@module(name="SampleModule", description="A test module")
class SampleModule(Module):
    def __init__(self):
        super().__init__()
        self.handle_count = 0

    def can_handle(self, event: Event) -> bool:
        return isinstance(event, SampleEvent)

    def handle(self, event: Event) -> None:
        if isinstance(event, SampleEvent):
            self.handle_count += 1
            event.output = SampleOutput(result=f"processed: {event.input.value}")
            event.handled = True


class UnhandledEvent(Event[SampleInput, SampleOutput]):
    name = "test.unhandled"


# Tests


class TestEventCreation:
    """Tests for Event, EventInput, EventOutput creation."""

    def test_create_event_input(self):
        inp = SampleInput(value="hello")
        assert inp.value == "hello"

    def test_create_event_output(self):
        out = SampleOutput(result="world")
        assert out.result == "world"

    def test_create_event(self):
        event = SampleEvent(input=SampleInput(value="test"))
        assert event.name == "test.event"
        assert event.input.value == "test"
        assert event.output is None
        assert event.handled is False
        assert event.meta == {}

    def test_event_meta(self):
        event = SampleEvent(input=SampleInput(value="test"), meta={"key": "value"})
        assert event.meta["key"] == "value"


class TestModuleClass:
    """Tests for Module base class."""

    def test_module_metadata(self):
        mod = SampleModule()
        assert mod.metadata.name == "SampleModule"
        assert mod.metadata.description == "A test module"

    def test_module_host_initially_none(self):
        mod = SampleModule()
        assert mod.host is None

    def test_module_can_handle(self):
        mod = SampleModule()
        event = SampleEvent(input=SampleInput())
        assert mod.can_handle(event) is True

    def test_module_cannot_handle_different_event(self):
        mod = SampleModule()
        event = UnhandledEvent(input=SampleInput())
        assert mod.can_handle(event) is False


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

    def test_handle_event(self):
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        event = SampleEvent(input=SampleInput(value="hello"))
        result = host.handle(event)

        assert result is event
        assert event.handled is True
        assert event.output.result == "processed: hello"
        assert mod.handle_count == 1

    def test_unhandled_event(self):
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        event = UnhandledEvent(input=SampleInput(value="test"))
        result = host.handle(event)

        assert result is event
        assert event.handled is False
        assert event.output is None

    def test_can_handle(self):
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        assert host.can_handle(SampleEvent(input=SampleInput())) is True
        assert host.can_handle(UnhandledEvent(input=SampleInput())) is False

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

        result = host.register(mod1).register(mod2)

        assert result is host
        assert len(host.modules) == 2


class TestMultipleModules:
    """Tests for multiple module handling."""

    def test_first_handler_wins(self):
        """When multiple modules can handle, only first gets the event."""
        host = ModuleHost()
        mod1 = SampleModule()
        mod2 = SampleModule()
        host.register(mod1)
        host.register(mod2)

        event = SampleEvent(input=SampleInput(value="test"))
        host.handle(event)

        # First module handles, second should not
        assert mod1.handle_count == 1
        assert mod2.handle_count == 0

    def test_module_can_dispatch_to_host(self):
        """Modules can dispatch events through their host."""
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        # Simulate a module dispatching an event
        event = SampleEvent(input=SampleInput(value="from-module"))
        mod.host.handle(event)

        assert event.handled is True
        assert event.output.result == "processed: from-module"


@pytest.mark.asyncio
class TestAsyncHandling:
    """Tests for async event handling."""

    async def test_handle_async(self):
        host = ModuleHost()
        mod = SampleModule()
        host.register(mod)

        event = SampleEvent(input=SampleInput(value="async-test"))
        result = await host.handle_async(event)

        assert result is event
        assert event.handled is True
        assert event.output.result == "processed: async-test"
