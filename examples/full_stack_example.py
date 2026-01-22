"""Full stack example combining all PyModules layers.

This example demonstrates the complete flow:
    Events → Modules → Database → REST API

Requirements:
    pip install 'pymodules[web]'

Run:
    uvicorn examples.full_stack_example:app --reload
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from sqlalchemy import Column, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pymodules import Event, EventInput, EventOutput, Module, ModuleHost, module
from pymodules.api import ModuleRouter, api_endpoint, register_error_handlers
from pymodules.db import Base, BaseRepository, SoftDeleteMixin, TimestampMixin, UUIDMixin


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
# Event Definitions
# =============================================================================


@dataclass
class CreateTaskInput(EventInput):
    """Input for creating a task."""

    title: str
    description: str = ""


@dataclass
class CreateTaskOutput(EventOutput):
    """Output after creating a task."""

    id: str
    title: str
    description: str
    status: str


@dataclass
class GetTaskInput(EventInput):
    """Input for getting a task by ID."""

    task_id: str


@dataclass
class GetTaskOutput(EventOutput):
    """Output for a single task."""

    id: str
    title: str
    description: str
    status: str
    created_at: str
    updated_at: str


@dataclass
class ListTasksInput(EventInput):
    """Input for listing tasks."""

    limit: int = 10
    offset: int = 0
    include_deleted: bool = False


@dataclass
class ListTasksOutput(EventOutput):
    """Output for task listing."""

    tasks: list[dict[str, Any]]
    total: int


@dataclass
class UpdateTaskInput(EventInput):
    """Input for updating a task."""

    task_id: str
    title: str | None = None
    description: str | None = None
    status: str | None = None


@dataclass
class UpdateTaskOutput(EventOutput):
    """Output after updating a task."""

    id: str
    title: str
    description: str
    status: str


@dataclass
class DeleteTaskInput(EventInput):
    """Input for deleting a task."""

    task_id: str
    hard_delete: bool = False


@dataclass
class DeleteTaskOutput(EventOutput):
    """Output after deleting a task."""

    success: bool
    message: str


@dataclass
class CompleteTaskInput(EventInput):
    """Input for completing a task."""

    task_id: str


@dataclass
class CompleteTaskOutput(EventOutput):
    """Output after completing a task."""

    id: str
    title: str
    status: str


# =============================================================================
# Event Classes
# =============================================================================


class CreateTask(Event[CreateTaskInput, CreateTaskOutput]):
    """Event to create a new task.

    Convention: 'create' + 'Task' -> POST /tasks
    """

    pass


class GetTask(Event[GetTaskInput, GetTaskOutput]):
    """Event to get a task by ID.

    Convention: 'get' + 'Task' -> GET /tasks/{id}
    """

    pass


class ListTasks(Event[ListTasksInput, ListTasksOutput]):
    """Event to list all tasks.

    Convention: 'list' + 'Tasks' -> GET /tasks
    """

    pass


class UpdateTask(Event[UpdateTaskInput, UpdateTaskOutput]):
    """Event to update a task.

    Convention: 'update' + 'Task' -> PUT /tasks/{id}
    """

    pass


class DeleteTask(Event[DeleteTaskInput, DeleteTaskOutput]):
    """Event to delete a task.

    Convention: 'delete' + 'Task' -> DELETE /tasks/{id}
    """

    pass


@api_endpoint(
    path="/tasks/{task_id}/complete",
    method="POST",
    tags=["Tasks"],
    summary="Mark a task as completed",
)
class CompleteTask(Event[CompleteTaskInput, CompleteTaskOutput]):
    """Event to mark a task as completed.

    Uses @api_endpoint for custom route.
    """

    pass


# =============================================================================
# Module Implementation
# =============================================================================


@module(name="tasks", description="Task management module with database persistence")
class TaskModule(Module):
    """Module that handles all task-related events with database persistence."""

    def __init__(self, repository: BaseRepository):
        """Initialize with a task repository."""
        self.repo = repository

    def can_handle(self, event: Event) -> bool:
        """Check if this module handles the event."""
        return isinstance(
            event,
            (CreateTask, GetTask, ListTasks, UpdateTask, DeleteTask, CompleteTask),
        )

    async def handle(self, event: Event) -> None:
        """Handle task events with database operations."""
        if isinstance(event, CreateTask):
            await self._handle_create(event)
        elif isinstance(event, GetTask):
            await self._handle_get(event)
        elif isinstance(event, ListTasks):
            await self._handle_list(event)
        elif isinstance(event, UpdateTask):
            await self._handle_update(event)
        elif isinstance(event, DeleteTask):
            await self._handle_delete(event)
        elif isinstance(event, CompleteTask):
            await self._handle_complete(event)

    async def _handle_create(self, event: CreateTask) -> None:
        """Handle task creation."""
        task = await self.repo.create(
            title=event.input.title,
            description=event.input.description,
        )
        event.output = CreateTaskOutput(
            id=str(task.id),
            title=task.title,
            description=task.description,
            status=task.status,
        )
        event.handled = True

    async def _handle_get(self, event: GetTask) -> None:
        """Handle getting a single task."""
        task = await self.repo.get_by_id(UUID(event.input.task_id))
        if not task:
            raise ValueError(f"Task {event.input.task_id} not found")

        event.output = GetTaskOutput(
            id=str(task.id),
            title=task.title,
            description=task.description,
            status=task.status,
            created_at=task.created_at.isoformat(),
            updated_at=task.updated_at.isoformat(),
        )
        event.handled = True

    async def _handle_list(self, event: ListTasks) -> None:
        """Handle listing tasks."""
        tasks = await self.repo.get_all(
            limit=event.input.limit,
            offset=event.input.offset,
            include_deleted=event.input.include_deleted,
        )
        total = await self.repo.count()

        event.output = ListTasksOutput(
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
        event.handled = True

    async def _handle_update(self, event: UpdateTask) -> None:
        """Handle task update."""
        updates = {}
        if event.input.title is not None:
            updates["title"] = event.input.title
        if event.input.description is not None:
            updates["description"] = event.input.description
        if event.input.status is not None:
            updates["status"] = event.input.status

        task = await self.repo.update(UUID(event.input.task_id), **updates)
        if not task:
            raise ValueError(f"Task {event.input.task_id} not found")

        event.output = UpdateTaskOutput(
            id=str(task.id),
            title=task.title,
            description=task.description,
            status=task.status,
        )
        event.handled = True

    async def _handle_delete(self, event: DeleteTask) -> None:
        """Handle task deletion."""
        task_id = UUID(event.input.task_id)

        if event.input.hard_delete:
            success = await self.repo.delete(task_id)
            message = "Task permanently deleted" if success else "Task not found"
        else:
            await self.repo.soft_delete(task_id)
            success = True
            message = "Task soft deleted"

        event.output = DeleteTaskOutput(success=success, message=message)
        event.handled = True

    async def _handle_complete(self, event: CompleteTask) -> None:
        """Handle marking a task as completed."""
        task = await self.repo.update(
            UUID(event.input.task_id),
            status="completed",
        )
        if not task:
            raise ValueError(f"Task {event.input.task_id} not found")

        event.output = CompleteTaskOutput(
            id=str(task.id),
            title=task.title,
            status=task.status,
        )
        event.handled = True


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
    - Task module for event handling
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
        description="Full-stack example using PyModules with Events, Database, and REST API",
        version="1.0.0",
    )

    # Register error handlers
    register_error_handlers(app)

    # Create ModuleRouter and register events
    router = ModuleRouter(host)
    router.register_event(CreateTask)
    router.register_event(GetTask)
    router.register_event(ListTasks)
    router.register_event(UpdateTask)
    router.register_event(DeleteTask)
    router.register_event(CompleteTask)

    # Mount router on app
    router.mount(app, prefix="/api/v1")

    # Startup event to initialize database
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
  - Event-driven architecture with typed Events
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
