"""Tests for ``pymodules.contrib.fullstack.asyncapi.emit_asyncapi``.

These tests cover the observable shape of the AsyncAPI document produced
from a synthetic ``ModuleHost`` with declared Events. They do not look
inside the emitter's traversal — just at the dict it returns.

Style note: dataclass Events and Modules are defined inline in this file
(matching ``tests/test_eventbus.py``) rather than imported from a shared
fixture module, so each test reads top-to-bottom.

Schema-shape pin (documented here so the test reads honestly):
    Nested dataclass fields are emitted **inline** as full
    ``{"type": "object", "properties": {...}, "required": [...]}`` blocks
    rather than as ``$ref`` pointers. This is intentional for v1 to keep
    codegen consumers simple — they can walk the payload without a JSON-
    schema $ref resolver. The test below asserts on this inline shape
    directly; if we later switch to ``$ref``, this test should be updated
    to follow.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pymodules import Event, Module, ModuleHost
from pymodules.contrib.fullstack import emit_asyncapi

# ---------------------------------------------------------------------------
# Synthetic Events used across the test cases
# ---------------------------------------------------------------------------


@dataclass
class Address:
    """Nested dataclass used as a field on ``UserCreated``."""

    street: str = ""
    city: str = ""
    postcode: str | None = None


@dataclass
class UserCreated(Event):
    user_id: str = ""
    email: str = ""
    address: Address = field(default_factory=Address)
    name: str = "user.created"


@dataclass
class PremiumUserCreated(UserCreated):
    """Subclass that adds a field — must produce a distinct schema."""

    plan: str = "premium"


@dataclass
class OrderPlaced(Event):
    order_id: str = ""
    total_cents: int = 0
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    name: str = "order.placed"


# ---------------------------------------------------------------------------
# Synthetic Modules — declare ``published_events`` so the emitter sees them
# ---------------------------------------------------------------------------


class UserModule(Module):
    published_events = (UserCreated, PremiumUserCreated)


class OrderModule(Module):
    published_events = (OrderPlaced,)


class SilentModule(Module):
    """A Module that publishes nothing — must contribute zero channels."""

    # Inherits ``published_events = ()`` default from ``Module``.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_host() -> ModuleHost:
    host = ModuleHost()
    host.register(UserModule())
    host.register(OrderModule())
    host.register(SilentModule())
    return host


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTopLevelShape:
    """The document must have the AsyncAPI 3.0 envelope."""

    def test_asyncapi_version_is_3_0(self):
        host = _build_host()
        doc = emit_asyncapi(host)
        try:
            assert doc["asyncapi"].startswith("3.0")
        finally:
            host.shutdown()

    def test_info_block_present(self):
        host = _build_host()
        try:
            doc = emit_asyncapi(host, title="My Host", version="2.3.4")
            assert doc["info"]["title"] == "My Host"
            assert doc["info"]["version"] == "2.3.4"
        finally:
            host.shutdown()

    def test_top_level_keys_present(self):
        host = _build_host()
        try:
            doc = emit_asyncapi(host)
            assert "channels" in doc
            assert "operations" in doc
            assert "components" in doc
            assert "messages" in doc["components"]
            assert "schemas" in doc["components"]
        finally:
            host.shutdown()


class TestChannelsAndMessages:
    """Two Modules / three Events produce three channels and three messages."""

    def test_one_channel_per_declared_event(self):
        host = _build_host()
        try:
            doc = emit_asyncapi(host)
            channels = doc["channels"]
            assert set(channels.keys()) == {
                "UserCreated",
                "PremiumUserCreated",
                "OrderPlaced",
            }
        finally:
            host.shutdown()

    def test_channel_address_matches_key(self):
        host = _build_host()
        try:
            doc = emit_asyncapi(host)
            for name, channel in doc["channels"].items():
                assert channel["address"] == name
        finally:
            host.shutdown()

    def test_one_message_per_declared_event(self):
        host = _build_host()
        try:
            doc = emit_asyncapi(host)
            messages = doc["components"]["messages"]
            assert set(messages.keys()) == {
                "UserCreated",
                "PremiumUserCreated",
                "OrderPlaced",
            }
        finally:
            host.shutdown()

    def test_each_channel_references_its_message(self):
        host = _build_host()
        try:
            doc = emit_asyncapi(host)
            for name, channel in doc["channels"].items():
                refs = channel["messages"]
                assert name in refs
                assert refs[name] == {"$ref": f"#/components/messages/{name}"}
        finally:
            host.shutdown()

    def test_each_message_payload_points_at_schema(self):
        host = _build_host()
        try:
            doc = emit_asyncapi(host)
            for name, message in doc["components"]["messages"].items():
                assert message["payload"] == {"$ref": f"#/components/schemas/{name}"}
        finally:
            host.shutdown()


class TestSchemas:
    """Per-Event JSON schemas are derived from dataclass fields."""

    def test_primitive_fields_typed_correctly(self):
        host = _build_host()
        try:
            doc = emit_asyncapi(host)
            schema = doc["components"]["schemas"]["OrderPlaced"]
            props = schema["properties"]
            assert props["order_id"] == {"type": "string"}
            assert props["total_cents"] == {"type": "integer"}
        finally:
            host.shutdown()

    def test_list_field_uses_array_with_items(self):
        host = _build_host()
        try:
            doc = emit_asyncapi(host)
            tags = doc["components"]["schemas"]["OrderPlaced"]["properties"]["tags"]
            assert tags == {"type": "array", "items": {"type": "string"}}
        finally:
            host.shutdown()

    def test_dict_field_uses_object_with_additional_properties(self):
        host = _build_host()
        try:
            doc = emit_asyncapi(host)
            metadata = doc["components"]["schemas"]["OrderPlaced"]["properties"]["metadata"]
            assert metadata == {
                "type": "object",
                "additionalProperties": {"type": "string"},
            }
        finally:
            host.shutdown()

    def test_nested_dataclass_field_inlined(self):
        """Pinned in the module docstring: nested dataclasses are inlined.

        ``UserCreated.address: Address`` must appear in the ``UserCreated``
        schema as a full inline ``{"type": "object", "properties": {...}}``
        block — not as a ``$ref`` to a separately-emitted ``Address``
        schema.
        """
        host = _build_host()
        try:
            doc = emit_asyncapi(host)
            address = doc["components"]["schemas"]["UserCreated"]["properties"]["address"]
            # Inline object shape, not a $ref.
            assert address["type"] == "object"
            assert "$ref" not in address
            assert address["title"] == "Address"
            assert set(address["properties"].keys()) == {
                "street",
                "city",
                "postcode",
            }
            # ``postcode: str | None`` is represented as a nullable string.
            assert address["properties"]["postcode"]["type"] == [
                "string",
                "null",
            ]
        finally:
            host.shutdown()

    def test_event_subclass_with_extra_field_has_distinct_schema(self):
        """ADR-0007 exact-type routing: subclasses are not collapsed."""
        host = _build_host()
        try:
            doc = emit_asyncapi(host)
            schemas = doc["components"]["schemas"]
            assert "UserCreated" in schemas
            assert "PremiumUserCreated" in schemas
            # The subclass schema must have its own ``plan`` field.
            assert "plan" in schemas["PremiumUserCreated"]["properties"]
            # The parent schema must NOT have it.
            assert "plan" not in schemas["UserCreated"]["properties"]
            # Distinct objects (no aliasing).
            assert schemas["UserCreated"] is not schemas["PremiumUserCreated"]
        finally:
            host.shutdown()


class TestSilentModulesContributeNothing:
    """A Module with empty ``published_events`` adds no channels/messages."""

    def test_silent_only_host_emits_empty_channels(self):
        host = ModuleHost()
        host.register(SilentModule())
        try:
            doc = emit_asyncapi(host)
            assert doc["channels"] == {}
            assert doc["operations"] == {}
            assert doc["components"]["messages"] == {}
            assert doc["components"]["schemas"] == {}
        finally:
            host.shutdown()

    def test_silent_module_alongside_publisher_does_not_inflate_channels(self):
        host = ModuleHost()
        host.register(UserModule())
        host.register(SilentModule())
        try:
            doc = emit_asyncapi(host)
            # Only UserModule's two Events should show up.
            assert set(doc["channels"].keys()) == {
                "UserCreated",
                "PremiumUserCreated",
            }
        finally:
            host.shutdown()


class TestIdempotence:
    """Calling the emitter twice on the same host returns equal dicts."""

    def test_repeated_calls_produce_equal_documents(self):
        host = _build_host()
        try:
            first = emit_asyncapi(host)
            second = emit_asyncapi(host)
            assert first == second
            # Distinct objects — the emitter doesn't return a cached singleton.
            assert first is not second
        finally:
            host.shutdown()

    def test_channel_order_is_deterministic(self):
        """Channels are sorted by Event class name."""
        host = _build_host()
        try:
            doc = emit_asyncapi(host)
            assert list(doc["channels"].keys()) == sorted(doc["channels"].keys())
        finally:
            host.shutdown()


class TestPureness:
    """The emitter must not touch the host or pull in side effects."""

    def test_host_modules_unchanged_after_emit(self):
        host = _build_host()
        try:
            before = list(host.modules)
            emit_asyncapi(host)
            assert host.modules == before
        finally:
            host.shutdown()

    def test_emit_does_not_subscribe_anything(self):
        """Pure: emit_asyncapi must not register subscribers on the bus."""
        host = _build_host()
        try:
            before = {
                ev: host.event_bus.subscriber_count(ev)
                for ev in (UserCreated, PremiumUserCreated, OrderPlaced)
            }
            emit_asyncapi(host)
            after = {
                ev: host.event_bus.subscriber_count(ev)
                for ev in (UserCreated, PremiumUserCreated, OrderPlaced)
            }
            assert before == after
        finally:
            host.shutdown()


class TestPublishedEventsDefault:
    """Existing Modules with no ``published_events`` declaration still work."""

    def test_module_class_without_attribute_treated_as_empty(self):
        """Inherited default of ``()`` makes silent Modules a no-op."""
        host = ModuleHost()
        host.register(SilentModule())
        try:
            doc = emit_asyncapi(host)
            assert doc["channels"] == {}
        finally:
            host.shutdown()
