"""Tests for DatabaseSettings."""

from __future__ import annotations

import os

import pytest


class TestDatabaseSettings:
    """Tests for DatabaseSettings."""

    def test_loads_from_env_vars(self, monkeypatch) -> None:
        """DatabaseSettings should load from environment variables."""
        monkeypatch.setenv("PYMODULES_DB_URL", "postgresql+asyncpg://user:pass@host/db")
        monkeypatch.setenv("PYMODULES_DB_POOL_SIZE", "10")
        monkeypatch.setenv("PYMODULES_DB_ECHO", "true")

        from pymodules.db import DatabaseSettings

        settings = DatabaseSettings()

        assert settings.url == "postgresql+asyncpg://user:pass@host/db"
        assert settings.pool_size == 10
        assert settings.echo is True

    def test_default_values(self) -> None:
        """DatabaseSettings should have sensible defaults."""
        # Clear any existing env vars
        env_backup = {}
        for key in list(os.environ.keys()):
            if key.startswith("PYMODULES_DB_"):
                env_backup[key] = os.environ.pop(key)

        try:
            from pymodules.db import DatabaseSettings

            settings = DatabaseSettings()

            assert settings.pool_size == 5
            assert settings.max_overflow == 10
            assert settings.pool_timeout == 30
            assert settings.pool_recycle == 3600
            assert settings.echo is False
        finally:
            # Restore env vars
            os.environ.update(env_backup)

    def test_env_prefix(self, monkeypatch) -> None:
        """DatabaseSettings should use PYMODULES_DB_ prefix."""
        # Set with prefix
        monkeypatch.setenv("PYMODULES_DB_POOL_SIZE", "20")
        # Set without prefix (should be ignored)
        monkeypatch.setenv("POOL_SIZE", "100")

        from pymodules.db import DatabaseSettings

        settings = DatabaseSettings()

        assert settings.pool_size == 20

    def test_url_construction(self, monkeypatch) -> None:
        """DatabaseSettings should support URL construction from parts."""
        monkeypatch.setenv("PYMODULES_DB_DRIVER", "postgresql+asyncpg")
        monkeypatch.setenv("PYMODULES_DB_HOST", "localhost")
        monkeypatch.setenv("PYMODULES_DB_PORT", "5432")
        monkeypatch.setenv("PYMODULES_DB_NAME", "testdb")
        monkeypatch.setenv("PYMODULES_DB_USER", "testuser")
        monkeypatch.setenv("PYMODULES_DB_PASSWORD", "testpass")

        from pymodules.db import DatabaseSettings

        settings = DatabaseSettings()

        # Either url is set directly or can be constructed
        constructed_url = settings.get_url()
        assert "postgresql+asyncpg" in constructed_url
        assert "localhost" in constructed_url
        assert "testdb" in constructed_url

    def test_sqlite_url(self, monkeypatch) -> None:
        """DatabaseSettings should handle SQLite URLs."""
        monkeypatch.setenv("PYMODULES_DB_URL", "sqlite+aiosqlite:///./test.db")

        from pymodules.db import DatabaseSettings

        settings = DatabaseSettings()

        assert "sqlite" in settings.url
        assert "aiosqlite" in settings.url
