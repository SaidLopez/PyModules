"""
PyModules Service Discovery Package.

Provides service discovery integration for microservices deployments.
Supports Kubernetes DNS-based discovery (zero deps) and Consul (optional).

Example:
    from pymodules.discovery import DNSServiceRegistry, ServiceInstance

    # Kubernetes DNS-based discovery
    registry = DNSServiceRegistry(
        namespace="default",
        service_suffix="svc.cluster.local"
    )

    # Discover services
    instances = await registry.discover("user-service")
    for instance in instances:
        print(f"Found: {instance.host}:{instance.port}")

    # Register this service
    await registry.register(ServiceInstance(
        service_name="my-service",
        instance_id="pod-123",
        host="10.0.0.5",
        port=8000
    ))
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Lazy imports for optional dependencies
__all__ = [
    # Core types
    "ServiceInstance",
    "ServiceRegistry",
    "ServiceRegistryConfig",
    # DNS discovery (zero deps)
    "DNSServiceRegistry",
    "DNSRegistryConfig",
    # Consul adapter (optional)
    "ConsulServiceRegistry",
    "ConsulRegistryConfig",
    # Exceptions
    "DiscoveryError",
    "RegistrationError",
    "ServiceNotFoundError",
]


def __getattr__(name: str) -> object:
    """Lazy import discovery components."""
    if name in (
        "ServiceInstance",
        "ServiceRegistry",
        "ServiceRegistryConfig",
        "DiscoveryError",
        "RegistrationError",
        "ServiceNotFoundError",
    ):
        from .registry import (
            DiscoveryError,
            RegistrationError,
            ServiceInstance,
            ServiceNotFoundError,
            ServiceRegistry,
            ServiceRegistryConfig,
        )

        return locals()[name]

    if name in ("DNSServiceRegistry", "DNSRegistryConfig"):
        from .dns import DNSRegistryConfig, DNSServiceRegistry

        return locals()[name]

    if name in ("ConsulServiceRegistry", "ConsulRegistryConfig"):
        from .consul import ConsulRegistryConfig, ConsulServiceRegistry

        return locals()[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if TYPE_CHECKING:
    from .consul import ConsulRegistryConfig, ConsulServiceRegistry
    from .dns import DNSRegistryConfig, DNSServiceRegistry
    from .registry import (
        DiscoveryError,
        RegistrationError,
        ServiceInstance,
        ServiceNotFoundError,
        ServiceRegistry,
        ServiceRegistryConfig,
    )
