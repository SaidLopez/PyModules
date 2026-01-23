"""Database abstraction layer for PyModules.

This module provides database utilities including:
- DatabaseManager: Async session management
- Base: SQLAlchemy declarative base
- Mixins: UUIDMixin, TimestampMixin, SoftDeleteMixin
- BaseRepository: Generic CRUD repository
- DatabaseSettings: Configuration via environment variables

Example:
    from pymodules.db import DatabaseManager, Base, BaseRepository

    db = DatabaseManager("postgresql+asyncpg://localhost/mydb")
    await db.connect()

    async with db.session() as session:
        # use session
        pass
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pymodules._imports import require_optional_dependency

# Check for SQLAlchemy dependency at import time
require_optional_dependency("sqlalchemy", "pymodules.db", "db")

if TYPE_CHECKING:
    from .base import Base
    from .manager import DatabaseManager
    from .mixins import SoftDeleteMixin, TimestampMixin, UUIDMixin
    from .repository import BaseRepository
    from .settings import DatabaseSettings

__all__ = [
    "Base",
    "BaseRepository",
    "DatabaseManager",
    "DatabaseSettings",
    "SoftDeleteMixin",
    "TimestampMixin",
    "UUIDMixin",
]


def __getattr__(name: str):
    """Lazy load database components."""
    if name == "Base":
        from .base import Base

        return Base
    elif name == "DatabaseManager":
        from .manager import DatabaseManager

        return DatabaseManager
    elif name == "UUIDMixin":
        from .mixins import UUIDMixin

        return UUIDMixin
    elif name == "TimestampMixin":
        from .mixins import TimestampMixin

        return TimestampMixin
    elif name == "SoftDeleteMixin":
        from .mixins import SoftDeleteMixin

        return SoftDeleteMixin
    elif name == "BaseRepository":
        from .repository import BaseRepository

        return BaseRepository
    elif name == "DatabaseSettings":
        from .settings import DatabaseSettings

        return DatabaseSettings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
