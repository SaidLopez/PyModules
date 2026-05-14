"""
Runtime sibling of ``test_agent_spawner_type.py``.

The static type-level assertions in ``test_agent_spawner_type.py`` only
run under mypy/pyright. This file uses :mod:`inspect` to assert the
same narrowness invariant at *runtime*, so the issue-#15 contract is
enforced by ``pytest`` alone, without requiring a type checker in the
loop.

The invariant: the ``AgentSpawner`` Protocol declares exactly one
method, ``spawn``, with the signature ``(template, **kwargs) -> AgentRun``.
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

from pymodules import AgentRun, AgentSpawner


def _protocol_method_names(protocol_cls: type) -> list[str]:
    """Return the public method names declared on a ``typing.Protocol``.

    Walks ``protocol_cls.__dict__`` directly and filters out dunders +
    ``typing.Protocol``'s own bookkeeping (``_is_protocol``,
    ``_is_runtime_protocol``, etc.). The result is just the surface the
    Protocol *adds* — for our narrow capabilities, that should be a
    single-element list. Sorted for stable assertion-failure output.
    """
    return sorted(name for name in protocol_cls.__dict__ if not name.startswith("_"))


def test_agent_spawner_declares_exactly_one_method() -> None:
    """``AgentSpawner`` is a one-method Protocol.

    If a future change adds a second method to ``AgentSpawner``, this
    test fails loudly — preserving the narrow-capability guarantee from
    issue #15. Widening must be a deliberate, reviewed change to both
    the Protocol *and* this test.
    """
    methods = _protocol_method_names(AgentSpawner)
    assert methods == ["spawn"], (
        f"AgentSpawner Protocol declares {methods!r} — issue #15 "
        "requires exactly ['spawn']. Widening the Protocol breaks the "
        "narrow-capability contract that lets Modules spawn Agents "
        "without holding a host back-reference (ADR-0003)."
    )


def test_agent_spawner_spawn_signature_is_template_and_kwargs() -> None:
    """The ``spawn`` method's signature is ``(template, **kwargs)``.

    Pinning the signature keeps the Protocol stable across refactors
    of the ``host.spawn`` implementation — a private keyword-only
    argument silently added to the host method must not leak through
    the narrow capability.
    """
    sig = inspect.signature(AgentSpawner.spawn)
    params = list(sig.parameters.values())

    # ``self`` is the first param; then a positional ``template``; then
    # a ``**kwargs`` catch-all. No other positional or keyword-only
    # parameters allowed.
    assert [p.name for p in params] == ["self", "template", "kwargs"], (
        f"AgentSpawner.spawn signature drifted: {sig}"
    )
    assert params[2].kind is inspect.Parameter.VAR_KEYWORD, (
        "AgentSpawner.spawn third parameter must be **kwargs, got "
        f"{params[2].kind}"
    )


def test_agent_spawner_spawn_returns_agent_run() -> None:
    """The declared return type of ``spawn`` is :class:`AgentRun`.

    Reads the resolved type hints (forward references resolved) and
    asserts the return annotation. This is the runtime echo of the
    ``assert_type(result, AgentRun)`` line in the static fixture.
    """
    hints = get_type_hints(AgentSpawner.spawn)
    assert hints.get("return") is AgentRun, (
        f"AgentSpawner.spawn must return AgentRun; got {hints.get('return')!r}"
    )
