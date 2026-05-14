"""
Type-level test: ``AgentSpawner`` exposes ONLY ``spawn()``; the static
type checker must reject any attempt to access ``dispatch`` /
``publish`` / ``register`` / ``agent_runs`` on a variable typed as
``AgentSpawner``.

Run with: ``mypy tests/typing/test_agent_spawner_type.py``

The commented-out lines below are the *negative* type-level assertions:
uncommenting any of them must produce an mypy error
(``"AgentSpawner" has no attribute "dispatch"``, etc). If those lines
ever start type-checking cleanly without the comments, ``AgentSpawner``
has been accidentally widened and the narrow-capability guarantee from
issue #15 has been broken — fix the Protocol, do not relax this file.

The single positive assertion uses :func:`typing.assert_type` so mypy
verifies the return type of ``spawn`` propagates correctly to callers.
"""

from __future__ import annotations

# ``typing.assert_type`` is Python 3.11+; ``typing_extensions`` ships
# the same symbol on older runtimes. The project's mypy config targets
# Python 3.10, so we import from ``typing_extensions`` to stay portable.
from typing_extensions import assert_type

from pymodules import Agent, AgentRun, AgentSpawner


def _typing_only_check(spawner: AgentSpawner) -> None:
    """Body never runs — this function exists for mypy's benefit only.

    Pytest still imports the module (catching e.g. a renamed symbol),
    but no runtime call into the body is made.
    """
    # Positive assertion: ``spawn`` is on the Protocol and returns AgentRun.
    # Passing the bare ``Agent`` base class satisfies ``type[Agent]``;
    # real callers pass a concrete subclass — this is a typing fixture,
    # not a runtime call.
    result = spawner.spawn(Agent)
    assert_type(result, AgentRun)

    # Negative assertions — these MUST fail type-checking. Uncomment any
    # one of them locally to confirm mypy flags it; leave them commented
    # in the committed file so the suite stays green.
    #
    # spawner.dispatch       # E: "AgentSpawner" has no attribute "dispatch"
    # spawner.dispatch_async # E: "AgentSpawner" has no attribute "dispatch_async"
    # spawner.publish        # E: "AgentSpawner" has no attribute "publish"
    # spawner.publish_async  # E: "AgentSpawner" has no attribute "publish_async"
    # spawner.register       # E: "AgentSpawner" has no attribute "register"
    # spawner.unregister     # E: "AgentSpawner" has no attribute "unregister"
    # spawner.agent_runs     # E: "AgentSpawner" has no attribute "agent_runs"
    # spawner.event_bus      # E: "AgentSpawner" has no attribute "event_bus"
    # spawner.modules        # E: "AgentSpawner" has no attribute "modules"


def test_typing_module_imports_cleanly() -> None:
    """Smoke test so pytest collects this file and catches import-time
    breakage (renamed symbol, removed export, etc).

    The real assertion is run by mypy on the function body above; this
    test only ensures the file remains importable as the suite evolves.
    """
    # Reference the symbols so ``ruff`` / ``mypy`` don't flag them
    # unused in the typing-only function.
    assert Agent is not None
    assert AgentRun is not None
    assert AgentSpawner is not None
    assert _typing_only_check is not None
