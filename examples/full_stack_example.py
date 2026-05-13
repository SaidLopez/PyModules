"""Full stack example combining all PyModules layers.

This example demonstrates the complete flow:
    Commands -> Modules -> Database -> REST API

Requirements:
    pip install 'pymodules[web]'

Run:
    uvicorn examples.full_stack_example:app --reload
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from sqlalchemy import Column, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    Module,
    ModuleHost,
    module,
)
from pymodules.contrib.api import ModuleRouter, api_endpoint, register_error_handlers
from pymodules.contrib.db import Base, BaseRepository, SoftDeleteMixin, TimestampMixin, UUIDMixin

# =============================================================================
# Database Model
# =============================================================================


class Task(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Task model with UUID, timestamps, and soft delete support."""

    __tablename__ = "tasks"

    title = Column(String(200), nullable=False)
    description = Column(String(1000), default="")
    status = Column(String(50), default="pending")

    def __repr__(self) -> str:
        return f"<Task(id={self.id}, title={self.title}, status={self.status})>"


# =============================================================================
# Command Definitions
# =============================================================================


@dataclass
class CreateTaskInput(CommandRequest):
    """Request payload for creating a task."""

    title: str
    description: str = ""


@dataclass
class CreateTaskOutput(CommandResponse):
    """Response after creating a task."""

    id: str
    title: str
    description: str
    status: str


@dataclass
class GetTaskInput(CommandRequest):
    """Request payload for getting a task by ID."""

    task_id: str


@dataclass
class GetTaskOutput(CommandResponse):
    """Response for a single task."""

    id: str
    title: str
    description: str
    status: str
    created_at: str
    updated_at: str


@dataclass
class ListTasksInput(CommandRequest):
    """Request payload for listing tasks."""

    limit: int = 10
    offset: int = 0
    include_deleted: bool = False


@dataclass
class ListTasksOutput(CommandResponse):
    """Response for task listing."""

    tasks: list[dict[str, Any]]
    total: int


@dataclass
class UpdateTaskInput(CommandRequest):
    """Request payload for updating a task."""

    task_id: str
    title: str | None = None
    description: str | None = None
    status: str | None = None


@dataclass
class UpdateTaskOutput(CommandResponse):
    """Response after updating a task."""

    id: str
    title: str
    description: str
    status: str


@dataclass
class DeleteTaskInput(CommandRequest):
    """Request payload for deleting a task."""

    task_id: str
    hard_delete: bool = False


@dataclass
class DeleteTaskOutput(CommandResponse):
    """Response after deleting a task."""

    success: bool
    message: str


@dataclass
class CompleteTaskInput(CommandRequest):
    """Request payload for completing a task."""

    task_id: str


@dataclass
class CompleteTaskOutput(CommandResponse):
    """Response after completing a task."""

    id: str
    title: str
    status: str


# =============================================================================
# Command Classes
# =============================================================================


class CreateTask(Command[CreateTaskInput, CreateTaskOutput]):
    """Command to create a new task.

    Convention: 'create' + 'Task' -> POST /tasks
    """

    pass


class GetTask(Command[GetTaskInput, GetTaskOutput]):
    """Command to get a task by ID.

    Convention: 'get' + 'Task' -> GET /tasks/{id}
    """

    pass


class ListTasks(Command[ListTasksInput, ListTasksOutput]):
    """Command to list all tasks.

    Convention: 'list' + 'Tasks' -> GET /tasks
    """

    pass


class UpdateTask(Command[UpdateTaskInput, UpdateTaskOutput]):
    """Command to update a task.

    Convention: 'update' + 'Task' -> PUT /tasks/{id}
    """

    pass


class DeleteTask(Command[DeleteTaskInput, DeleteTaskOutput]):
    """Command to delete a task.

    Convention: 'delete' + 'Task' -> DELETE /tasks/{id}
    """

    pass


@api_endpoint(
    path="/tasks/{task_id}/complete",
    method="POST",
    tags=["Tasks"],
    summary="Mark a task as completed",
)
class CompleteTask(Command[CompleteTaskInput, CompleteTaskOutput]):
    """Command to mark a task as completed.

    Uses @api_endpoint for custom route.
    """

    pass


# =============================================================================
# Module Implementation
# =============================================================================


@module(name="tasks", description="Task management module with database persistence")
class TaskModule(Module):
    """Module that handles all task-related commands with database persistence."""

    def __init__(self, repository: BaseRepository):
        """Initialize with a task repository."""
        self.repo = repository

    def can_handle(self, command: Command) -> bool:
        """Check if this module handles the command."""
        return isinstance(
            command,
            (CreateTask, GetTask, ListTasks, UpdateTask, DeleteTask, CompleteTask),
        )

    async def handle(self, command: Command) -> None:
        """Handle task commands with database operations."""
        if isinstance(command, CreateTask):
            await self._handle_create(command)
        elif isinstance(command, GetTask):
            await self._handle_get(command)
        elif isinstance(command, ListTasks):
            await self._handle_list(command)
        elif isinstance(command, UpdateTask):
            await self._handle_update(command)
        elif isinstance(command, DeleteTask):
            await self._handle_delete(command)
        elif isinstance(command, CompleteTask):
            await self._handle_complete(command)

    async def _handle_create(self, command: CreateTask) -> None:
        """Handle task creation."""
        task = await self.repo.create(
            title=command.input.title,
            description=command.input.description,
        )
        command.output = CreateTaskOutput(
            id=str(task.id),
            title=task.title,
            description=task.description,
            status=task.status,
        )
        command.handled = True

    async def _handle_get(self, command: GetTask) -> None:
        """Handle getting a single task."""
        task = await self.repo.get_by_id(UUID(command.input.task_id))
        if not task:
            raise ValueError(f"Task {command.input.task_id} not found")

        command.output = GetTaskOutput(
            id=str(task.id),
            title=task.title,
            description=task.description,
            status=task.status,
            created_at=task.created_at.isoformat(),
            updated_at=task.updated_at.isoformat(),
        )
        command.handled = True

    async def _handle_list(self, command: ListTasks) -> None:
        """Handle listing tasks."""
        tasks = await self.repo.get_all(
            limit=command.input.limit,
            offset=command.input.offset,
            include_deleted=command.input.include_deleted,
        )
        total = await self.repo.count()

        command.output = ListTasksOutput(
            tasks=[
                {
                    "id": str(t.id),
                    "title": t.title,
                    "status": t.status,
                    "created_at": t.created_at.isoformat(),
                }
                for t in tasks
            ],
            total=total,
        )
        command.handled = True

    async def _handle_update(self, command: UpdateTask) -> None:
        """Handle task update."""
        updates = {}
        if command.input.title is not None:
            updates["title"] = command.input.title
        if command.input.description is not None:
            updates["description"] = command.input.description
        if command.input.status is not None:
            updates["status"] = command.input.status

        task = await self.repo.update(UUID(command.input.task_id), **updates)
        if not task:
            raise ValueError(f"Task {command.input.task_id} not found")

        command.output = UpdateTaskOutput(
            id=str(task.id),
            title=task.title,
            description=task.description,
            status=task.status,
        )
        command.handled = True

    async def _handle_delete(self, command: DeleteTask) -> None:
        """Handle task deletion."""
        task_id = UUID(command.input.task_id)

        if command.input.hard_delete:
            success = await self.repo.delete(task_id)
            message = "Task permanently deleted" if success else "Task not found"
        else:
            await self.repo.soft_delete(task_id)
            success = True
            message = "Task soft deleted"

        command.output = DeleteTaskOutput(success=success, message=message)
        command.handled = True

    async def _handle_complete(self, command: CompleteTask) -> None:
        """Handle marking a task as completed."""
        task = await self.repo.update(
            UUID(command.input.task_id),
            status="completed",
        )
        if not task:
            raise ValueError(f"Task {command.input.task_id} not found")

        command.output = CompleteTaskOutput(
            id=str(task.id),
            title=task.title,
            status=task.status,
        )
        command.handled = True


# =============================================================================
# Application Factory
# =============================================================================


async def init_database(engine):
    """Initialize database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    This factory creates an app with:
    - In-memory SQLite database
    - Task repository for data access
    - Task module for command handling
    - REST API endpoints via ModuleRouter
    """
    # Create async engine (SQLite for demo, use PostgreSQL in production)
    engine = create_async_engine(
        "sqlite+aiosqlite:///tasks.db",
        echo=False,
    )

    # Create session factory
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Create repository
    task_repo = BaseRepository(session_factory, Task)

    # Create ModuleHost and register module
    host = ModuleHost()
    host.register(TaskModule(task_repo))

    # Create FastAPI app
    app = FastAPI(
        title="Task Manager API",
        description="Full-stack example using PyModules with Commands, Database, and REST API",
        version="1.0.0",
    )

    # Register error handlers
    register_error_handlers(app)

    # Create ModuleRouter and register commands
    router = ModuleRouter(host)
    router.register_command(CreateTask)
    router.register_command(GetTask)
    router.register_command(ListTasks)
    router.register_command(UpdateTask)
    router.register_command(DeleteTask)
    router.register_command(CompleteTask)

    # Mount router on app
    router.mount(app, prefix="/api/v1")

    # Startup hook to initialize database
    @app.on_event("startup")
    async def on_startup():
        await init_database(engine)

    # Add health endpoint
    @app.get("/health")
    async def health():
        return {"status": "healthy", "database": "connected"}

    return app


# Create app instance for uvicorn
app = create_app()


# =============================================================================
# CLI Usage Example
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    print("""
=============================================================================
PyModules Full Stack Example
=============================================================================

This example demonstrates:
  - Database models with mixins (UUID, Timestamps, SoftDelete)
  - Command-driven architecture with typed Commands
  - Module handling with database persistence
  - REST API generation via ModuleRouter

Starting server at http://localhost:8000

Available endpoints:
  GET    /health                     - Health check
  POST   /api/v1/tasks               - Create a task
  GET    /api/v1/tasks               - List tasks
  GET    /api/v1/tasks/{id}          - Get a task
  PUT    /api/v1/tasks/{id}          - Update a task
  DELETE /api/v1/tasks/{id}          - Delete a task (soft delete)
  POST   /api/v1/tasks/{id}/complete - Mark task as completed

Example API calls:

  # Create a task
  curl -X POST http://localhost:8000/api/v1/tasks \\
       -H "Content-Type: application/json" \\
       -d '{"title": "Learn PyModules", "description": "Study the full stack example"}'

  # List all tasks
  curl http://localhost:8000/api/v1/tasks

  # Get a specific task
  curl http://localhost:8000/api/v1/tasks/{task_id}

  # Complete a task
  curl -X POST http://localhost:8000/api/v1/tasks/{task_id}/complete

  # Soft delete a task
  curl -X DELETE http://localhost:8000/api/v1/tasks/{task_id}

OpenAPI docs: http://localhost:8000/docs
=============================================================================
""")

    uvicorn.run(app, host="0.0.0.0", port=8000)
