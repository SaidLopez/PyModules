"""Tests for ``pymodules.contrib.fullstack.outbound_policy``.

These tests pin the observable contract of :class:`OutboundPolicyRegistry`
and the ``@outbound_policy`` decorator — the small core of the
deny-by-default Outbound policy mechanism that the SSE slice (#6) will
consume.

Style note: synthetic Events, Modules, and a stand-in ``ClientContext``
are defined inline (matching ``tests/test_eventbus.py`` and
``tests/contrib/fullstack/test_asyncapi.py``) so each test reads
top-to-bottom. The real ``ClientContext`` ships in the cookie-auth slice
(#5); we don't depend on its concrete shape here — a tiny frozen
dataclass with the same surface area is enough.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from pymodules import Event, Module, ModuleHost
from pymodules.contrib.fullstack import (
    FullstackError,
    OutboundPolicyConflict,
    OutboundPolicyRegistry,
    outbound_policy,
)

# ---------------------------------------------------------------------------
# Synthetic Events + minimal ClientContext stand-in
# ---------------------------------------------------------------------------


@dataclass
class MessagePosted(Event):
    tenant_id: str = ""
    body: str = ""
    name: str = "message.posted"


@dataclass
class OrderPlaced(Event):
    tenant_id: str = ""
    order_id: str = ""
    name: str = "order.placed"


@dataclass(frozen=True)
class FakeClientContext:
    """Stand-in for ``pymodules.contrib.fullstack.ClientContext``.

    The cookie-auth slice (#5) owns the real frozen dataclass; this test
    file is deliberately decoupled from that slice's import shape so the
    two slices can land independently. Any object with ``tenant_id`` and
    ``user_id`` attributes is enough for the policies under test.
    """

    user_id: str = "u-1"
    tenant_id: str = "t-1"
    claims: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# OutboundPolicyRegistry unit tests
# ---------------------------------------------------------------------------


class TestRegisterAndHasPolicy:
    """``register`` + ``has_policy`` form the registry's read/write surface."""

    def test_has_policy_false_before_registration(self):
        registry = OutboundPolicyRegistry()
        assert registry.has_policy(MessagePosted) is False

    def test_has_policy_true_after_registration(self):
        registry = OutboundPolicyRegistry()
        registry.register(MessagePosted, lambda event, client: True)
        assert registry.has_policy(MessagePosted) is True

    def test_has_policy_is_per_event_class(self):
        """ADR-0007 exact-type routing: subclasses do not inherit policies."""
        registry = OutboundPolicyRegistry()
        registry.register(MessagePosted, lambda event, client: True)
        assert registry.has_policy(OrderPlaced) is False

    def test_register_rejects_non_event_class(self):
        registry = OutboundPolicyRegistry()
        with pytest.raises(TypeError):
            registry.register(str, lambda event, client: True)  # type: ignore[arg-type]


class TestDoubleRegistration:
    """Double-registration is loud by default — mirrors ``DuplicateCommandError``."""

    def test_double_registration_raises_outbound_policy_conflict(self):
        registry = OutboundPolicyRegistry()
        registry.register(MessagePosted, lambda event, client: True)
        with pytest.raises(OutboundPolicyConflict):
            registry.register(MessagePosted, lambda event, client: False)

    def test_outbound_policy_conflict_inherits_fullstack_error(self):
        """The fullstack contrib roots its exceptions in ``FullstackError``."""
        assert issubclass(OutboundPolicyConflict, FullstackError)

    def test_override_true_replaces_existing_policy(self):
        registry = OutboundPolicyRegistry()
        registry.register(MessagePosted, lambda event, client: True)
        registry.register(MessagePosted, lambda event, client: False, override=True)
        # Confirm the *new* callable is the live one.
        event = MessagePosted(tenant_id="t-1", body="hi")
        assert registry.apply(event, FakeClientContext()) is False


class TestApply:
    """``apply`` invokes the registered callable for matching Event types."""

    def test_apply_returns_callable_bool_for_matching_event(self):
        registry = OutboundPolicyRegistry()

        def policy(event: MessagePosted, client: FakeClientContext) -> bool:
            return event.tenant_id == client.tenant_id

        registry.register(MessagePosted, policy)

        match = MessagePosted(tenant_id="t-1", body="hi")
        miss = MessagePosted(tenant_id="t-2", body="hi")
        assert registry.apply(match, FakeClientContext(tenant_id="t-1")) is True
        assert registry.apply(miss, FakeClientContext(tenant_id="t-1")) is False

    def test_apply_unregistered_event_returns_false(self):
        """Pinned deny-by-default for unregistered Event classes.

        The PRD frames the Outbound layer as "deny-by-default outbound" —
        the safe failure mode when no policy is registered is to drop the
        message rather than leak it. The SSE slice still rejects
        subscriptions to unpolicied Events with a loud HTTP 400, so
        reaching this branch at runtime indicates a publishing Module
        without an outbound contract; deny silently here and let
        observability tooling surface the missing policy.

        Changing this default — e.g., raising ``MissingOutboundPolicy``
        instead — would require a deliberate update to this test.
        """
        registry = OutboundPolicyRegistry()
        unpolicied = MessagePosted(tenant_id="t-1", body="leak?")
        assert registry.apply(unpolicied, FakeClientContext()) is False

    def test_apply_uses_exact_type_not_subclass(self):
        """Per ADR-0007: a registered policy on Parent does not cover Subclass."""

        @dataclass
        class PriorityMessage(MessagePosted):
            priority: int = 1
            name: str = "message.posted.priority"

        registry = OutboundPolicyRegistry()
        registry.register(MessagePosted, lambda event, client: True)

        subclass_event = PriorityMessage(tenant_id="t-1", body="x", priority=9)
        # Subclass routes to its own (absent) policy → deny-by-default.
        assert registry.apply(subclass_event, FakeClientContext()) is False


# ---------------------------------------------------------------------------
# @outbound_policy decorator wiring through ModuleHost.register
# ---------------------------------------------------------------------------


class TestDecoratorWiring:
    """``@outbound_policy`` marks a Module method; ``host.register`` wires it."""

    def test_decorator_wires_method_into_host_registry(self):
        class ChatModule(Module):
            published_events = (MessagePosted,)

            @outbound_policy(MessagePosted)
            def gate_message_posted(self, event: MessagePosted, client: FakeClientContext) -> bool:
                return event.tenant_id == client.tenant_id

        host = ModuleHost()
        host.register(ChatModule())
        try:
            assert host.outbound_policies.has_policy(MessagePosted) is True

            match = MessagePosted(tenant_id="t-1", body="hi")
            miss = MessagePosted(tenant_id="t-2", body="hi")
            assert host.outbound_policies.apply(match, FakeClientContext(tenant_id="t-1")) is True
            assert host.outbound_policies.apply(miss, FakeClientContext(tenant_id="t-1")) is False
        finally:
            host.shutdown()

    def test_decorator_rejects_non_event_class(self):
        with pytest.raises(TypeError):

            @outbound_policy(str)  # type: ignore[arg-type]
            def bad_policy(self, event, client):
                return True

    def test_double_decorator_across_modules_raises_conflict(self):
        """Two Modules both decorating policies for the same Event must conflict."""

        class ModuleA(Module):
            published_events = (MessagePosted,)

            @outbound_policy(MessagePosted)
            def gate(self, event: MessagePosted, client: Any) -> bool:
                return True

        class ModuleB(Module):
            published_events = (MessagePosted,)

            @outbound_policy(MessagePosted)
            def gate(self, event: MessagePosted, client: Any) -> bool:
                return False

        host = ModuleHost()
        host.register(ModuleA())
        try:
            with pytest.raises(OutboundPolicyConflict):
                host.register(ModuleB())
        finally:
            host.shutdown()


class TestLazyRegistryConstruction:
    """Hosts that need no policy never instantiate the registry."""

    def test_empty_module_does_not_instantiate_registry(self):
        """A Module declaring neither ``published_events`` nor a policy is silent.

        Pinned: the lazy ``host.outbound_policies`` property is the only
        construction site, so an empty Module's ``register`` path must
        not touch it.
        """

        class SilentModule(Module):
            # Inherits ``published_events = ()`` default from ``Module``,
            # and has no ``@outbound_policy`` method.
            pass

        host = ModuleHost()
        host.register(SilentModule())
        try:
            # Reach into the private attribute deliberately: this test
            # pins the *construction-side-effect* contract, which can't
            # be observed via the lazy property without triggering it.
            assert host._outbound_policies is None
        finally:
            host.shutdown()

    def test_publisher_with_no_policy_still_constructs_registry(self):
        """``published_events`` alone is enough to materialise the registry.

        The SSE slice (#6) needs ``host.outbound_policies`` available to
        check ``has_policy`` at subscribe time — even for Modules that
        publish Events without yet wiring a policy callable (the SSE
        layer then rejects the subscription with a loud HTTP 400).
        """

        class PublisherWithoutPolicy(Module):
            published_events = (MessagePosted,)

        host = ModuleHost()
        host.register(PublisherWithoutPolicy())
        try:
            assert host._outbound_policies is not None
            assert host.outbound_policies.has_policy(MessagePosted) is False
        finally:
            host.shutdown()

    def test_outbound_policies_property_is_idempotent(self):
        """Repeated access returns the same registry instance."""

        class PolicyModule(Module):
            published_events = (MessagePosted,)

            @outbound_policy(MessagePosted)
            def gate(self, event: MessagePosted, client: Any) -> bool:
                return True

        host = ModuleHost()
        host.register(PolicyModule())
        try:
            first = host.outbound_policies
            second = host.outbound_policies
            assert first is second
        finally:
            host.shutdown()


class TestUnregisterRemovesPolicy:
    """``host.unregister`` must drop the Module's outbound policies."""

    def test_unregister_removes_policy_from_registry(self):
        class ChatModule(Module):
            published_events = (MessagePosted,)

            @outbound_policy(MessagePosted)
            def gate(self, event: MessagePosted, client: Any) -> bool:
                return True

        host = ModuleHost()
        module = ChatModule()
        host.register(module)
        try:
            assert host.outbound_policies.has_policy(MessagePosted) is True
            host.unregister(module)
            assert host.outbound_policies.has_policy(MessagePosted) is False
        finally:
            host.shutdown()
