"""SQLAlchemy model mixins for common functionality."""

from __future__ import annotations

import uuid as uuid_module
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, String, TypeDecorator, func
from sqlalchemy.orm import Mapped, mapped_column

if TYPE_CHECKING:
    from sqlalchemy.engine import Dialect


class UUIDType(TypeDecorator):
    """Platform-independent UUID type.

    Uses String(36) storage but handles UUID conversion transparently.
    Works with both PostgreSQL and SQLite.
    """

    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        """Convert UUID to string for storage."""
        if value is None:
            return None
        if isinstance(value, uuid_module.UUID):
            return str(value)
        return str(uuid_module.UUID(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> uuid_module.UUID | None:
        """Convert string back to UUID on retrieval."""
        if value is None:
            return None
        if isinstance(value, uuid_module.UUID):
            return value
        return uuid_module.UUID(value)


class UUIDMixin:
    """Mixin that provides a UUID primary key.

    Adds an `id` column as the primary key with a default UUID v4 value.
    Works with both PostgreSQL (native UUID type) and SQLite (stored as string).

    Example:
        class User(UUIDMixin, Base):
            __tablename__ = "users"
            name = Column(String(100))
    """

    id: Mapped[uuid_module.UUID] = mapped_column(
        UUIDType(),
        primary_key=True,
        default=uuid_module.uuid4,
    )


class TimestampMixin:
    """Mixin that provides created_at and updated_at timestamps.

    - `created_at`: Set automatically when the record is created
    - `updated_at`: Set automatically on create and updated on every modification

    Example:
        class User(UUIDMixin, TimestampMixin, Base):
            __tablename__ = "users"
            name = Column(String(100))
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
    )


class SoftDeleteMixin:
    """Mixin for soft delete functionality.

    Instead of permanently deleting records, they are marked as deleted
    with a timestamp. This allows for recovery and audit trails.

    Attributes:
        is_deleted: Boolean flag indicating if the record is deleted
        deleted_at: Timestamp of when the record was deleted

    Methods:
        soft_delete(): Mark the record as deleted
        restore(): Restore a soft-deleted record

    Example:
        class User(UUIDMixin, SoftDeleteMixin, Base):
            __tablename__ = "users"
            name = Column(String(100))

        # Soft delete a user
        user.soft_delete()
        session.commit()

        # Restore a user
        user.restore()
        session.commit()
    """

    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        index=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    def soft_delete(self) -> None:
        """Mark the record as deleted."""
        self.is_deleted = True
        self.deleted_at = datetime.now(UTC)

    def restore(self) -> None:
        """Restore a soft-deleted record."""
        self.is_deleted = False
        self.deleted_at = None
