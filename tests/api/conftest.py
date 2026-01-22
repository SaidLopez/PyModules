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
def sample_events():
    """Create sample event classes for testing."""
    from pymodules import Event, EventInput, EventOutput

    @dataclass
    class CreateUserInput(EventInput):
        name: str
        email: str

    @dataclass
    class CreateUserOutput(EventOutput):
        id: str
        name: str
        email: str

    @dataclass
    class GetUserInput(EventInput):
        user_id: str

    @dataclass
    class GetUserOutput(EventOutput):
        id: str
        name: str
        email: str

    @dataclass
    class ListUsersInput(EventInput):
        limit: int = 10
        offset: int = 0

    @dataclass
    class ListUsersOutput(EventOutput):
        users: list
        total: int

    class CreateUser(Event[CreateUserInput, CreateUserOutput]):
        pass

    class GetUser(Event[GetUserInput, GetUserOutput]):
        pass

    class ListUsers(Event[ListUsersInput, ListUsersOutput]):
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
def sample_module(sample_events):
    """Create a sample module that handles the events."""
    from pymodules import Module, module

    @module(name="user", description="User management")
    class UserModule(Module):
        def can_handle(self, event):
            return isinstance(
                event,
                (
                    sample_events["CreateUser"],
                    sample_events["GetUser"],
                    sample_events["ListUsers"],
                ),
            )

        async def handle(self, event):
            if isinstance(event, sample_events["CreateUser"]):
                event.output = sample_events["CreateUserOutput"](
                    id="123",
                    name=event.input.name,
                    email=event.input.email,
                )
                event.handled = True
            elif isinstance(event, sample_events["GetUser"]):
                event.output = sample_events["GetUserOutput"](
                    id=event.input.user_id,
                    name="Test User",
                    email="test@example.com",
                )
                event.handled = True
            elif isinstance(event, sample_events["ListUsers"]):
                event.output = sample_events["ListUsersOutput"](
                    users=[{"id": "1", "name": "User 1"}],
                    total=1,
                )
                event.handled = True

    return UserModule()
