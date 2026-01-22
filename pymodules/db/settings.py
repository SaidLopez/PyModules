"""Database settings configuration via environment variables."""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database connection settings loaded from environment variables.

    All settings use the PYMODULES_DB_ prefix by default.

    Environment Variables:
        PYMODULES_DB_URL: Full database URL (takes precedence)
        PYMODULES_DB_DRIVER: Database driver (e.g., postgresql+asyncpg)
        PYMODULES_DB_HOST: Database host
        PYMODULES_DB_PORT: Database port
        PYMODULES_DB_NAME: Database name
        PYMODULES_DB_USER: Database user
        PYMODULES_DB_PASSWORD: Database password
        PYMODULES_DB_POOL_SIZE: Connection pool size
        PYMODULES_DB_MAX_OVERFLOW: Maximum overflow connections
        PYMODULES_DB_POOL_TIMEOUT: Pool connection timeout
        PYMODULES_DB_POOL_RECYCLE: Connection recycle time
        PYMODULES_DB_ECHO: Whether to echo SQL statements

    Example:
        # Using full URL
        export PYMODULES_DB_URL="postgresql+asyncpg://user:pass@localhost/mydb"

        # Or using individual settings
        export PYMODULES_DB_DRIVER="postgresql+asyncpg"
        export PYMODULES_DB_HOST="localhost"
        export PYMODULES_DB_NAME="mydb"
        export PYMODULES_DB_USER="user"
        export PYMODULES_DB_PASSWORD="pass"

        # In code
        from pymodules.db import DatabaseSettings
        settings = DatabaseSettings()
        url = settings.get_url()
    """

    model_config = SettingsConfigDict(
        env_prefix="PYMODULES_DB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Direct URL (takes precedence)
    url: str | None = Field(
        default=None,
        description="Full async database URL",
    )

    # Individual connection settings (used if url is not set)
    driver: str = Field(
        default="postgresql+asyncpg",
        description="Database driver (e.g., postgresql+asyncpg, sqlite+aiosqlite)",
    )
    host: str = Field(
        default="localhost",
        description="Database host",
    )
    port: int = Field(
        default=5432,
        description="Database port",
    )
    name: str = Field(
        default="pymodules",
        description="Database name",
    )
    user: str = Field(
        default="postgres",
        description="Database user",
    )
    password: str = Field(
        default="",
        description="Database password",
    )

    # Pool settings
    pool_size: int = Field(
        default=5,
        description="Connection pool size",
    )
    max_overflow: int = Field(
        default=10,
        description="Maximum overflow connections",
    )
    pool_timeout: float = Field(
        default=30.0,
        description="Pool connection timeout in seconds",
    )
    pool_recycle: int = Field(
        default=3600,
        description="Recycle connections after this many seconds",
    )
    echo: bool = Field(
        default=False,
        description="Echo SQL statements (for debugging)",
    )

    def get_url(self) -> str:
        """Get the database URL.

        If `url` is set, returns it directly. Otherwise, constructs
        the URL from individual settings.

        Returns:
            Database URL string
        """
        if self.url:
            return self.url

        # Handle SQLite specially
        if "sqlite" in self.driver:
            return f"{self.driver}:///{self.name}"

        # Construct URL from parts
        auth = ""
        if self.user:
            auth = self.user
            if self.password:
                auth += f":{self.password}"
            auth += "@"

        return f"{self.driver}://{auth}{self.host}:{self.port}/{self.name}"

    def model_post_init(self, __context: Any) -> None:
        """Validate settings after initialization."""
        # If url is set, no further validation needed
        if self.url:
            return

        # Basic validation for constructed URLs
        if not self.driver:
            raise ValueError("Database driver is required when URL is not set")
        if "sqlite" not in self.driver and not self.host:
            raise ValueError("Database host is required for non-SQLite databases")
