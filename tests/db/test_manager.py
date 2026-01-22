"""Tests for DatabaseManager class."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
class TestDatabaseManager:
    """Tests for DatabaseManager class."""

    async def test_initialize_creates_engine(self) -> None:
        """DatabaseManager should create engine on connect."""
        from pymodules.db import DatabaseManager

        manager = DatabaseManager("sqlite+aiosqlite:///:memory:")
        await manager.connect()

        assert manager.engine is not None
        await manager.disconnect()

    async def test_session_context_manager(self) -> None:
        """Session context manager should yield async session."""
        from pymodules.db import DatabaseManager

        manager = DatabaseManager("sqlite+aiosqlite:///:memory:")
        await manager.connect()

        async with manager.session() as session:
            assert session is not None

        await manager.disconnect()

    async def test_session_commits_on_success(self) -> None:
        """Session should commit when no exception occurs."""
        from sqlalchemy import text

        from pymodules.db import Base, DatabaseManager

        manager = DatabaseManager("sqlite+aiosqlite:///:memory:")
        await manager.connect()

        async with manager.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with manager.session() as session:
            await session.execute(
                text("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY)")
            )
            await session.execute(text("INSERT INTO test_table (id) VALUES (1)"))

        # Verify data persisted
        async with manager.session() as session:
            result = await session.execute(text("SELECT id FROM test_table"))
            rows = result.fetchall()
            assert len(rows) == 1

        await manager.disconnect()

    async def test_session_rollbacks_on_error(self) -> None:
        """Session should rollback when exception occurs."""
        from sqlalchemy import text

        from pymodules.db import Base, DatabaseManager

        manager = DatabaseManager("sqlite+aiosqlite:///:memory:")
        await manager.connect()

        async with manager.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Create table first
        async with manager.session() as session:
            await session.execute(
                text("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY)")
            )

        # This should rollback due to exception
        with pytest.raises(RuntimeError):
            async with manager.session() as session:
                await session.execute(text("INSERT INTO test_table (id) VALUES (99)"))
                raise RuntimeError("Test error")

        # Verify data was not persisted
        async with manager.session() as session:
            result = await session.execute(text("SELECT id FROM test_table WHERE id = 99"))
            rows = result.fetchall()
            assert len(rows) == 0

        await manager.disconnect()

    async def test_close_disposes_engine(self) -> None:
        """Disconnect should dispose of the engine."""
        from pymodules.db import DatabaseManager

        manager = DatabaseManager("sqlite+aiosqlite:///:memory:")
        await manager.connect()

        engine = manager.engine
        await manager.disconnect()

        # Engine should be disposed (pool closed)
        assert manager._engine is None

    async def test_engine_property_raises_before_init(self) -> None:
        """Accessing engine before connect should raise."""
        from pymodules.db import DatabaseManager

        manager = DatabaseManager("sqlite+aiosqlite:///:memory:")

        with pytest.raises(RuntimeError, match="not initialized"):
            _ = manager.engine

    async def test_get_session_dependency(self) -> None:
        """get_session should work as FastAPI dependency."""
        from pymodules.db import DatabaseManager

        manager = DatabaseManager("sqlite+aiosqlite:///:memory:")
        await manager.connect()

        sessions = []
        async for session in manager.get_session():
            sessions.append(session)

        assert len(sessions) == 1
        assert sessions[0] is not None

        await manager.disconnect()

    async def test_double_connect_is_safe(self) -> None:
        """Calling connect twice should be idempotent."""
        from pymodules.db import DatabaseManager

        manager = DatabaseManager("sqlite+aiosqlite:///:memory:")
        await manager.connect()
        engine1 = manager.engine

        await manager.connect()
        engine2 = manager.engine

        assert engine1 is engine2
        await manager.disconnect()

    async def test_disconnect_without_connect_is_safe(self) -> None:
        """Calling disconnect without connect should not raise."""
        from pymodules.db import DatabaseManager

        manager = DatabaseManager("sqlite+aiosqlite:///:memory:")
        await manager.disconnect()  # Should not raise
