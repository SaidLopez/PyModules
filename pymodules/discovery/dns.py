"""
Kubernetes DNS-based service discovery.

Zero external dependencies - uses Python's built-in socket module.
Works with Kubernetes headless services and standard DNS.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field

from ..logging import get_logger
from .registry import (
    DiscoveryError,
    ServiceInstance,
    ServiceNotFoundError,
    ServiceRegistry,
    ServiceRegistryConfig,
    ServiceStatus,
)

logger = get_logger("discovery.dns")


# =============================================================================
# DNS Registry Configuration
# =============================================================================


@dataclass
class DNSRegistryConfig(ServiceRegistryConfig):
    """
    DNS-based service discovery configuration.

    Attributes:
        namespace: Kubernetes namespace.
        service_suffix: DNS suffix for services.
        resolve_timeout: Timeout for DNS resolution.
        cache_ttl: How long to cache DNS results.
        use_srv_records: Use SRV records for port discovery.

    Environment Variables:
        PYMODULES_K8S_NAMESPACE
        PYMODULES_DNS_SUFFIX
        PYMODULES_DNS_TIMEOUT
        PYMODULES_DNS_CACHE_TTL
    """

    namespace: str = "default"
    service_suffix: str = "svc.cluster.local"
    resolve_timeout: float = 5.0
    cache_ttl: float = 30.0
    use_srv_records: bool = False

    @classmethod
    def from_env(cls) -> DNSRegistryConfig:
        """Load configuration from environment variables."""
        import os

        base = ServiceRegistryConfig.from_env()

        # Try to detect namespace from K8s service account
        namespace = os.getenv("PYMODULES_K8S_NAMESPACE", "")
        if not namespace:
            try:
                with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
                    namespace = f.read().strip()
            except Exception:
                namespace = "default"

        return cls(
            # Base config
            service_name=base.service_name,
            instance_id=base.instance_id,
            host=base.host,
            port=base.port,
            health_check_interval=base.health_check_interval,
            health_check_timeout=base.health_check_timeout,
            tags=base.tags,
            # DNS-specific
            namespace=namespace,
            service_suffix=os.getenv("PYMODULES_DNS_SUFFIX", "svc.cluster.local"),
            resolve_timeout=float(os.getenv("PYMODULES_DNS_TIMEOUT", "5.0")),
            cache_ttl=float(os.getenv("PYMODULES_DNS_CACHE_TTL", "30.0")),
        )


# =============================================================================
# DNS Cache Entry
# =============================================================================


@dataclass
class CacheEntry:
    """Cached DNS lookup result."""

    instances: list[ServiceInstance]
    timestamp: float
    ttl: float

    def is_valid(self, current_time: float) -> bool:
        """Check if cache entry is still valid."""
        return (current_time - self.timestamp) < self.ttl


# =============================================================================
# DNS Service Registry
# =============================================================================


class DNSServiceRegistry(ServiceRegistry):
    """
    Kubernetes DNS-based service discovery.

    Uses DNS to discover services - no external dependencies required.
    Works with Kubernetes headless services for pod discovery.

    DNS Patterns:
    - Standard service: <service>.<namespace>.svc.cluster.local
    - Headless service: <pod>.<service>.<namespace>.svc.cluster.local

    Example:
        config = DNSRegistryConfig(
            namespace="production",
            service_name="my-service"
        )
        registry = DNSServiceRegistry(config)

        # Discover services
        instances = await registry.discover("user-service")
        for inst in instances:
            print(f"Found: {inst.host}:{inst.port}")

    Note: Registration is a no-op with DNS discovery since Kubernetes
    handles service registration automatically through pod endpoints.
    """

    config: DNSRegistryConfig = field(default_factory=DNSRegistryConfig)

    def __init__(
        self,
        config: DNSRegistryConfig | None = None,
        namespace: str | None = None,
    ):
        """
        Initialize DNS registry.

        Args:
            config: DNS registry configuration.
            namespace: Kubernetes namespace (shortcut, overrides config.namespace).
        """
        if config is None:
            config = DNSRegistryConfig()
        if namespace is not None:
            config.namespace = namespace

        super().__init__(config)
        self.config: DNSRegistryConfig = config

        self._cache: dict[str, CacheEntry] = {}
        self._local_instance: ServiceInstance | None = None

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect (verify DNS is accessible)."""
        try:
            # Test DNS resolution
            socket.gethostbyname("localhost")
            logger.info(
                "DNS registry ready (namespace: %s, suffix: %s)",
                self.config.namespace,
                self.config.service_suffix,
            )
        except Exception as e:
            raise DiscoveryError(f"DNS not accessible: {e}") from e

    async def disconnect(self) -> None:
        """Disconnect (clear cache)."""
        self._cache.clear()
        self._local_instance = None
        self._registered = False
        logger.info("DNS registry disconnected")

    # -------------------------------------------------------------------------
    # Registration (K8s handles this via endpoints)
    # -------------------------------------------------------------------------

    async def register(self, instance: ServiceInstance | None = None) -> None:
        """
        Register a service instance.

        Note: In Kubernetes, registration happens automatically when pods
        are created. This method stores the instance locally for reference
        but doesn't perform actual registration.

        Args:
            instance: Service instance to register.
        """
        if instance is None:
            instance = self.create_instance()

        self._local_instance = instance
        self._registered = True

        logger.info(
            "Service registered locally (K8s handles actual registration): %s",
            instance.address,
        )

    async def deregister(self, instance_id: str | None = None) -> None:
        """
        Deregister a service instance.

        Note: In Kubernetes, deregistration happens automatically when pods
        are terminated.
        """
        self._local_instance = None
        self._registered = False
        logger.info("Service deregistered locally")

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
        Discover instances of a service using DNS.

        Args:
            service_name: Name of service to discover.
            tags: Not supported in DNS discovery (ignored).
            healthy_only: If True, only returns instances that respond to DNS.

        Returns:
            List of service instances.
        """
        # Check cache
        cache_key = f"{service_name}:{self.config.namespace}"
        current_time = asyncio.get_event_loop().time()

        if cache_key in self._cache:
            entry = self._cache[cache_key]
            if entry.is_valid(current_time):
                logger.debug("Cache hit for %s", service_name)
                return entry.instances

        # Build DNS name
        dns_name = self._build_dns_name(service_name)
        logger.debug("Resolving DNS: %s", dns_name)

        try:
            # Resolve A records
            instances = await self._resolve_service(dns_name, service_name)

            # Cache results
            self._cache[cache_key] = CacheEntry(
                instances=instances,
                timestamp=current_time,
                ttl=self.config.cache_ttl,
            )

            if not instances:
                raise ServiceNotFoundError(f"No instances found for {service_name}")

            return instances

        except ServiceNotFoundError:
            raise
        except Exception as e:
            logger.error("DNS resolution failed for %s: %s", service_name, e)
            raise DiscoveryError(f"Failed to discover {service_name}: {e}") from e

    async def _resolve_service(self, dns_name: str, service_name: str) -> list[ServiceInstance]:
        """Resolve service DNS to instances."""
        instances: list[ServiceInstance] = []

        try:
            # Run DNS resolution in thread pool
            loop = asyncio.get_event_loop()
            addresses = await asyncio.wait_for(
                loop.run_in_executor(None, self._resolve_dns, dns_name),
                timeout=self.config.resolve_timeout,
            )

            for i, addr in enumerate(addresses):
                instance = ServiceInstance(
                    service_name=service_name,
                    instance_id=f"{service_name}-{i}",
                    host=addr,
                    port=self.config.port,  # Default port, SRV would provide this
                    status=ServiceStatus.HEALTHY,
                )
                instances.append(instance)

            logger.debug("Resolved %d instances for %s", len(instances), service_name)

        except asyncio.TimeoutError:
            logger.warning("DNS resolution timeout for %s", dns_name)
        except socket.gaierror as e:
            # No records found
            logger.debug("No DNS records for %s: %s", dns_name, e)

        return instances

    def _resolve_dns(self, dns_name: str) -> list[str]:
        """Synchronous DNS resolution (runs in thread pool)."""
        try:
            # Get all A records
            results = socket.getaddrinfo(
                dns_name,
                None,
                socket.AF_INET,
                socket.SOCK_STREAM,
            )
            # Extract unique IPs (result[4][0] is always str for IP address)
            addresses: list[str] = list({str(result[4][0]) for result in results})
            return addresses
        except socket.gaierror:
            return []

    def _build_dns_name(self, service_name: str) -> str:
        """Build full DNS name for a service."""
        # Handle already-qualified names
        if self.config.service_suffix in service_name:
            return service_name

        # Handle namespace prefix
        if "." in service_name:
            return f"{service_name}.{self.config.service_suffix}"

        # Build full name: service.namespace.svc.cluster.local
        return f"{service_name}.{self.config.namespace}.{self.config.service_suffix}"

    async def discover_one(
        self,
        service_name: str,
        tags: list[str] | None = None,
    ) -> ServiceInstance:
        """
        Discover a single instance using round-robin selection.

        Uses simple counter-based round-robin for load balancing.
        """
        instances = await self.discover(service_name, tags)
        if not instances:
            raise ServiceNotFoundError(f"No instances of {service_name}")

        # Simple round-robin using hash of service name + time
        import time

        index = int(time.time() * 1000) % len(instances)
        return instances[index]

    # -------------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------------

    async def update_health(
        self,
        status: ServiceStatus,
        instance_id: str | None = None,
    ) -> None:
        """
        Update health status.

        Note: Health status in Kubernetes is managed by readiness/liveness
        probes, not directly by the application. This updates local state only.
        """
        if self._local_instance:
            self._local_instance.status = status
            logger.debug("Local health status updated to %s", status.value)

    async def health_check(self) -> bool:
        """Check if DNS is accessible."""
        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, socket.gethostbyname, "localhost"),
                timeout=self.config.resolve_timeout,
            )
            return True
        except Exception:
            return False

    # -------------------------------------------------------------------------
    # Utility
    # -------------------------------------------------------------------------

    def clear_cache(self) -> None:
        """Clear the DNS cache."""
        self._cache.clear()
        logger.debug("DNS cache cleared")

    async def resolve_pod(
        self,
        pod_name: str,
        service_name: str,
    ) -> ServiceInstance | None:
        """
        Resolve a specific pod by name (headless service).

        Args:
            pod_name: Name of the pod.
            service_name: Name of the headless service.

        Returns:
            ServiceInstance for the pod, or None if not found.
        """
        # Headless service DNS: pod.service.namespace.svc.cluster.local
        dns_name = f"{pod_name}.{self._build_dns_name(service_name)}"

        try:
            loop = asyncio.get_event_loop()
            addresses = await asyncio.wait_for(
                loop.run_in_executor(None, self._resolve_dns, dns_name),
                timeout=self.config.resolve_timeout,
            )

            if addresses:
                return ServiceInstance(
                    service_name=service_name,
                    instance_id=pod_name,
                    host=addresses[0],
                    port=self.config.port,
                    status=ServiceStatus.HEALTHY,
                )
        except Exception as e:
            logger.debug("Failed to resolve pod %s: %s", pod_name, e)

        return None
