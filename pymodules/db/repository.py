"""Generic repository base class for CRUD operations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Select

from .base import Base
from .mixins import SoftDeleteMixin

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """Generic repository providing CRUD operations.

    Provides a clean interface for database operations with automatic
    soft delete filtering and pagination support.

    Example:
        class UserRepository(BaseRepository[User]):
            def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
                super().__init__(session_factory, User)

            async def find_by_email(self, email: str) -> User | None:
                return await self.find_one(email=email)

        # Usage
        repo = UserRepository(db.session_factory)
        user = await repo.create(name="John", email="john@example.com")
        users = await repo.get_all(limit=10)
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        model_class: type[ModelT],
    ):
        """Initialize repository.

        Args:
            session_factory: Async session factory from DatabaseManager
            model_class: SQLAlchemy model class to operate on
        """
        self._session_factory = session_factory
        self._model_class = model_class

    def _session(self) -> AsyncSession:
        """Create a new session."""
        return self._session_factory()

    def _apply_soft_delete_filter(
        self, query: Select, include_deleted: bool = False
    ) -> Select:
        """Apply soft delete filter if model supports it.

        Args:
            query: The SQLAlchemy select query
            include_deleted: If True, include soft-deleted records

        Returns:
            Modified query with soft delete filter applied
        """
        if include_deleted:
            return query
        if hasattr(self._model_class, "is_deleted"):
            query = query.where(self._model_class.is_deleted == False)  # noqa: E712
        return query

    async def get_by_id(
        self, id: UUID | str, *, include_deleted: bool = False
    ) -> ModelT | None:
        """Get a record by ID.

        Args:
            id: Record UUID (string or UUID object)
            include_deleted: If True, include soft-deleted records

        Returns:
            Model instance or None if not found
        """
        if isinstance(id, str):
            id = UUID(id)

        async with self._session() as session:
            query = select(self._model_class).where(self._model_class.id == id)
            query = self._apply_soft_delete_filter(query, include_deleted)
            result = await session.execute(query)
            return result.scalar_one_or_none()

    async def get_all(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        order_by: str | None = None,
        descending: bool = False,
        include_deleted: bool = False,
    ) -> Sequence[ModelT]:
        """Get all records with pagination.

        Args:
            offset: Number of records to skip
            limit: Maximum number of records to return
            order_by: Column name to order by
            descending: Whether to sort descending
            include_deleted: If True, include soft-deleted records

        Returns:
            List of model instances
        """
        async with self._session() as session:
            query = select(self._model_class)
            query = self._apply_soft_delete_filter(query, include_deleted)

            if order_by and hasattr(self._model_class, order_by):
                column = getattr(self._model_class, order_by)
                query = query.order_by(column.desc() if descending else column)
            elif hasattr(self._model_class, "created_at"):
                query = query.order_by(self._model_class.created_at.desc())

            query = query.offset(offset).limit(limit)
            result = await session.execute(query)
            return result.scalars().all()

    async def find_one(
        self, *, include_deleted: bool = False, **kwargs: Any
    ) -> ModelT | None:
        """Find a single record by attributes.

        Args:
            include_deleted: If True, include soft-deleted records
            **kwargs: Attribute filters (e.g., email="test@example.com")

        Returns:
            Model instance or None if not found
        """
        async with self._session() as session:
            query = select(self._model_class).filter_by(**kwargs)
            query = self._apply_soft_delete_filter(query, include_deleted)
            result = await session.execute(query)
            return result.scalar_one_or_none()

    async def find_many(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        include_deleted: bool = False,
        **kwargs: Any,
    ) -> Sequence[ModelT]:
        """Find records by attributes.

        Args:
            offset: Number of records to skip
            limit: Maximum number of records to return
            include_deleted: If True, include soft-deleted records
            **kwargs: Attribute filters

        Returns:
            List of model instances
        """
        async with self._session() as session:
            query = select(self._model_class).filter_by(**kwargs)
            query = self._apply_soft_delete_filter(query, include_deleted)
            query = query.offset(offset).limit(limit)
            result = await session.execute(query)
            return result.scalars().all()

    async def create(self, **kwargs: Any) -> ModelT:
        """Create a new record.

        Args:
            **kwargs: Model attributes

        Returns:
            Created model instance with generated ID
        """
        async with self._session() as session:
            instance = self._model_class(**kwargs)
            session.add(instance)
            await session.flush()
            await session.refresh(instance)
            await session.commit()
            return instance

    async def update(self, id: UUID | str, **kwargs: Any) -> ModelT | None:
        """Update a record by ID.

        Args:
            id: Record UUID
            **kwargs: Attributes to update

        Returns:
            Updated model instance or None if not found
        """
        if isinstance(id, str):
            id = UUID(id)

        async with self._session() as session:
            query = select(self._model_class).where(self._model_class.id == id)
            query = self._apply_soft_delete_filter(query)
            result = await session.execute(query)
            instance = result.scalar_one_or_none()

            if instance is None:
                return None

            for key, value in kwargs.items():
                if hasattr(instance, key):
                    setattr(instance, key, value)

            await session.flush()
            await session.refresh(instance)
            await session.commit()
            return instance

    async def delete(self, id: UUID | str, *, soft: bool = True) -> bool:
        """Delete a record by ID.

        Args:
            id: Record UUID
            soft: If True and model supports it, soft delete instead of hard delete

        Returns:
            True if record was deleted, False if not found
        """
        if isinstance(id, str):
            id = UUID(id)

        async with self._session() as session:
            query = select(self._model_class).where(self._model_class.id == id)
            query = self._apply_soft_delete_filter(query)
            result = await session.execute(query)
            instance = result.scalar_one_or_none()

            if instance is None:
                return False

            if soft and isinstance(instance, SoftDeleteMixin):
                instance.soft_delete()
            else:
                await session.delete(instance)

            await session.commit()
            return True

    async def count(
        self, *, include_deleted: bool = False, **kwargs: Any
    ) -> int:
        """Count records matching filters.

        Args:
            include_deleted: If True, include soft-deleted records
            **kwargs: Attribute filters

        Returns:
            Number of matching records
        """
        async with self._session() as session:
            query = (
                select(func.count())
                .select_from(self._model_class)
                .filter_by(**kwargs)
            )
            query = self._apply_soft_delete_filter(query, include_deleted)
            result = await session.execute(query)
            return result.scalar_one()

    async def exists(self, id: UUID | str) -> bool:
        """Check if a record with the given ID exists.

        Args:
            id: Record UUID to check

        Returns:
            True if record exists (and is not soft-deleted)
        """
        if isinstance(id, str):
            id = UUID(id)

        async with self._session() as session:
            query = (
                select(func.count())
                .select_from(self._model_class)
                .where(self._model_class.id == id)
            )
            query = self._apply_soft_delete_filter(query)
            result = await session.execute(query)
            return result.scalar_one() > 0

    def _apply_filters(self, query: Select, filters: dict[str, Any]) -> Select:
        """Apply filters to a query.

        Override this method for custom filter logic (e.g., range queries).

        Args:
            query: The SQLAlchemy select query
            filters: Dictionary of filters to apply

        Returns:
            Modified query with filters applied
        """
        for key, value in filters.items():
            if hasattr(self._model_class, key):
                column = getattr(self._model_class, key)
                if isinstance(value, list):
                    query = query.where(column.in_(value))
                else:
                    query = query.where(column == value)
        return query
