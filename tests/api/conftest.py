"""Shared fixtures for API layer tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pymodules import ModuleHost


@pytest.fixture
def app() -> FastAPI:
    """Create a fresh FastAPI app for testing."""
    from fastapi import FastAPI

    return FastAPI()


@pytest.fixture
def host() -> ModuleHost:
    """Create a fresh ModuleHost for testing."""
    from pymodules import ModuleHost

    return ModuleHost()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Create a TestClient for HTTP testing."""
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture
def sample_commands():
    """Create sample command classes for testing."""
    from pymodules import Command, CommandRequest, CommandResponse

    @dataclass
    class CreateUserInput(CommandRequest):
        name: str
        email: str

    @dataclass
    class CreateUserOutput(CommandResponse):
        id: str
        name: str
        email: str

    @dataclass
    class GetUserInput(CommandRequest):
        user_id: str

    @dataclass
    class GetUserOutput(CommandResponse):
        id: str
        name: str
        email: str

    @dataclass
    class ListUsersInput(CommandRequest):
        limit: int = 10
        offset: int = 0

    @dataclass
    class ListUsersOutput(CommandResponse):
        users: list
        total: int

    class CreateUser(Command[CreateUserInput, CreateUserOutput]):
        pass

    class GetUser(Command[GetUserInput, GetUserOutput]):
        pass

    class ListUsers(Command[ListUsersInput, ListUsersOutput]):
        pass

    return {
        "CreateUser": CreateUser,
        "GetUser": GetUser,
        "ListUsers": ListUsers,
        "CreateUserInput": CreateUserInput,
        "CreateUserOutput": CreateUserOutput,
        "GetUserInput": GetUserInput,
        "GetUserOutput": GetUserOutput,
        "ListUsersInput": ListUsersInput,
        "ListUsersOutput": ListUsersOutput,
    }


@pytest.fixture
def sample_module(sample_commands):
    """Create a sample module that handles the commands."""
    from pymodules import Module, module

    @module(name="user", description="User management")
    class UserModule(Module):
        def can_handle(self, command):
            return isinstance(
                command,
                (
                    sample_commands["CreateUser"],
                    sample_commands["GetUser"],
                    sample_commands["ListUsers"],
                ),
            )

        async def handle(self, command):
            if isinstance(command, sample_commands["CreateUser"]):
                command.output = sample_commands["CreateUserOutput"](
                    id="123",
                    name=command.input.name,
                    email=command.input.email,
                )
                command.handled = True
            elif isinstance(command, sample_commands["GetUser"]):
                command.output = sample_commands["GetUserOutput"](
                    id=command.input.user_id,
                    name="Test User",
                    email="test@example.com",
                )
                command.handled = True
            elif isinstance(command, sample_commands["ListUsers"]):
                command.output = sample_commands["ListUsersOutput"](
                    users=[{"id": "1", "name": "User 1"}],
                    total=1,
                )
                command.handled = True

    return UserModule()
