"""Tests for database mixins."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Column, String, select


class TestUUIDMixin:
    """Tests for UUIDMixin."""

    @pytest.mark.asyncio
    async def test_generates_uuid_on_create(self, async_engine) -> None:
        """UUIDMixin should generate UUID on insert."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from pymodules.db import Base, UUIDMixin

        class UUIDModel(UUIDMixin, Base):
            __tablename__ = "uuid_test"
            name = Column(String(50))

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(async_engine, class_=AsyncSession)

        async with session_factory() as session:
            model = UUIDModel(name="test")
            session.add(model)
            await session.commit()
            await session.refresh(model)

            assert model.id is not None

    @pytest.mark.asyncio
    async def test_uuid_is_valid_uuid4(self, async_engine) -> None:
        """Generated UUID should be a valid UUID."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from pymodules.db import Base, UUIDMixin

        class UUIDModel(UUIDMixin, Base):
            __tablename__ = "uuid_valid_test"
            name = Column(String(50))

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(async_engine, class_=AsyncSession)

        async with session_factory() as session:
            model = UUIDModel(name="test")
            session.add(model)
            await session.commit()
            await session.refresh(model)

            assert isinstance(model.id, UUID)
            # UUID version 4 has specific format
            assert model.id.version == 4

    def test_uuid_is_primary_key(self) -> None:
        """UUIDMixin.id should be the primary key."""
        from sqlalchemy import Column, String

        from pymodules.db import Base, UUIDMixin

        class UUIDModel(UUIDMixin, Base):
            __tablename__ = "uuid_pk_test"
            name = Column(String(50))

        # Check that id is in primary key columns
        pk_cols = [c.name for c in UUIDModel.__table__.primary_key.columns]
        assert "id" in pk_cols


class TestTimestampMixin:
    """Tests for TimestampMixin."""

    @pytest.mark.asyncio
    async def test_created_at_set_on_insert(self, async_engine) -> None:
        """TimestampMixin should set created_at on insert."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from pymodules.db import Base, TimestampMixin, UUIDMixin

        class TimestampModel(UUIDMixin, TimestampMixin, Base):
            __tablename__ = "ts_created_test"
            name = Column(String(50))

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(async_engine, class_=AsyncSession)

        before = datetime.now(UTC)
        async with session_factory() as session:
            model = TimestampModel(name="test")
            session.add(model)
            await session.commit()
            await session.refresh(model)
        after = datetime.now(UTC)

        # created_at may be timezone-naive, normalize for comparison
        created_at = model.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)

        assert before <= created_at <= after

    @pytest.mark.asyncio
    async def test_updated_at_set_on_insert(self, async_engine) -> None:
        """TimestampMixin should set updated_at on insert."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from pymodules.db import Base, TimestampMixin, UUIDMixin

        class TimestampModel(UUIDMixin, TimestampMixin, Base):
            __tablename__ = "ts_updated_insert_test"
            name = Column(String(50))

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(async_engine, class_=AsyncSession)

        async with session_factory() as session:
            model = TimestampModel(name="test")
            session.add(model)
            await session.commit()
            await session.refresh(model)

        assert model.updated_at is not None

    @pytest.mark.asyncio
    async def test_updated_at_changes_on_update(self, async_engine) -> None:
        """TimestampMixin should update updated_at on update."""
        import asyncio

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from pymodules.db import Base, TimestampMixin, UUIDMixin

        class TimestampModel(UUIDMixin, TimestampMixin, Base):
            __tablename__ = "ts_updated_change_test"
            name = Column(String(50))

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(async_engine, class_=AsyncSession)

        async with session_factory() as session:
            model = TimestampModel(name="test")
            session.add(model)
            await session.commit()
            await session.refresh(model)
            original_updated_at = model.updated_at

        # Small delay to ensure timestamp changes
        await asyncio.sleep(0.01)

        async with session_factory() as session:
            result = await session.execute(
                select(TimestampModel).where(TimestampModel.id == model.id)
            )
            model = result.scalar_one()
            model.name = "updated"
            await session.commit()
            await session.refresh(model)

        assert model.updated_at > original_updated_at

    @pytest.mark.asyncio
    async def test_created_at_unchanged_on_update(self, async_engine) -> None:
        """TimestampMixin should not change created_at on update."""
        import asyncio

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from pymodules.db import Base, TimestampMixin, UUIDMixin

        class TimestampModel(UUIDMixin, TimestampMixin, Base):
            __tablename__ = "ts_created_unchanged_test"
            name = Column(String(50))

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(async_engine, class_=AsyncSession)

        async with session_factory() as session:
            model = TimestampModel(name="test")
            session.add(model)
            await session.commit()
            await session.refresh(model)
            original_created_at = model.created_at
            model_id = model.id

        await asyncio.sleep(0.01)

        async with session_factory() as session:
            result = await session.execute(
                select(TimestampModel).where(TimestampModel.id == model_id)
            )
            model = result.scalar_one()
            model.name = "updated"
            await session.commit()
            await session.refresh(model)

        assert model.created_at == original_created_at


class TestSoftDeleteMixin:
    """Tests for SoftDeleteMixin."""

    @pytest.mark.asyncio
    async def test_is_deleted_defaults_false(self, async_engine) -> None:
        """SoftDeleteMixin.is_deleted should default to False."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from pymodules.db import Base, SoftDeleteMixin, UUIDMixin

        class SoftDeleteModel(UUIDMixin, SoftDeleteMixin, Base):
            __tablename__ = "sd_default_test"
            name = Column(String(50))

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(async_engine, class_=AsyncSession)

        async with session_factory() as session:
            model = SoftDeleteModel(name="test")
            session.add(model)
            await session.commit()
            await session.refresh(model)

        assert model.is_deleted is False

    @pytest.mark.asyncio
    async def test_soft_delete_sets_flag(self, async_engine) -> None:
        """soft_delete() should set is_deleted to True."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from pymodules.db import Base, SoftDeleteMixin, UUIDMixin

        class SoftDeleteModel(UUIDMixin, SoftDeleteMixin, Base):
            __tablename__ = "sd_flag_test"
            name = Column(String(50))

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(async_engine, class_=AsyncSession)

        async with session_factory() as session:
            model = SoftDeleteModel(name="test")
            session.add(model)
            await session.commit()
            await session.refresh(model)

            model.soft_delete()
            await session.commit()
            await session.refresh(model)

        assert model.is_deleted is True

    @pytest.mark.asyncio
    async def test_soft_delete_sets_timestamp(self, async_engine) -> None:
        """soft_delete() should set deleted_at timestamp."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from pymodules.db import Base, SoftDeleteMixin, UUIDMixin

        class SoftDeleteModel(UUIDMixin, SoftDeleteMixin, Base):
            __tablename__ = "sd_timestamp_test"
            name = Column(String(50))

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(async_engine, class_=AsyncSession)

        before = datetime.now(UTC)
        async with session_factory() as session:
            model = SoftDeleteModel(name="test")
            session.add(model)
            await session.commit()

            model.soft_delete()
            await session.commit()
            await session.refresh(model)
        after = datetime.now(UTC)

        assert model.deleted_at is not None
        deleted_at = model.deleted_at
        if deleted_at.tzinfo is None:
            deleted_at = deleted_at.replace(tzinfo=UTC)
        assert before <= deleted_at <= after

    @pytest.mark.asyncio
    async def test_restore_clears_flag(self, async_engine) -> None:
        """restore() should set is_deleted to False."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from pymodules.db import Base, SoftDeleteMixin, UUIDMixin

        class SoftDeleteModel(UUIDMixin, SoftDeleteMixin, Base):
            __tablename__ = "sd_restore_flag_test"
            name = Column(String(50))

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(
            async_engine, class_=AsyncSession, expire_on_commit=False
        )

        async with session_factory() as session:
            model = SoftDeleteModel(name="test")
            session.add(model)
            await session.commit()

            model.soft_delete()
            await session.commit()
            assert model.is_deleted is True

            model.restore()
            await session.commit()
            assert model.is_deleted is False

    @pytest.mark.asyncio
    async def test_restore_clears_timestamp(self, async_engine) -> None:
        """restore() should clear deleted_at timestamp."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from pymodules.db import Base, SoftDeleteMixin, UUIDMixin

        class SoftDeleteModel(UUIDMixin, SoftDeleteMixin, Base):
            __tablename__ = "sd_restore_timestamp_test"
            name = Column(String(50))

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(
            async_engine, class_=AsyncSession, expire_on_commit=False
        )

        async with session_factory() as session:
            model = SoftDeleteModel(name="test")
            session.add(model)
            await session.commit()

            model.soft_delete()
            await session.commit()
            assert model.deleted_at is not None

            model.restore()
            await session.commit()
            assert model.deleted_at is None
