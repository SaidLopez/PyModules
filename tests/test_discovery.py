"""
Tests for service discovery: registry, DNS, Consul adapter.
"""

from unittest.mock import patch

import pytest

from pymodules.discovery.dns import DNSRegistryConfig, DNSServiceRegistry
from pymodules.discovery.registry import (
    DiscoveryError,
    RegistrationError,
    ServiceInstance,
    ServiceNotFoundError,
    ServiceRegistryConfig,
    ServiceStatus,
)

# =============================================================================
# ServiceInstance Tests
# =============================================================================


class TestServiceInstance:
    """Tests for ServiceInstance class."""

    def test_instance_creation(self) -> None:
        """ServiceInstance can be created with required fields."""
        instance = ServiceInstance(
            service_name="user-service",
            host="10.0.0.5",
            port=8080,
        )

        assert instance.service_name == "user-service"
        assert instance.host == "10.0.0.5"
        assert instance.port == 8080
        assert instance.instance_id  # Auto-generated

    def test_instance_address(self) -> None:
        """ServiceInstance provides address property."""
        instance = ServiceInstance(
            service_name="api",
            host="192.168.1.100",
            port=3000,
        )

        assert instance.address == "192.168.1.100:3000"
        assert instance.url == "http://192.168.1.100:3000"

    def test_instance_health_status(self) -> None:
        """ServiceInstance tracks health status."""
        instance = ServiceInstance(service_name="test")

        assert instance.status == ServiceStatus.UNKNOWN
        assert not instance.is_healthy()

        instance.status = ServiceStatus.HEALTHY
        assert instance.is_healthy()

        instance.status = ServiceStatus.UNHEALTHY
        assert not instance.is_healthy()

    def test_instance_serialization(self) -> None:
        """ServiceInstance can be serialized to dict."""
        instance = ServiceInstance(
            service_name="test-service",
            instance_id="inst-123",
            host="localhost",
            port=8000,
            metadata={"region": "us-west"},
            status=ServiceStatus.HEALTHY,
            weight=50,
            zone="zone-a",
            version="1.0.0",
            tags=["primary", "v1"],
        )

        d = instance.to_dict()

        assert d["service_name"] == "test-service"
        assert d["instance_id"] == "inst-123"
        assert d["host"] == "localhost"
        assert d["port"] == 8000
        assert d["metadata"] == {"region": "us-west"}
        assert d["status"] == "healthy"
        assert d["weight"] == 50
        assert d["zone"] == "zone-a"
        assert d["version"] == "1.0.0"
        assert d["tags"] == ["primary", "v1"]

    def test_instance_deserialization(self) -> None:
        """ServiceInstance can be deserialized from dict."""
        data = {
            "service_name": "test-service",
            "instance_id": "inst-456",
            "host": "10.0.0.1",
            "port": 9000,
            "metadata": {"env": "prod"},
            "status": "healthy",
            "weight": 100,
            "zone": "zone-b",
            "version": "2.0.0",
            "tags": ["secondary"],
        }

        instance = ServiceInstance.from_dict(data)

        assert instance.service_name == "test-service"
        assert instance.instance_id == "inst-456"
        assert instance.host == "10.0.0.1"
        assert instance.port == 9000
        assert instance.status == ServiceStatus.HEALTHY
        assert instance.tags == ["secondary"]


# =============================================================================
# ServiceRegistryConfig Tests
# =============================================================================


class TestServiceRegistryConfig:
    """Tests for ServiceRegistryConfig."""

    def test_default_config(self) -> None:
        """Default configuration has sensible values."""
        config = ServiceRegistryConfig()

        assert config.service_name == "pymodules-service"
        assert config.instance_id.startswith("instance-")
        assert config.port == 8000
        assert config.health_check_interval == 10.0

    def test_config_from_env(self, monkeypatch) -> None:
        """Configuration can be loaded from environment."""
        monkeypatch.setenv("PYMODULES_SERVICE_NAME", "my-api")
        monkeypatch.setenv("PYMODULES_SERVICE_ID", "api-instance-1")
        monkeypatch.setenv("PYMODULES_SERVICE_HOST", "10.0.0.5")
        monkeypatch.setenv("PYMODULES_SERVICE_PORT", "9000")
        monkeypatch.setenv("PYMODULES_SERVICE_TAGS", "primary,v2,production")
        monkeypatch.setenv("PYMODULES_HEALTH_CHECK_INTERVAL", "15")

        config = ServiceRegistryConfig.from_env()

        assert config.service_name == "my-api"
        assert config.instance_id == "api-instance-1"
        assert config.host == "10.0.0.5"
        assert config.port == 9000
        assert config.tags == ["primary", "v2", "production"]
        assert config.health_check_interval == 15.0


# =============================================================================
# DNS Registry Config Tests
# =============================================================================


class TestDNSRegistryConfig:
    """Tests for DNSRegistryConfig."""

    def test_default_dns_config(self) -> None:
        """Default DNS configuration has K8s defaults."""
        config = DNSRegistryConfig()

        assert config.namespace == "default"
        assert config.service_suffix == "svc.cluster.local"
        assert config.resolve_timeout == 5.0
        assert config.cache_ttl == 30.0

    def test_dns_config_from_env(self, monkeypatch) -> None:
        """DNS config can be loaded from environment."""
        monkeypatch.setenv("PYMODULES_K8S_NAMESPACE", "production")
        monkeypatch.setenv("PYMODULES_DNS_SUFFIX", "svc.local")
        monkeypatch.setenv("PYMODULES_DNS_TIMEOUT", "10")
        monkeypatch.setenv("PYMODULES_DNS_CACHE_TTL", "60")

        config = DNSRegistryConfig.from_env()

        assert config.namespace == "production"
        assert config.service_suffix == "svc.local"
        assert config.resolve_timeout == 10.0
        assert config.cache_ttl == 60.0


# =============================================================================
# DNS Service Registry Tests
# =============================================================================


class TestDNSServiceRegistry:
    """Tests for DNSServiceRegistry."""

    @pytest.mark.asyncio
    async def test_registry_creation(self) -> None:
        """DNS registry can be created."""
        config = DNSRegistryConfig(namespace="test")
        registry = DNSServiceRegistry(config)

        assert registry.config.namespace == "test"
        assert not registry.registered

    @pytest.mark.asyncio
    async def test_registry_connect(self) -> None:
        """DNS registry can connect (verifies DNS access)."""
        registry = DNSServiceRegistry()

        await registry.connect()
        # Connection should succeed (localhost always resolves)

        await registry.disconnect()

    @pytest.mark.asyncio
    async def test_registry_context_manager(self) -> None:
        """DNS registry works as async context manager."""
        async with DNSServiceRegistry() as registry:
            assert isinstance(registry, DNSServiceRegistry)

    @pytest.mark.asyncio
    async def test_local_registration(self) -> None:
        """DNS registry stores local instance on register."""
        registry = DNSServiceRegistry(
            DNSRegistryConfig(
                service_name="my-service",
                host="10.0.0.5",
                port=8080,
            )
        )

        await registry.register()

        assert registry.registered
        assert registry._local_instance is not None
        assert registry._local_instance.service_name == "my-service"
        assert registry._local_instance.host == "10.0.0.5"

    @pytest.mark.asyncio
    async def test_deregistration(self) -> None:
        """DNS registry clears local instance on deregister."""
        registry = DNSServiceRegistry()
        await registry.register()
        assert registry.registered

        await registry.deregister()
        assert not registry.registered
        assert registry._local_instance is None

    @pytest.mark.asyncio
    async def test_dns_name_building(self) -> None:
        """DNS registry builds correct DNS names."""
        registry = DNSServiceRegistry(
            DNSRegistryConfig(
                namespace="production",
                service_suffix="svc.cluster.local",
            )
        )

        # Simple service name
        name = registry._build_dns_name("user-service")
        assert name == "user-service.production.svc.cluster.local"

        # Already has namespace
        name = registry._build_dns_name("api.staging")
        assert name == "api.staging.svc.cluster.local"

        # Already fully qualified
        name = registry._build_dns_name("db.data.svc.cluster.local")
        assert name == "db.data.svc.cluster.local"

    @pytest.mark.asyncio
    async def test_discover_localhost(self) -> None:
        """DNS registry can discover localhost (basic DNS test)."""
        registry = DNSServiceRegistry(
            DNSRegistryConfig(
                namespace="local",
                service_suffix="",
            )
        )

        # Mock the internal resolver to return localhost
        with patch.object(registry, "_resolve_dns", return_value=["127.0.0.1"]):
            instances = await registry.discover("localhost")

            assert len(instances) >= 1
            assert instances[0].host == "127.0.0.1"

    @pytest.mark.asyncio
    async def test_caching(self) -> None:
        """DNS registry caches results."""
        registry = DNSServiceRegistry(
            DNSRegistryConfig(
                cache_ttl=60.0,
            )
        )

        # Mock resolver
        resolve_count = 0

        def mock_resolve(dns_name: str) -> list[str]:
            nonlocal resolve_count
            resolve_count += 1
            return ["10.0.0.1", "10.0.0.2"]

        with patch.object(registry, "_resolve_dns", side_effect=mock_resolve):
            # First call - should resolve
            await registry.discover("test-service")
            assert resolve_count == 1

            # Second call - should use cache
            await registry.discover("test-service")
            assert resolve_count == 1  # No additional resolution

            # Clear cache
            registry.clear_cache()

            # Third call - should resolve again
            await registry.discover("test-service")
            assert resolve_count == 2

    @pytest.mark.asyncio
    async def test_health_check(self) -> None:
        """DNS registry health check verifies DNS accessibility."""
        registry = DNSServiceRegistry()

        healthy = await registry.health_check()
        assert healthy  # localhost should always be resolvable

    @pytest.mark.asyncio
    async def test_update_health(self) -> None:
        """DNS registry updates local health status."""
        registry = DNSServiceRegistry()
        await registry.register()

        assert registry._local_instance.status == ServiceStatus.STARTING

        await registry.update_health(ServiceStatus.HEALTHY)
        assert registry._local_instance.status == ServiceStatus.HEALTHY

        await registry.update_health(ServiceStatus.UNHEALTHY)
        assert registry._local_instance.status == ServiceStatus.UNHEALTHY

    @pytest.mark.asyncio
    async def test_create_instance_from_config(self) -> None:
        """DNS registry creates instance from config."""
        registry = DNSServiceRegistry(
            DNSRegistryConfig(
                service_name="test-api",
                host="192.168.1.10",
                port=3000,
                tags=["prod", "v1"],
            )
        )

        instance = registry.create_instance()

        assert instance.service_name == "test-api"
        assert instance.host == "192.168.1.10"
        assert instance.port == 3000
        assert instance.tags == ["prod", "v1"]


# =============================================================================
# ServiceStatus Tests
# =============================================================================


class TestServiceStatus:
    """Tests for ServiceStatus enum."""

    def test_status_values(self) -> None:
        """ServiceStatus has expected values."""
        assert ServiceStatus.HEALTHY.value == "healthy"
        assert ServiceStatus.UNHEALTHY.value == "unhealthy"
        assert ServiceStatus.UNKNOWN.value == "unknown"
        assert ServiceStatus.STARTING.value == "starting"
        assert ServiceStatus.STOPPING.value == "stopping"


# =============================================================================
# Exception Tests
# =============================================================================


class TestDiscoveryExceptions:
    """Tests for discovery exceptions."""

    def test_discovery_error(self) -> None:
        """DiscoveryError is base exception."""
        error = DiscoveryError("Something went wrong")
        assert str(error) == "Something went wrong"
        assert isinstance(error, Exception)

    def test_registration_error(self) -> None:
        """RegistrationError for registration failures."""
        error = RegistrationError("Failed to register")
        assert isinstance(error, DiscoveryError)

    def test_service_not_found_error(self) -> None:
        """ServiceNotFoundError for missing services."""
        error = ServiceNotFoundError("Service not found")
        assert isinstance(error, DiscoveryError)


# =============================================================================
# Integration Tests
# =============================================================================


class TestDiscoveryIntegration:
    """Integration tests for discovery components."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self) -> None:
        """Test full registry lifecycle."""
        config = DNSRegistryConfig(
            service_name="integration-test",
            host="10.0.0.50",
            port=8080,
            tags=["test"],
        )

        async with DNSServiceRegistry(config) as registry:
            # Register
            await registry.register()
            assert registry.registered

            # Update health
            await registry.update_health(ServiceStatus.HEALTHY)
            assert registry._local_instance.is_healthy()

            # Verify health check
            assert await registry.health_check()

        # After context exit
        assert not registry.registered

    @pytest.mark.asyncio
    async def test_discover_one_round_robin(self) -> None:
        """discover_one uses round-robin selection."""
        registry = DNSServiceRegistry()

        # Mock resolver to return multiple IPs
        def mock_resolve(dns_name: str) -> list[str]:
            return ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

        with patch.object(registry, "_resolve_dns", side_effect=mock_resolve):
            # Clear cache between calls to force selection logic
            registry.clear_cache()

            # Get multiple instances - should vary over time
            instances = set()
            for _ in range(10):
                registry.clear_cache()
                instance = await registry.discover_one("test-service")
                instances.add(instance.host)

            # Should have selected different hosts
            assert len(instances) > 1

    @pytest.mark.asyncio
    async def test_empty_discovery_raises(self) -> None:
        """discover raises when no instances found."""
        registry = DNSServiceRegistry()

        # Mock resolver to return empty list
        with patch.object(registry, "_resolve_dns", return_value=[]):
            with pytest.raises(ServiceNotFoundError):
                await registry.discover("nonexistent-service")
