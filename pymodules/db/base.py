"""SQLAlchemy declarative base for PyModules models."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models.

    All models should inherit from this class to ensure they are
    registered with the same metadata.

    Example:
        from pymodules.db import Base, UUIDMixin, TimestampMixin

        class User(UUIDMixin, TimestampMixin, Base):
            __tablename__ = "users"
            name = Column(String(100))
    """

    pass
