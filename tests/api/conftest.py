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
    """Create sample command classes for testing.

    Each Command carries an explicit ``@api_endpoint`` decorator — the
    contrib.api layer does no class-name-to-URL convention magic.
    """
    from pymodules import Command, CommandRequest, CommandResponse
    from pymodules.contrib.api import api_endpoint

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

    @api_endpoint(method="POST", path="/users")
    class CreateUser(Command[CreateUserInput, CreateUserOutput]):
        pass

    @api_endpoint(method="GET", path="/users/{user_id}")
    class GetUser(Command[GetUserInput, GetUserOutput]):
        pass

    @api_endpoint(method="GET", path="/users")
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
    from pymodules import Module, handles, module

    CreateUser = sample_commands["CreateUser"]
    GetUser = sample_commands["GetUser"]
    ListUsers = sample_commands["ListUsers"]

    @module(name="user", description="User management")
    class UserModule(Module):
        @handles(CreateUser)
        async def create_user(self, command):
            return sample_commands["CreateUserOutput"](
                id="123",
                name=command.request.name,
                email=command.request.email,
            )

        @handles(GetUser)
        async def get_user(self, command):
            return sample_commands["GetUserOutput"](
                id=command.request.user_id,
                name="Test User",
                email="test@example.com",
            )

        @handles(ListUsers)
        async def list_users(self, command):
            return sample_commands["ListUsersOutput"](
                users=[{"id": "1", "name": "User 1"}],
                total=1,
            )

    return UserModule()
