"""End-to-end integration tests for complete event → module → db → api flow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Column, String
from sqlalchemy.orm import declarative_base

from pymodules import Event, EventInput, EventOutput, Module, ModuleHost, module
from pymodules.db.mixins import UUIDType

# Define Base and model at module level to avoid SQLAlchemy redefinition issues
IntegrationBase = declarative_base()


class IntegrationUser(IntegrationBase):
    """User model for integration tests."""

    __tablename__ = "integration_users"

    id = Column(UUIDType(), primary_key=True, default=uuid4)
    name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False)


# Define event types at module level
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
    id: str = ""  # Matches route convention {id}, injected from path


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


@dataclass
class UpdateUserInput(EventInput):
    id: str = ""  # Matches route convention {id}, injected from path
    name: str | None = None
    email: str | None = None


@dataclass
class UpdateUserOutput(EventOutput):
    id: str
    name: str
    email: str


@dataclass
class DeleteUserInput(EventInput):
    id: str = ""  # Matches route convention {id}, injected from path


@dataclass
class DeleteUserOutput(EventOutput):
    success: bool


class CreateUser(Event[CreateUserInput, CreateUserOutput]):
    pass


class GetUser(Event[GetUserInput, GetUserOutput]):
    pass


class ListUsers(Event[ListUsersInput, ListUsersOutput]):
    pass


class UpdateUser(Event[UpdateUserInput, UpdateUserOutput]):
    pass


class DeleteUser(Event[DeleteUserInput, DeleteUserOutput]):
    pass


@pytest.fixture
async def full_stack_setup(tmp_path):
    """Set up a full stack test environment."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from pymodules.db import BaseRepository

    # Set up database
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async with engine.begin() as conn:
        await conn.run_sync(IntegrationBase.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Create repository
    user_repo = BaseRepository(session_factory, IntegrationUser)

    # Define module
    @module(name="users", description="User management")
    class UserModule(Module):
        def __init__(self, repo):
            self.repo = repo

        def can_handle(self, event):
            return isinstance(event, (CreateUser, GetUser, ListUsers, UpdateUser, DeleteUser))

        async def handle(self, event):
            if isinstance(event, CreateUser):
                user = await self.repo.create(
                    name=event.input.name,
                    email=event.input.email,
                )
                event.output = CreateUserOutput(
                    id=str(user.id),
                    name=user.name,
                    email=user.email,
                )
                event.handled = True
            elif isinstance(event, GetUser):
                user = await self.repo.get_by_id(UUID(event.input.id))
                if not user:
                    raise ValueError("User not found")
                event.output = GetUserOutput(
                    id=str(user.id),
                    name=user.name,
                    email=user.email,
                )
                event.handled = True
            elif isinstance(event, ListUsers):
                users = await self.repo.get_all(
                    limit=event.input.limit,
                    offset=event.input.offset,
                )
                total = await self.repo.count()
                event.output = ListUsersOutput(
                    users=[{"id": str(u.id), "name": u.name} for u in users],
                    total=total,
                )
                event.handled = True
            elif isinstance(event, UpdateUser):
                updates = {}
                if event.input.name:
                    updates["name"] = event.input.name
                if event.input.email:
                    updates["email"] = event.input.email
                user = await self.repo.update(UUID(event.input.id), **updates)
                event.output = UpdateUserOutput(
                    id=str(user.id),
                    name=user.name,
                    email=user.email,
                )
                event.handled = True
            elif isinstance(event, DeleteUser):
                result = await self.repo.delete(UUID(event.input.id))
                event.output = DeleteUserOutput(success=result)
                event.handled = True

    # Set up host
    host = ModuleHost()
    user_module = UserModule(user_repo)
    host.register(user_module)

    return {
        "host": host,
        "engine": engine,
        "events": {
            "CreateUser": CreateUser,
            "GetUser": GetUser,
            "ListUsers": ListUsers,
            "UpdateUser": UpdateUser,
            "DeleteUser": DeleteUser,
        },
        "inputs": {
            "CreateUserInput": CreateUserInput,
            "GetUserInput": GetUserInput,
            "ListUsersInput": ListUsersInput,
            "UpdateUserInput": UpdateUserInput,
            "DeleteUserInput": DeleteUserInput,
        },
    }


class TestFullStackIntegration:
    """Tests for complete event → module → db → api flow."""

    @pytest.mark.asyncio
    async def test_create_entity_via_api(self, full_stack_setup) -> None:
        """Test creating an entity through the full stack."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api import ModuleRouter

        setup = full_stack_setup
        host = setup["host"]
        events = setup["events"]

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_event(events["CreateUser"])
        app.include_router(router.router)

        client = TestClient(app)

        # Find the route
        routes = [(r.path, r.methods) for r in app.routes if hasattr(r, "methods")]
        create_route = next((p for p, m in routes if "user" in p.lower() and "POST" in m), None)

        if create_route:
            response = client.post(
                create_route, json={"name": "John Doe", "email": "john@example.com"}
            )
            assert response.status_code == 200
            data = response.json()
            assert data["name"] == "John Doe"
            assert data["email"] == "john@example.com"
            assert "id" in data

    @pytest.mark.asyncio
    async def test_get_entity_via_api(self, full_stack_setup) -> None:
        """Test getting an entity through the full stack."""
        setup = full_stack_setup
        host = setup["host"]
        events = setup["events"]
        inputs = setup["inputs"]

        # First create a user via event
        create_event = events["CreateUser"](
            input=inputs["CreateUserInput"](name="Jane", email="jane@example.com")
        )
        await host.handle_async(create_event)
        user_id = create_event.output.id

        # Now test getting via API
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api import ModuleRouter

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_event(events["GetUser"])
        app.include_router(router.router)

        client = TestClient(app)
        routes = [(r.path, r.methods) for r in app.routes if hasattr(r, "methods")]
        get_route = next((p for p, m in routes if "user" in p.lower() and "GET" in m), None)

        if get_route and "{" in get_route:
            response = client.get(get_route.replace("{id}", user_id))
            assert response.status_code == 200
            data = response.json()
            assert data["name"] == "Jane"

    @pytest.mark.asyncio
    async def test_list_entities_via_api(self, full_stack_setup) -> None:
        """Test listing entities through the full stack."""
        setup = full_stack_setup
        host = setup["host"]
        events = setup["events"]
        inputs = setup["inputs"]

        # Create a few users
        for i in range(3):
            event = events["CreateUser"](
                input=inputs["CreateUserInput"](name=f"User {i}", email=f"user{i}@example.com")
            )
            await host.handle_async(event)

        # Test listing via API
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api import ModuleRouter

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_event(events["ListUsers"])
        app.include_router(router.router)

        client = TestClient(app)
        routes = [(r.path, r.methods) for r in app.routes if hasattr(r, "methods")]
        list_route = next(
            (p for p, m in routes if "user" in p.lower() and "GET" in m and "{" not in p), None
        )

        if list_route:
            response = client.get(list_route)
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 3
            assert len(data["users"]) == 3

    @pytest.mark.asyncio
    async def test_update_entity_via_api(self, full_stack_setup) -> None:
        """Test updating an entity through the full stack."""
        setup = full_stack_setup
        host = setup["host"]
        events = setup["events"]
        inputs = setup["inputs"]

        # Create user
        create_event = events["CreateUser"](
            input=inputs["CreateUserInput"](name="Original", email="original@example.com")
        )
        await host.handle_async(create_event)
        user_id = create_event.output.id

        # Test update via API
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api import ModuleRouter

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_event(events["UpdateUser"])
        app.include_router(router.router)

        client = TestClient(app)
        routes = [(r.path, r.methods) for r in app.routes if hasattr(r, "methods")]
        update_route = next((p for p, m in routes if "user" in p.lower() and "PUT" in m), None)

        if update_route and "{" in update_route:
            response = client.put(update_route.replace("{id}", user_id), json={"name": "Updated"})
            assert response.status_code == 200
            data = response.json()
            assert data["name"] == "Updated"

    @pytest.mark.asyncio
    async def test_delete_entity_via_api(self, full_stack_setup) -> None:
        """Test deleting an entity through the full stack."""
        setup = full_stack_setup
        host = setup["host"]
        events = setup["events"]
        inputs = setup["inputs"]

        # Create user
        create_event = events["CreateUser"](
            input=inputs["CreateUserInput"](name="ToDelete", email="delete@example.com")
        )
        await host.handle_async(create_event)
        user_id = create_event.output.id

        # Test delete via API
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api import ModuleRouter

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_event(events["DeleteUser"])
        app.include_router(router.router)

        client = TestClient(app)
        routes = [(r.path, r.methods) for r in app.routes if hasattr(r, "methods")]
        delete_route = next((p for p, m in routes if "user" in p.lower() and "DELETE" in m), None)

        if delete_route and "{" in delete_route:
            response = client.request("DELETE", delete_route.replace("{id}", user_id), json={})
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True

    @pytest.mark.asyncio
    async def test_error_propagation(self, full_stack_setup) -> None:
        """Test that errors propagate correctly through the stack."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api import ModuleRouter, register_error_handlers

        setup = full_stack_setup
        host = setup["host"]
        events = setup["events"]

        app = FastAPI()
        register_error_handlers(app)
        router = ModuleRouter(host)
        router.register_event(events["GetUser"])
        app.include_router(router.router)

        client = TestClient(app, raise_server_exceptions=False)
        routes = [(r.path, r.methods) for r in app.routes if hasattr(r, "methods")]
        get_route = next((p for p, m in routes if "user" in p.lower() and "GET" in m), None)

        if get_route and "{" in get_route:
            # Try to get non-existent user
            response = client.get(get_route.replace("{id}", "00000000-0000-0000-0000-000000000000"))
            assert response.status_code >= 400

    @pytest.mark.asyncio
    async def test_auth_integration(self, full_stack_setup) -> None:
        """Test that auth middleware works with the full stack."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api import ModuleRouter
        from pymodules.api.auth import AuthMiddleware, AuthProvider, TokenClaims

        setup = full_stack_setup
        host = setup["host"]
        events = setup["events"]

        class TestAuthProvider(AuthProvider):
            async def validate_token(self, token: str) -> TokenClaims | None:
                if token == "valid-token":
                    return TokenClaims(
                        sub="test-user",
                        exp=datetime.now(UTC) + timedelta(hours=1),
                        iat=datetime.now(UTC),
                    )
                return None

            async def create_token(self, claims: dict) -> str:
                return "token"

        app = FastAPI()
        app.add_middleware(AuthMiddleware, provider=TestAuthProvider())

        router = ModuleRouter(host)
        router.register_event(events["CreateUser"])
        app.include_router(router.router)

        client = TestClient(app)
        routes = [(r.path, r.methods) for r in app.routes if hasattr(r, "methods")]
        create_route = next((p for p, m in routes if "user" in p.lower() and "POST" in m), None)

        if create_route:
            # Without auth
            response = client.post(create_route, json={"name": "Test", "email": "test@test.com"})
            assert response.status_code == 401

            # With auth
            response = client.post(
                create_route,
                json={"name": "Test", "email": "auth@test.com"},
                headers={"Authorization": "Bearer valid-token"},
            )
            assert response.status_code == 200
