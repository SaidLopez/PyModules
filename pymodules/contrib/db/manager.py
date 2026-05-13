"""Async SQLAlchemy database session management."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    pass


class DatabaseManager:
    """Manages async database connections and sessions.

    Provides a high-level interface for managing database connections,
    session creation, and transaction handling.

    Example:
        db = DatabaseManager("postgresql+asyncpg://localhost/mydb")
        await db.connect()

        async with db.session() as session:
            # Automatic commit on success, rollback on exception
            result = await session.execute(select(User))
            users = result.scalars().all()

        await db.disconnect()

    With FastAPI dependency injection:
        @router.get("/users")
        async def get_users(session: AsyncSession = Depends(db.get_session)):
            result = await session.execute(select(User))
            return result.scalars().all()
    """

    def __init__(
        self,
        database_url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_timeout: float = 30.0,
        pool_recycle: int = 1800,
        echo: bool = False,
    ):
        """Initialize database manager.

        Args:
            database_url: Async database URL (e.g., postgresql+asyncpg://...)
            pool_size: Connection pool size
            max_overflow: Maximum overflow connections
            pool_timeout: Pool connection timeout in seconds
            pool_recycle: Recycle connections after this many seconds
            echo: Whether to echo SQL statements
        """
        self._database_url = database_url
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._pool_timeout = pool_timeout
        self._pool_recycle = pool_recycle
        self._echo = echo
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        """Get the database engine.

        Raises:
            RuntimeError: If database is not connected.
        """
        if self._engine is None:
            raise RuntimeError("Database not initialized. Call connect() first.")
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Get the session factory.

        Raises:
            RuntimeError: If database is not connected.
        """
        if self._session_factory is None:
            raise RuntimeError("Database not initialized. Call connect() first.")
        return self._session_factory

    async def connect(self) -> None:
        """Connect to the database and create engine/session factory.

        This method is idempotent - calling it multiple times is safe.
        """
        if self._engine is not None:
            return

        # Check if this is SQLite (doesn't support pooling the same way)
        is_sqlite = "sqlite" in self._database_url.lower()

        if is_sqlite:
            self._engine = create_async_engine(
                self._database_url,
                echo=self._echo,
            )
        else:
            self._engine = create_async_engine(
                self._database_url,
                pool_size=self._pool_size,
                max_overflow=self._max_overflow,
                pool_timeout=self._pool_timeout,
                pool_recycle=self._pool_recycle,
                echo=self._echo,
            )

        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )

    async def disconnect(self) -> None:
        """Disconnect from the database.

        This method is idempotent - calling it multiple times is safe.
        """
        if self._engine is None:
            return

        await self._engine.dispose()
        self._engine = None
        self._session_factory = None

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get a database session context manager.

        The session automatically commits on success and rolls back on exception.

        Usage:
            async with db_manager.session() as session:
                # Your database operations
                result = await session.execute(select(User))

        Yields:
            AsyncSession: A database session.

        Raises:
            RuntimeError: If database is not connected.
        """
        if self._session_factory is None:
            raise RuntimeError("Database not initialized. Call connect() first.")

        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Dependency injection compatible session generator.

        Usage with FastAPI:
            @router.get("/items")
            async def get_items(
                session: AsyncSession = Depends(db_manager.get_session)
            ):
                result = await session.execute(select(Item))
                return result.scalars().all()

        Yields:
            AsyncSession: A database session.
        """
        async with self.session() as session:
            yield session
