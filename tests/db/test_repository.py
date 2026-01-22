"""Tests for BaseRepository class."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import Column, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pymodules.db import Base, BaseRepository, SoftDeleteMixin, TimestampMixin, UUIDMixin


# Define test model at module level to avoid redefinition issues
class RepoTestEntity(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Test entity for repository tests."""

    __tablename__ = "repo_test_entities"
    name = Column(String(100), nullable=False)
    status = Column(String(50), default="active")


class TestBaseRepository:
    """Tests for BaseRepository[ModelT]."""

    @pytest.fixture
    async def engine_and_session(self):
        """Create engine and session factory."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )

        yield engine, session_factory

        await engine.dispose()

    @pytest.fixture
    async def repository(self, engine_and_session):
        """Create a repository for testing."""
        engine, session_factory = engine_and_session
        return BaseRepository(session_factory, RepoTestEntity)

    @pytest.fixture
    async def populated_repository(self, engine_and_session):
        """Repository with some test data."""
        engine, session_factory = engine_and_session
        repo = BaseRepository(session_factory, RepoTestEntity)

        async with session_factory() as session:
            entities = [
                RepoTestEntity(name="Entity 1", status="active"),
                RepoTestEntity(name="Entity 2", status="active"),
                RepoTestEntity(name="Entity 3", status="inactive"),
            ]
            for entity in entities:
                session.add(entity)
            await session.commit()
            for entity in entities:
                await session.refresh(entity)

        return repo, entities

    @pytest.mark.asyncio
    async def test_get_by_id_returns_entity(self, populated_repository) -> None:
        """get_by_id should return the entity with matching ID."""
        repository, entities = populated_repository

        result = await repository.get_by_id(entities[0].id)

        assert result is not None
        assert result.id == entities[0].id
        assert result.name == "Entity 1"

    @pytest.mark.asyncio
    async def test_get_by_id_returns_none_for_missing(self, repository) -> None:
        """get_by_id should return None for non-existent ID."""
        from uuid import uuid4

        result = await repository.get_by_id(uuid4())

        assert result is None

    @pytest.mark.asyncio
    async def test_list_returns_all(self, populated_repository) -> None:
        """list should return all entities."""
        repository, entities = populated_repository

        result = await repository.get_all()

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_list_with_limit(self, populated_repository) -> None:
        """list should respect limit parameter."""
        repository, entities = populated_repository

        result = await repository.get_all(limit=2)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_with_offset(self, populated_repository) -> None:
        """list should respect offset parameter."""
        repository, entities = populated_repository

        result = await repository.get_all(offset=1)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_with_filters(self, populated_repository) -> None:
        """list should support filtering."""
        repository, entities = populated_repository

        result = await repository.find_many(status="inactive")

        assert len(result) == 1
        assert result[0].status == "inactive"

    @pytest.mark.asyncio
    async def test_find_one_returns_match(self, populated_repository) -> None:
        """find_one should return matching entity."""
        repository, entities = populated_repository

        result = await repository.find_one(name="Entity 2")

        assert result is not None
        assert result.name == "Entity 2"

    @pytest.mark.asyncio
    async def test_find_one_returns_none(self, repository) -> None:
        """find_one should return None when no match."""
        result = await repository.find_one(name="Non-existent")

        assert result is None

    @pytest.mark.asyncio
    async def test_find_many_returns_matches(self, populated_repository) -> None:
        """find_many should return all matching entities."""
        repository, entities = populated_repository

        result = await repository.find_many(status="active")

        assert len(result) == 2
        assert all(e.status == "active" for e in result)

    @pytest.mark.asyncio
    async def test_create_persists_entity(self, engine_and_session) -> None:
        """create should persist entity to database."""
        engine, session_factory = engine_and_session
        repo = BaseRepository(session_factory, RepoTestEntity)

        entity = await repo.create(name="New Entity", status="pending")

        assert entity.id is not None

        # Verify in database
        async with session_factory() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(RepoTestEntity).where(RepoTestEntity.id == entity.id)
            )
            db_entity = result.scalar_one()
            assert db_entity.name == "New Entity"

    @pytest.mark.asyncio
    async def test_create_returns_with_id(self, repository) -> None:
        """create should return entity with generated ID."""
        entity = await repository.create(name="New Entity")

        assert entity.id is not None
        assert isinstance(entity.id, UUID)

    @pytest.mark.asyncio
    async def test_update_persists_changes(self, populated_repository) -> None:
        """update should persist changes to database."""
        repository, entities = populated_repository

        updated = await repository.update(entities[0].id, name="Updated Name")

        assert updated is not None
        assert updated.name == "Updated Name"

        # Verify change persisted
        fetched = await repository.get_by_id(entities[0].id)
        assert fetched.name == "Updated Name"

    @pytest.mark.asyncio
    async def test_delete_removes_entity(self, populated_repository) -> None:
        """delete should remove entity from database."""
        repository, entities = populated_repository

        result = await repository.delete(entities[0].id, soft=False)

        assert result is True

        # Verify removed (include_deleted to check it's truly gone)
        fetched = await repository.get_by_id(entities[0].id, include_deleted=True)
        assert fetched is None

    @pytest.mark.asyncio
    async def test_count_returns_total(self, populated_repository) -> None:
        """count should return total number of entities."""
        repository, entities = populated_repository

        count = await repository.count()

        assert count == 3

    @pytest.mark.asyncio
    async def test_count_with_filters(self, populated_repository) -> None:
        """count should respect filters."""
        repository, entities = populated_repository

        count = await repository.count(status="active")

        assert count == 2

    @pytest.mark.asyncio
    async def test_exists_returns_true(self, populated_repository) -> None:
        """exists should return True for existing entity."""
        repository, entities = populated_repository

        exists = await repository.exists(entities[0].id)

        assert exists is True

    @pytest.mark.asyncio
    async def test_exists_returns_false(self, repository) -> None:
        """exists should return False for non-existent entity."""
        from uuid import uuid4

        exists = await repository.exists(uuid4())

        assert exists is False

    @pytest.mark.asyncio
    async def test_list_excludes_soft_deleted(self, populated_repository) -> None:
        """list should exclude soft-deleted entities by default."""
        repository, entities = populated_repository

        # Soft delete one entity
        await repository.delete(entities[0].id, soft=True)

        result = await repository.get_all()

        assert len(result) == 2
        assert all(e.id != entities[0].id for e in result)

    @pytest.mark.asyncio
    async def test_get_by_id_excludes_soft_deleted(self, populated_repository) -> None:
        """get_by_id should not return soft-deleted entities."""
        repository, entities = populated_repository

        await repository.delete(entities[0].id, soft=True)

        result = await repository.get_by_id(entities[0].id)

        assert result is None

    @pytest.mark.asyncio
    async def test_list_includes_soft_deleted_when_requested(
        self, populated_repository
    ) -> None:
        """list should include soft-deleted when include_deleted=True."""
        repository, entities = populated_repository

        await repository.delete(entities[0].id, soft=True)

        result = await repository.get_all(include_deleted=True)

        assert len(result) == 3
