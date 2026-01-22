"""Tests for Base declarative class."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, String


class TestBase:
    """Tests for SQLAlchemy Base class."""

    def test_base_is_declarative(self) -> None:
        """Base should be a DeclarativeBase."""
        from sqlalchemy.orm import DeclarativeBase

        from pymodules.db import Base

        assert issubclass(Base, DeclarativeBase)

    def test_can_create_models(self) -> None:
        """Models can inherit from Base."""
        from pymodules.db import Base

        class TestModel(Base):
            __tablename__ = "test_models"
            id = Column(String, primary_key=True)

        assert hasattr(TestModel, "__tablename__")
        assert hasattr(TestModel, "__table__")

    def test_models_have_metadata(self) -> None:
        """Models should have access to metadata."""
        from pymodules.db import Base

        assert Base.metadata is not None

    @pytest.mark.asyncio
    async def test_create_all_creates_tables(self, async_engine) -> None:
        """Base.metadata.create_all should create tables."""
        from sqlalchemy import Column, String, inspect

        from pymodules.db import Base

        class TempModel(Base):
            __tablename__ = "temp_test_table"
            id = Column(String, primary_key=True)

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

            def check_table(connection):
                inspector = inspect(connection)
                return inspector.has_table("temp_test_table")

            has_table = await conn.run_sync(check_table)
            assert has_table
