"""
Abstract service registry interface for PyModules.

Defines the base protocol for service discovery implementations.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# =============================================================================
# Exceptions
# =============================================================================


class DiscoveryError(Exception):
    """Base exception for discovery errors."""

    pass


class RegistrationError(DiscoveryError):
    """Raised when service registration fails."""

    pass


class ServiceNotFoundError(DiscoveryError):
    """Raised when a service cannot be found."""

    pass


# =============================================================================
# Service Instance
# =============================================================================


class ServiceStatus(Enum):
    """Status of a service instance."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"
    STARTING = "starting"
    STOPPING = "stopping"


@dataclass
class ServiceInstance:
    """
    Represents a single instance of a service.

    Attributes:
        service_name: Name of the service (e.g., "user-service").
        instance_id: Unique identifier for this instance.
        host: Hostname or IP address.
        port: Service port number.
        metadata: Additional instance metadata.
        status: Current health status.
        weight: Load balancing weight (higher = more traffic).
        zone: Availability zone or region.
        version: Service version.
        tags: Tags for filtering/routing.

    Example:
        instance = ServiceInstance(
            service_name="user-service",
            instance_id="user-service-abc123",
            host="10.0.0.5",
            port=8080,
            metadata={"region": "us-west-2"},
            tags=["primary", "v2"]
        )
    """

    service_name: str
    instance_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    host: str = "localhost"
    port: int = 8000
    metadata: dict[str, Any] = field(default_factory=dict)
    status: ServiceStatus = ServiceStatus.UNKNOWN
    weight: int = 100
    zone: str = ""
    version: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def address(self) -> str:
        """Get full address as host:port."""
        return f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        """Get HTTP URL for the service."""
        return f"http://{self.host}:{self.port}"

    def is_healthy(self) -> bool:
        """Check if instance is healthy."""
        return self.status == ServiceStatus.HEALTHY

    def to_dict(self) -> dict[str, Any]:
        """Serialize instance to dictionary."""
        return {
            "service_name": self.service_name,
            "instance_id": self.instance_id,
            "host": self.host,
            "port": self.port,
            "metadata": self.metadata,
            "status": self.status.value,
            "weight": self.weight,
            "zone": self.zone,
            "version": self.version,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServiceInstance:
        """Deserialize instance from dictionary."""
        return cls(
            service_name=data["service_name"],
            instance_id=data.get("instance_id", str(uuid.uuid4())),
            host=data.get("host", "localhost"),
            port=data.get("port", 8000),
            metadata=data.get("metadata", {}),
            status=ServiceStatus(data.get("status", "unknown")),
            weight=data.get("weight", 100),
            zone=data.get("zone", ""),
            version=data.get("version", ""),
            tags=data.get("tags", []),
        )


# =============================================================================
# Registry Configuration
# =============================================================================


@dataclass
class ServiceRegistryConfig:
    """
    Base configuration for service registries.

    Attributes:
        service_name: Name of this service for registration.
        instance_id: Unique ID for this instance.
        host: Host address to register.
        port: Port to register.
        health_check_interval: Seconds between health checks.
        health_check_timeout: Seconds to wait for health check.
        deregister_critical_timeout: Seconds before deregistering unhealthy instance.
        tags: Tags to register with the service.

    Environment Variables:
        PYMODULES_SERVICE_NAME
        PYMODULES_SERVICE_ID
        PYMODULES_SERVICE_HOST
        PYMODULES_SERVICE_PORT
        PYMODULES_HEALTH_CHECK_INTERVAL
        PYMODULES_HEALTH_CHECK_TIMEOUT
    """

    service_name: str = "pymodules-service"
    instance_id: str = field(default_factory=lambda: f"instance-{uuid.uuid4().hex[:8]}")
    host: str = ""
    port: int = 8000
    health_check_interval: float = 10.0
    health_check_timeout: float = 5.0
    deregister_critical_timeout: float = 60.0
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> ServiceRegistryConfig:
        """Load configuration from environment variables."""
        import os
        import socket

        # Auto-detect host if not specified
        host = os.getenv("PYMODULES_SERVICE_HOST", "")
        if not host:
            try:
                host = socket.gethostname()
            except Exception:
                host = "localhost"

        tags_str = os.getenv("PYMODULES_SERVICE_TAGS", "")
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]

        return cls(
            service_name=os.getenv("PYMODULES_SERVICE_NAME", "pymodules-service"),
            instance_id=os.getenv(
                "PYMODULES_SERVICE_ID", f"instance-{uuid.uuid4().hex[:8]}"
            ),
            host=host,
            port=int(os.getenv("PYMODULES_SERVICE_PORT", "8000")),
            health_check_interval=float(
                os.getenv("PYMODULES_HEALTH_CHECK_INTERVAL", "10.0")
            ),
            health_check_timeout=float(
                os.getenv("PYMODULES_HEALTH_CHECK_TIMEOUT", "5.0")
            ),
            tags=tags,
        )


# =============================================================================
# Abstract Registry Interface
# =============================================================================


class ServiceRegistry(ABC):
    """
    Abstract base class for service registries.

    Implementations must provide methods for:
    - Registering/deregistering this service
    - Discovering other services
    - Health status management

    Example Implementation:
        class MyRegistry(ServiceRegistry):
            async def register(self, instance: ServiceInstance) -> None:
                await self._client.register_service(instance.to_dict())

            async def discover(self, service_name: str) -> list[ServiceInstance]:
                data = await self._client.lookup(service_name)
                return [ServiceInstance.from_dict(d) for d in data]

            # ... other methods
    """

    def __init__(self, config: ServiceRegistryConfig | None = None):
        """
        Initialize the registry.

        Args:
            config: Registry configuration. Uses defaults if not provided.
        """
        self.config = config or ServiceRegistryConfig()
        self._registered = False

    @property
    def registered(self) -> bool:
        """Check if this service is registered."""
        return self._registered

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """
        Connect to the service registry.

        Raises:
            DiscoveryError: If connection fails.
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Disconnect from the service registry.

        Should deregister this service if registered.
        """
        pass

    async def __aenter__(self) -> ServiceRegistry:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: type | None, exc_val: Exception | None,
                        exc_tb: object) -> None:
        """Async context manager exit."""
        await self.disconnect()

    # -------------------------------------------------------------------------
    # Registration
    # -------------------------------------------------------------------------

    @abstractmethod
    async def register(self, instance: ServiceInstance | None = None) -> None:
        """
        Register a service instance.

        If instance is None, registers this service using config.

        Args:
            instance: Service instance to register.

        Raises:
            RegistrationError: If registration fails.
        """
        pass

    @abstractmethod
    async def deregister(self, instance_id: str | None = None) -> None:
        """
        Deregister a service instance.

        If instance_id is None, deregisters this service.

        Args:
            instance_id: ID of instance to deregister.
        """
        pass

    # -------------------------------------------------------------------------
    # Discovery
    # -------------------------------------------------------------------------

    @abstractmethod
    async def discover(
        self,
        service_name: str,
        tags: list[str] | None = None,
        healthy_only: bool = True,
    ) -> list[ServiceInstance]:
        """
        Discover instances of a service.

        Args:
            service_name: Name of service to discover.
            tags: Filter by tags.
            healthy_only: Only return healthy instances.

        Returns:
            List of service instances.

        Raises:
            ServiceNotFoundError: If no instances found.
        """
        pass

    async def discover_one(
        self,
        service_name: str,
        tags: list[str] | None = None,
    ) -> ServiceInstance:
        """
        Discover a single instance of a service (for load balancing).

        Default implementation returns first healthy instance.
        Subclasses may implement more sophisticated selection.

        Args:
            service_name: Name of service to discover.
            tags: Filter by tags.

        Returns:
            A single service instance.

        Raises:
            ServiceNotFoundError: If no instances found.
        """
        instances = await self.discover(service_name, tags, healthy_only=True)
        if not instances:
            raise ServiceNotFoundError(f"No healthy instances of {service_name}")
        return instances[0]

    # -------------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------------

    @abstractmethod
    async def update_health(
        self,
        status: ServiceStatus,
        instance_id: str | None = None,
    ) -> None:
        """
        Update health status of an instance.

        Args:
            status: New health status.
            instance_id: Instance to update (None = this instance).
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check registry health/connectivity.

        Returns:
            True if registry is accessible.
        """
        pass

    # -------------------------------------------------------------------------
    # Utility
    # -------------------------------------------------------------------------

    def create_instance(self) -> ServiceInstance:
        """Create a ServiceInstance from this registry's config."""
        return ServiceInstance(
            service_name=self.config.service_name,
            instance_id=self.config.instance_id,
            host=self.config.host,
            port=self.config.port,
            tags=self.config.tags,
            status=ServiceStatus.STARTING,
        )
