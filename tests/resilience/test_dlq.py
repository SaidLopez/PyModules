"""
Tests for ``DeadLetterQueue`` and ``DLQMiddleware``.
"""

from dataclasses import dataclass

import pytest

from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    DeadLetterQueue,
    DLQMiddleware,
    Module,
    ModuleHost,
    ModuleHostConfig,
    handles,
    module,
)


@dataclass
class DLQInput(CommandRequest):
    value: str = ""
    should_fail: bool = False


@dataclass
class DLQOutput(CommandResponse):
    result: str = ""


class DLQCommand(Command[DLQInput, DLQOutput]):
    name = "test.dlq"


@module(name="DLQFailingModule")
class DLQFailingModule(Module):
    @handles(DLQCommand)
    def handle(self, command: DLQCommand) -> DLQOutput:
        if command.request.should_fail:
            raise ValueError("Boom")
        return DLQOutput(result=f"ok: {command.request.value}")


class TestDeadLetterQueue:
    def test_add_entry(self):
        dlq = DeadLetterQueue(max_size=100)
        command = DLQCommand(request=DLQInput(value="test"))
        error = ValueError("Test error")

        entry = dlq.add(command, error, module_name="TestModule")

        assert len(dlq) == 1
        assert entry.command is command
        assert entry.error is error
        assert entry.module_name == "TestModule"

    def test_max_size(self):
        dlq = DeadLetterQueue(max_size=2)
        for i in range(5):
            command = DLQCommand(request=DLQInput(value=str(i)))
            dlq.add(command, ValueError(f"Error {i}"))

        assert len(dlq) == 2
        entries = dlq.entries
        assert entries[0].command.request.value == "3"
        assert entries[1].command.request.value == "4"

    def test_pop(self):
        dlq = DeadLetterQueue(max_size=10)
        dlq.add(DLQCommand(request=DLQInput(value="first")), ValueError("Error 1"))
        dlq.add(DLQCommand(request=DLQInput(value="second")), ValueError("Error 2"))

        entry = dlq.pop()
        assert entry.command.request.value == "first"
        assert len(dlq) == 1

    def test_clear(self):
        dlq = DeadLetterQueue(max_size=10)
        for i in range(5):
            dlq.add(DLQCommand(request=DLQInput(value=str(i))), ValueError(""))

        assert dlq.clear() == 5
        assert len(dlq) == 0

    def test_on_add_callback(self):
        added = []
        dlq = DeadLetterQueue(max_size=10, on_add=lambda e: added.append(e))
        command = DLQCommand(request=DLQInput(value="test"))
        dlq.add(command, ValueError("Error"))

        assert len(added) == 1
        assert added[0].command is command


class TestDLQMiddleware:
    def test_integration_records_failure(self):
        dlq = DeadLetterQueue(max_size=100)
        mw = DLQMiddleware(dlq, propagate_exceptions=False)
        config = ModuleHostConfig(
            middleware=[mw],
            propagate_exceptions=False,
        )
        host = ModuleHost(config=config)
        host.register(DLQFailingModule())

        host.dispatch(DLQCommand(request=DLQInput(should_fail=True)))

        assert len(dlq) == 1
        assert mw.dead_lettered_count == 1


class TestDLQIgnoresFrameworkSignals:
    """Framework signals must not be dead-lettered."""

    def test_unknown_command_does_not_enqueue(self):
        from pymodules import UnknownCommandError

        queue = DeadLetterQueue(max_size=10)
        mw = DLQMiddleware(queue)
        host = ModuleHost(config=ModuleHostConfig(middleware=[mw]))
        # No module registered — terminal raises UnknownCommandError.
        with pytest.raises(UnknownCommandError):
            host.dispatch(DLQCommand(request=DLQInput()))
        assert mw.dead_lettered_count == 0
        assert len(queue) == 0
