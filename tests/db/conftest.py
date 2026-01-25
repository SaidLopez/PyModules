"""Shared fixtures for database layer tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.fixture
async def async_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Create an in-memory SQLite engine for testing."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(
    async_engine: AsyncEngine,
) -> AsyncGenerator[AsyncSession, None]:
    """Create an async session for testing."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    # Import Base to create tables
    from pymodules.db import Base

    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
def sample_uuid() -> UUID:
    """Return a fixed UUID for testing."""
    from uuid import UUID

    return UUID("12345678-1234-5678-1234-567812345678")


@pytest.fixture
async def session_factory(
    async_engine: AsyncEngine,
) -> AsyncGenerator[async_sessionmaker, None]:
    """Create a session factory for repository tests."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from pymodules.db import Base

    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    yield factory
