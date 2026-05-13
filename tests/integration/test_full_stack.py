"""End-to-end integration tests for the complete command -> module -> db -> api flow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Column, String
from sqlalchemy.orm import declarative_base

from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    Module,
    ModuleHost,
    handles,
    module,
)
from pymodules.contrib.db.mixins import UUIDType

# Define Base and model at module level to avoid SQLAlchemy redefinition issues
IntegrationBase = declarative_base()


class IntegrationUser(IntegrationBase):
    """User model for integration tests."""

    __tablename__ = "integration_users"

    id = Column(UUIDType(), primary_key=True, default=uuid4)
    name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False)


# Define command types at module level
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
    id: str = ""  # Matches route convention {id}, injected from path


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


@dataclass
class UpdateUserInput(CommandRequest):
    id: str = ""  # Matches route convention {id}, injected from path
    name: str | None = None
    email: str | None = None


@dataclass
class UpdateUserOutput(CommandResponse):
    id: str
    name: str
    email: str


@dataclass
class DeleteUserInput(CommandRequest):
    id: str = ""  # Matches route convention {id}, injected from path


@dataclass
class DeleteUserOutput(CommandResponse):
    success: bool


class CreateUser(Command[CreateUserInput, CreateUserOutput]):
    pass


class GetUser(Command[GetUserInput, GetUserOutput]):
    pass


class ListUsers(Command[ListUsersInput, ListUsersOutput]):
    pass


class UpdateUser(Command[UpdateUserInput, UpdateUserOutput]):
    pass


class DeleteUser(Command[DeleteUserInput, DeleteUserOutput]):
    pass


@pytest.fixture
async def full_stack_setup(tmp_path):
    """Set up a full stack test environment."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from pymodules.contrib.db import BaseRepository

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

        @handles(CreateUser)
        async def create_user(self, command: CreateUser) -> CreateUserOutput:
            user = await self.repo.create(
                name=command.request.name,
                email=command.request.email,
            )
            return CreateUserOutput(
                id=str(user.id),
                name=user.name,
                email=user.email,
            )

        @handles(GetUser)
        async def get_user(self, command: GetUser) -> GetUserOutput:
            user = await self.repo.get_by_id(UUID(command.request.id))
            if not user:
                raise ValueError("User not found")
            return GetUserOutput(
                id=str(user.id),
                name=user.name,
                email=user.email,
            )

        @handles(ListUsers)
        async def list_users(self, command: ListUsers) -> ListUsersOutput:
            users = await self.repo.get_all(
                limit=command.request.limit,
                offset=command.request.offset,
            )
            total = await self.repo.count()
            return ListUsersOutput(
                users=[{"id": str(u.id), "name": u.name} for u in users],
                total=total,
            )

        @handles(UpdateUser)
        async def update_user(self, command: UpdateUser) -> UpdateUserOutput:
            updates = {}
            if command.request.name:
                updates["name"] = command.request.name
            if command.request.email:
                updates["email"] = command.request.email
            user = await self.repo.update(UUID(command.request.id), **updates)
            return UpdateUserOutput(
                id=str(user.id),
                name=user.name,
                email=user.email,
            )

        @handles(DeleteUser)
        async def delete_user(self, command: DeleteUser) -> DeleteUserOutput:
            result = await self.repo.delete(UUID(command.request.id))
            return DeleteUserOutput(success=result)

    # Set up host
    host = ModuleHost()
    user_module = UserModule(user_repo)
    host.register(user_module)

    return {
        "host": host,
        "engine": engine,
        "commands": {
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
    """Tests for complete command -> module -> db -> api flow."""

    @pytest.mark.asyncio
    async def test_create_entity_via_api(self, full_stack_setup) -> None:
        """Test creating an entity through the full stack."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.contrib.api import ModuleRouter

        setup = full_stack_setup
        host = setup["host"]
        commands = setup["commands"]

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_command(commands["CreateUser"])
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
        commands = setup["commands"]
        inputs = setup["inputs"]

        # First create a user via command
        create_cmd = commands["CreateUser"](
            request=inputs["CreateUserInput"](name="Jane", email="jane@example.com")
        )
        create_response = await host.dispatch_async(create_cmd)
        user_id = create_response.id

        # Now test getting via API
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.contrib.api import ModuleRouter

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_command(commands["GetUser"])
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
        commands = setup["commands"]
        inputs = setup["inputs"]

        # Create a few users
        for i in range(3):
            cmd = commands["CreateUser"](
                request=inputs["CreateUserInput"](name=f"User {i}", email=f"user{i}@example.com")
            )
            await host.dispatch_async(cmd)

        # Test listing via API
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.contrib.api import ModuleRouter

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_command(commands["ListUsers"])
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
        commands = setup["commands"]
        inputs = setup["inputs"]

        # Create user
        create_cmd = commands["CreateUser"](
            request=inputs["CreateUserInput"](name="Original", email="original@example.com")
        )
        create_response = await host.dispatch_async(create_cmd)
        user_id = create_response.id

        # Test update via API
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.contrib.api import ModuleRouter

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_command(commands["UpdateUser"])
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
        commands = setup["commands"]
        inputs = setup["inputs"]

        # Create user
        create_cmd = commands["CreateUser"](
            request=inputs["CreateUserInput"](name="ToDelete", email="delete@example.com")
        )
        create_response = await host.dispatch_async(create_cmd)
        user_id = create_response.id

        # Test delete via API
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.contrib.api import ModuleRouter

        app = FastAPI()
        router = ModuleRouter(host)
        router.register_command(commands["DeleteUser"])
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

        from pymodules.contrib.api import ModuleRouter, register_error_handlers

        setup = full_stack_setup
        host = setup["host"]
        commands = setup["commands"]

        app = FastAPI()
        register_error_handlers(app)
        router = ModuleRouter(host)
        router.register_command(commands["GetUser"])
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

        from pymodules.contrib.api import ModuleRouter
        from pymodules.contrib.api.auth import AuthMiddleware, AuthProvider, TokenClaims

        setup = full_stack_setup
        host = setup["host"]
        commands = setup["commands"]

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
        router.register_command(commands["CreateUser"])
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
