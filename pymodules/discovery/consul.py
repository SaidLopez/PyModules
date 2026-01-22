"""
Consul service discovery adapter.

Provides service registration and discovery using HashiCorp Consul.
Requires the 'python-consul' package (optional dependency).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..logging import get_logger
from .registry import (
    DiscoveryError,
    RegistrationError,
    ServiceInstance,
    ServiceNotFoundError,
    ServiceRegistry,
    ServiceRegistryConfig,
    ServiceStatus,
)

if TYPE_CHECKING:
    import consul.aio

logger = get_logger("discovery.consul")


# =============================================================================
# Consul Configuration
# =============================================================================


@dataclass
class ConsulRegistryConfig(ServiceRegistryConfig):
    """
    Consul-specific registry configuration.

    Attributes:
        consul_host: Consul agent hostname.
        consul_port: Consul agent port.
        consul_token: ACL token for authentication.
        consul_scheme: HTTP or HTTPS.
        health_check_http: HTTP endpoint for health checks.
        health_check_interval: Consul health check interval.
        deregister_critical_after: Deregister after being critical for this long.
        enable_tag_override: Allow tag override.

    Environment Variables:
        PYMODULES_CONSUL_HOST
        PYMODULES_CONSUL_PORT
        PYMODULES_CONSUL_TOKEN
        PYMODULES_CONSUL_SCHEME
        PYMODULES_HEALTH_CHECK_HTTP
    """

    consul_host: str = "localhost"
    consul_port: int = 8500
    consul_token: str = ""
    consul_scheme: str = "http"
    health_check_http: str = "/health"
    deregister_critical_after: str = "1m"
    enable_tag_override: bool = False

    @classmethod
    def from_env(cls) -> ConsulRegistryConfig:
        """Load configuration from environment variables."""
        import os

        base = ServiceRegistryConfig.from_env()

        # Parse consul URL if provided
        consul_url = os.getenv("PYMODULES_CONSUL_URL", "")
        consul_host = os.getenv("PYMODULES_CONSUL_HOST", "localhost")
        consul_port = int(os.getenv("PYMODULES_CONSUL_PORT", "8500"))
        consul_scheme = "http"

        if consul_url:
            from urllib.parse import urlparse
            parsed = urlparse(consul_url)
            consul_host = parsed.hostname or "localhost"
            consul_port = parsed.port or 8500
            consul_scheme = parsed.scheme or "http"

        return cls(
            # Base config
            service_name=base.service_name,
            instance_id=base.instance_id,
            host=base.host,
            port=base.port,
            health_check_interval=base.health_check_interval,
            health_check_timeout=base.health_check_timeout,
            deregister_critical_timeout=base.deregister_critical_timeout,
            tags=base.tags,
            # Consul-specific
            consul_host=consul_host,
            consul_port=consul_port,
            consul_token=os.getenv("PYMODULES_CONSUL_TOKEN", ""),
            consul_scheme=consul_scheme,
            health_check_http=os.getenv("PYMODULES_HEALTH_CHECK_HTTP", "/health"),
            deregister_critical_after=os.getenv(
                "PYMODULES_CONSUL_DEREGISTER_AFTER", "1m"
            ),
        )


# =============================================================================
# Consul Service Registry
# =============================================================================


class ConsulServiceRegistry(ServiceRegistry):
    """
    Consul-based service discovery.

    Features:
    - Service registration with health checks
    - Service discovery with tag filtering
    - Health status updates
    - Automatic deregistration on disconnect

    Example:
        config = ConsulRegistryConfig(
            consul_host="consul.service.consul",
            service_name="my-service",
            tags=["primary", "v2"]
        )

        async with ConsulServiceRegistry(config) as registry:
            # Register this service
            await registry.register()

            # Discover other services
            instances = await registry.discover("user-service")
            for inst in instances:
                response = await client.get(inst.url + "/api/users")

    Requires:
        pip install python-consul
        # or
        pip install pymodules[consul]
    """

    config: ConsulRegistryConfig = field(default_factory=ConsulRegistryConfig)

    def __init__(
        self,
        config: ConsulRegistryConfig | None = None,
        consul_host: str | None = None,
    ):
        """
        Initialize Consul registry.

        Args:
            config: Consul registry configuration.
            consul_host: Consul host (shortcut, overrides config.consul_host).
        """
        if config is None:
            config = ConsulRegistryConfig()
        if consul_host is not None:
            config.consul_host = consul_host

        super().__init__(config)
        self.config: ConsulRegistryConfig = config

        self._consul: consul.aio.Consul | None = None
        self._registered_ids: set[str] = set()

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to Consul agent."""
        try:
            import consul.aio
        except ImportError as e:
            raise DiscoveryError(
                "python-consul package not installed. "
                "Install with: pip install pymodules[consul]"
            ) from e

        try:
            self._consul = consul.aio.Consul(
                host=self.config.consul_host,
                port=self.config.consul_port,
                token=self.config.consul_token or None,
                scheme=self.config.consul_scheme,
            )

            # Verify connection
            await self._consul.status.leader()
            logger.info(
                "Connected to Consul at %s:%d",
                self.config.consul_host,
                self.config.consul_port,
            )

        except Exception as e:
            raise DiscoveryError(f"Failed to connect to Consul: {e}") from e

    async def disconnect(self) -> None:
        """Disconnect from Consul, deregistering all services."""
        # Deregister all registered services
        for instance_id in list(self._registered_ids):
            try:
                await self.deregister(instance_id)
            except Exception as e:
                logger.warning("Failed to deregister %s: %s", instance_id, e)

        self._consul = None
        self._registered_ids.clear()
        logger.info("Disconnected from Consul")

    # -------------------------------------------------------------------------
    # Registration
    # -------------------------------------------------------------------------

    async def register(self, instance: ServiceInstance | None = None) -> None:
        """
        Register a service instance with Consul.

        Creates service registration with HTTP health check.

        Args:
            instance: Service instance to register.
        """
        if not self._consul:
            raise RegistrationError("Not connected to Consul")

        if instance is None:
            instance = self.create_instance()

        # Build health check URL
        check_url = f"http://{instance.host}:{instance.port}{self.config.health_check_http}"

        # Register service
        try:
            success = await self._consul.agent.service.register(
                name=instance.service_name,
                service_id=instance.instance_id,
                address=instance.host,
                port=instance.port,
                tags=instance.tags,
                meta=instance.metadata,
                check={
                    "http": check_url,
                    "interval": f"{int(self.config.health_check_interval)}s",
                    "timeout": f"{int(self.config.health_check_timeout)}s",
                    "deregister_critical_service_after": self.config.deregister_critical_after,
                },
                enable_tag_override=self.config.enable_tag_override,
            )

            if success:
                self._registered_ids.add(instance.instance_id)
                self._registered = True
                logger.info(
                    "Registered service %s (%s) at %s",
                    instance.service_name,
                    instance.instance_id,
                    instance.address,
                )
            else:
                raise RegistrationError("Consul returned false for registration")

        except Exception as e:
            raise RegistrationError(f"Failed to register service: {e}") from e

    async def deregister(self, instance_id: str | None = None) -> None:
        """
        Deregister a service instance from Consul.

        Args:
            instance_id: ID of instance to deregister (None = this instance).
        """
        if not self._consul:
            return

        if instance_id is None:
            instance_id = self.config.instance_id

        try:
            await self._consul.agent.service.deregister(instance_id)
            self._registered_ids.discard(instance_id)

            if instance_id == self.config.instance_id:
                self._registered = False

            logger.info("Deregistered service %s", instance_id)

        except Exception as e:
            logger.warning("Failed to deregister %s: %s", instance_id, e)

    # -------------------------------------------------------------------------
    # Discovery
    # -------------------------------------------------------------------------

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
            tags: Filter by tags (all must match).
            healthy_only: Only return passing health checks.

        Returns:
            List of service instances.
        """
        if not self._consul:
            raise DiscoveryError("Not connected to Consul")

        try:
            # Use health endpoint for filtering
            index, services = await self._consul.health.service(
                service_name,
                tag=tags[0] if tags else None,  # Consul only supports single tag filter
                passing=healthy_only,
            )

            instances: list[ServiceInstance] = []

            for service_entry in services:
                service = service_entry["Service"]
                checks = service_entry["Checks"]

                # Additional tag filtering (Consul API only filters by one tag)
                if tags and len(tags) > 1:
                    service_tags = set(service.get("Tags", []))
                    if not all(tag in service_tags for tag in tags):
                        continue

                # Determine health status
                status = ServiceStatus.HEALTHY
                for check in checks:
                    if check["Status"] != "passing":
                        status = ServiceStatus.UNHEALTHY
                        break

                instance = ServiceInstance(
                    service_name=service["Service"],
                    instance_id=service["ID"],
                    host=service["Address"] or service_entry["Node"]["Address"],
                    port=service["Port"],
                    tags=service.get("Tags", []),
                    metadata=service.get("Meta", {}),
                    status=status,
                    weight=service.get("Weights", {}).get("Passing", 100),
                )
                instances.append(instance)

            if not instances:
                raise ServiceNotFoundError(f"No instances of {service_name}")

            logger.debug("Discovered %d instances of %s", len(instances), service_name)
            return instances

        except ServiceNotFoundError:
            raise
        except Exception as e:
            raise DiscoveryError(f"Failed to discover {service_name}: {e}") from e

    async def discover_one(
        self,
        service_name: str,
        tags: list[str] | None = None,
    ) -> ServiceInstance:
        """
        Discover a single instance with weighted selection.

        Uses weight-based random selection for load balancing.
        """
        instances = await self.discover(service_name, tags, healthy_only=True)
        if not instances:
            raise ServiceNotFoundError(f"No healthy instances of {service_name}")

        # Weight-based selection
        import random
        total_weight = sum(i.weight for i in instances)
        if total_weight == 0:
            return random.choice(instances)

        r = random.randint(0, total_weight - 1)
        cumulative = 0
        for instance in instances:
            cumulative += instance.weight
            if r < cumulative:
                return instance

        return instances[-1]

    # -------------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------------

    async def update_health(
        self,
        status: ServiceStatus,
        instance_id: str | None = None,
    ) -> None:
        """
        Update health status of an instance.

        Note: Consul uses health checks, not direct status updates.
        This method updates the TTL check if configured.
        """
        if not self._consul:
            return

        if instance_id is None:
            instance_id = self.config.instance_id

        check_id = f"service:{instance_id}"

        try:
            if status == ServiceStatus.HEALTHY:
                await self._consul.agent.check.ttl_pass(check_id)
            elif status == ServiceStatus.UNHEALTHY:
                await self._consul.agent.check.ttl_fail(check_id)
            else:
                await self._consul.agent.check.ttl_warn(check_id)

            logger.debug("Updated health check %s to %s", check_id, status.value)

        except Exception as e:
            logger.debug("Could not update TTL check (may not be TTL type): %s", e)

    async def health_check(self) -> bool:
        """Check Consul connectivity."""
        if not self._consul:
            return False

        try:
            leader = await self._consul.status.leader()
            return bool(leader)
        except Exception:
            return False

    # -------------------------------------------------------------------------
    # Additional Consul Features
    # -------------------------------------------------------------------------

    async def get_service_tags(self, service_name: str) -> set[str]:
        """Get all tags for a service across instances."""
        if not self._consul:
            return set()

        try:
            index, services = await self._consul.catalog.service(service_name)
            tags: set[str] = set()
            for service in services:
                tags.update(service.get("ServiceTags", []))
            return tags
        except Exception:
            return set()

    async def watch_service(
        self,
        service_name: str,
        callback: Any,
        interval: float = 5.0,
    ) -> asyncio.Task[None]:
        """
        Watch a service for changes.

        Args:
            service_name: Service to watch.
            callback: Async function called with list[ServiceInstance] on changes.
            interval: Polling interval in seconds.

        Returns:
            Background task for the watcher.
        """
        async def _watcher() -> None:
            last_index = 0
            while True:
                try:
                    if not self._consul:
                        break

                    index, services = await self._consul.health.service(
                        service_name,
                        passing=True,
                        index=last_index,
                        wait="30s",
                    )

                    if index != last_index:
                        last_index = index
                        instances = await self.discover(service_name)
                        await callback(instances)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("Watch error: %s", e)
                    await asyncio.sleep(interval)

        task = asyncio.create_task(_watcher())
        return task

    async def set_key_value(self, key: str, value: str) -> bool:
        """Set a key-value pair in Consul KV store."""
        if not self._consul:
            return False

        try:
            result = await self._consul.kv.put(key, value)
            return bool(result)
        except Exception as e:
            logger.error("Failed to set KV %s: %s", key, e)
            return False

    async def get_key_value(self, key: str) -> str | None:
        """Get a value from Consul KV store."""
        if not self._consul:
            return None

        try:
            index, data = await self._consul.kv.get(key)
            if data:
                value = data["Value"]
                if isinstance(value, bytes):
                    return value.decode()
                return str(value)
            return None
        except Exception:
            return None
