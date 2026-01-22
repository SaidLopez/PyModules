"""Database layer example.

This example demonstrates how to use the pymodules.db layer for:
- Database connection management
- Model definitions with mixins
- Repository pattern for CRUD operations

Requirements:
    pip install 'pymodules[sqlite]'  # or 'pymodules[postgres]'
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Column, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pymodules.db import Base, BaseRepository, SoftDeleteMixin, TimestampMixin, UUIDMixin


# =============================================================================
# Model Definition
# =============================================================================


class User(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """User model with UUID primary key, timestamps, and soft delete.

    Inherits:
        UUIDMixin: Provides auto-generated UUID primary key
        TimestampMixin: Provides created_at and updated_at columns
        SoftDeleteMixin: Provides is_deleted flag and soft_delete()/restore() methods
        Base: SQLAlchemy declarative base
    """

    __tablename__ = "users"

    name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False, unique=True)

    def __repr__(self) -> str:
        return f"<User(id={self.id}, name={self.name}, email={self.email})>"


# =============================================================================
# Repository Usage
# =============================================================================


async def main():
    """Demonstrate database layer usage."""

    # Create async engine (SQLite for this example)
    engine = create_async_engine(
        "sqlite+aiosqlite:///example.db",
        echo=True,  # Set to False in production
    )

    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Create session factory
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Create repository
    user_repo = BaseRepository(session_factory, User)

    print("\n" + "=" * 60)
    print("PyModules Database Layer Example")
    print("=" * 60)

    # -------------------------------------------------------------------------
    # Create users
    # -------------------------------------------------------------------------
    print("\n1. Creating users...")

    user1 = await user_repo.create(name="Alice", email="alice@example.com")
    print(f"   Created: {user1}")

    user2 = await user_repo.create(name="Bob", email="bob@example.com")
    print(f"   Created: {user2}")

    user3 = await user_repo.create(name="Charlie", email="charlie@example.com")
    print(f"   Created: {user3}")

    # -------------------------------------------------------------------------
    # Read operations
    # -------------------------------------------------------------------------
    print("\n2. Reading users...")

    # Get by ID
    fetched_user = await user_repo.get_by_id(user1.id)
    print(f"   Get by ID: {fetched_user}")

    # Get all users
    all_users = await user_repo.get_all()
    print(f"   Total users: {len(all_users)}")

    # Get with pagination
    paginated = await user_repo.get_all(limit=2, offset=0)
    print(f"   First page (2 users): {[u.name for u in paginated]}")

    # Find by criteria
    alice = await user_repo.find_one(name="Alice")
    print(f"   Find by name: {alice}")

    # Count
    count = await user_repo.count()
    print(f"   Count: {count}")

    # -------------------------------------------------------------------------
    # Update operations
    # -------------------------------------------------------------------------
    print("\n3. Updating users...")

    updated_user = await user_repo.update(user1.id, name="Alice Smith")
    print(f"   Updated: {updated_user}")

    # Check updated_at changed
    print(f"   Created at: {updated_user.created_at}")
    print(f"   Updated at: {updated_user.updated_at}")

    # -------------------------------------------------------------------------
    # Soft delete
    # -------------------------------------------------------------------------
    print("\n4. Soft delete...")

    # Soft delete user2
    await user_repo.soft_delete(user2.id)
    print(f"   Soft deleted: Bob")

    # Verify it's excluded from normal queries
    active_users = await user_repo.get_all()
    print(f"   Active users after soft delete: {len(active_users)}")

    # Can still get with include_deleted
    all_including_deleted = await user_repo.get_all(include_deleted=True)
    print(f"   All users (including deleted): {len(all_including_deleted)}")

    # Restore
    await user_repo.restore(user2.id)
    print(f"   Restored: Bob")

    active_after_restore = await user_repo.get_all()
    print(f"   Active users after restore: {len(active_after_restore)}")

    # -------------------------------------------------------------------------
    # Hard delete
    # -------------------------------------------------------------------------
    print("\n5. Hard delete...")

    deleted = await user_repo.delete(user3.id)
    print(f"   Deleted Charlie: {deleted}")

    final_count = await user_repo.count()
    print(f"   Final count: {final_count}")

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------
    print("\n6. Cleanup...")

    # Drop tables and dispose engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()
    print("   Done!")

    print("\n" + "=" * 60)
    print("Example complete!")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
