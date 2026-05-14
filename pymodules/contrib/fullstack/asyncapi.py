"""AsyncAPI 3.0 emitter for PyModules **Events**.

``emit_asyncapi(host)`` walks every registered Module on the host, reads each
Module's ``published_events: ClassVar[tuple[type[Event], ...]]`` declaration,
and produces an AsyncAPI 3.0 document describing those Events as channels +
messages with JSON-schema payloads derived from the Event dataclass fields.

Design constraints:

- **Pure.** Reads no environment, opens no sockets, emits no logs, has no
  side effects on the host. Pure data in (a ``ModuleHost``), pure data out
  (a ``dict``). Suitable for golden-file testing.
- **Idempotent.** Calling twice on the same host returns equal dicts.
- **Deterministic ordering.** Channels, operations, messages, and schemas
  are emitted in alphabetic order of the Event class name so the output is
  reproducible (and diffs cleanly).
- **Exact-type per ADR-0007.** An Event subclass with extra fields produces
  its own distinct schema, never a shared one with its parent. Inheritance
  is not flattened anywhere — each declared class stands on its own.
- **Stdlib only at runtime.** This module imports only ``dataclasses``,
  ``typing``, and PyModules' own ``Event`` base. The ``[fullstack]`` extra
  pulls in ``jsonschema`` etc. for *test-time* validation, not for the
  emitter itself.

This first slice covers the field shapes the framework's own tests use:

- Python primitives: ``str``, ``int``, ``float``, ``bool``, ``bytes``,
  ``None``.
- ``T | None`` / ``Optional[T]`` — emitted as ``["<T>", "null"]`` type.
- ``list[T]`` — emitted as ``{"type": "array", "items": <T>}``.
- ``dict[str, T]`` — emitted as ``{"type": "object",
  "additionalProperties": <T>}``.
- Nested ``@dataclass`` types — emitted inline as ``{"type": "object",
  "properties": {...}, "required": [...]}``. Inline rather than ``$ref``
  to keep the v1 surface small; the codegen path can chase nested
  objects without a resolver. Pinned in the test below.

Anything we don't recognise (custom types, ``Any``, etc.) degrades to
``{"type": "object"}`` so emission never raises on a malformed declaration.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from typing import TYPE_CHECKING, Any

from pymodules.interfaces import CommandContext, Event

if TYPE_CHECKING:
    from pymodules.host import ModuleHost

# ---------------------------------------------------------------------------
# Type → JSON-schema translation
# ---------------------------------------------------------------------------

# Python builtin -> JSON-schema primitive ``"type"`` value.
_PRIMITIVE_TYPES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    bytes: "string",
}


def _is_union_type(origin: Any) -> bool:
    """True for both ``typing.Union[...]`` and ``T | U`` PEP-604 unions."""
    if origin is typing.Union:
        return True
    return origin is getattr(types, "UnionType", ())


def _is_dataclass_type(tp: Any) -> bool:
    """True if ``tp`` is a dataclass class (not an instance)."""
    return isinstance(tp, type) and dataclasses.is_dataclass(tp)


def _schema_for_type(tp: Any) -> dict[str, Any]:
    """Translate a Python type annotation to a JSON-schema fragment.

    Handles primitives, ``T | None`` / ``Optional[T]``, ``list[T]``,
    ``dict[str, T]``, and nested dataclass types (emitted inline). Unknown
    annotations degrade to ``{"type": "object"}``.
    """
    if tp is type(None):  # ``None`` as an annotation
        return {"type": "null"}

    if isinstance(tp, type) and tp in _PRIMITIVE_TYPES:
        return {"type": _PRIMITIVE_TYPES[tp]}

    if _is_dataclass_type(tp):
        return _schema_for_dataclass(tp)

    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    if _is_union_type(origin):
        # ``T | None`` is the only union we model precisely; arbitrary
        # unions ``A | B`` collapse to oneOf for the codegen side.
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and len(args) == 2:
            inner = _schema_for_type(non_none[0])
            t = inner.get("type")
            if isinstance(t, str):
                inner = {**inner, "type": [t, "null"]}
            elif isinstance(t, list):
                if "null" not in t:
                    inner = {**inner, "type": [*t, "null"]}
            else:
                inner = {**inner, "nullable": True}
            return inner
        return {"oneOf": [_schema_for_type(a) for a in args]}

    if origin in (list, tuple, set, frozenset):
        if args:
            return {"type": "array", "items": _schema_for_type(args[0])}
        return {"type": "array"}

    if origin is dict:
        if len(args) == 2:
            return {
                "type": "object",
                "additionalProperties": _schema_for_type(args[1]),
            }
        return {"type": "object"}

    # Any / unknown / typing.Any fall through to a permissive object.
    return {"type": "object"}


def _schema_for_dataclass(cls: type) -> dict[str, Any]:
    """Inline JSON-schema for a dataclass type.

    Walks ``dataclasses.fields(cls)`` with type hints resolved via
    ``typing.get_type_hints`` so PEP-563 string annotations are evaluated.
    """
    try:
        hints = typing.get_type_hints(cls)
    except Exception:
        # Forward refs that can't be resolved (rare in well-typed code)
        # degrade gracefully — keep emission total.
        hints = {f.name: f.type for f in dataclasses.fields(cls)}

    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []

    for field in dataclasses.fields(cls):
        annotated = hints.get(field.name, field.type)
        properties[field.name] = _schema_for_type(annotated)
        # A field is "required" in the AsyncAPI sense if it has no default
        # — i.e. constructing the dataclass without supplying it would raise.
        if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            required.append(field.name)

    schema: dict[str, Any] = {
        "type": "object",
        "title": cls.__name__,
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


# ---------------------------------------------------------------------------
# AsyncAPI document construction
# ---------------------------------------------------------------------------


def _collect_published_events(host: ModuleHost) -> list[type[Event]]:
    """Walk ``host.modules`` and collect each module's declared Events.

    Returns a list with duplicates removed (the same Event class declared by
    two different Modules contributes a single channel) and sorted by class
    name for deterministic output.
    """
    seen: dict[str, type[Event]] = {}
    for module in host.modules:
        declared = getattr(type(module), "published_events", ())
        for event_cls in declared:
            if not (isinstance(event_cls, type) and issubclass(event_cls, Event)):
                # Stay loud-but-bounded: a misdeclared entry is the
                # Module's bug to fix, but we don't want emission to
                # explode on it during a partial migration. Skip it.
                continue
            seen.setdefault(event_cls.__name__, event_cls)
    return [seen[name] for name in sorted(seen)]


def _channel_name(event_cls: type[Event]) -> str:
    """The channel address used for an Event class.

    We use the bare class name. AsyncAPI lets the channel *key* (the lookup
    name in the ``channels`` map) and the channel *address* (the runtime
    routing key) differ; we keep them identical for v1 — the routing
    surface is in-process anyway, and a stable class-name key lines up with
    the SSE wire format (``event: <ClassName>``) the later slice ships.
    """
    return event_cls.__name__


def emit_asyncapi(
    host: ModuleHost,
    *,
    title: str = "PyModules Event API",
    version: str = "1.0.0",
) -> dict[str, Any]:
    """Build an AsyncAPI 3.0 document for the host's declared Events.

    Args:
        host: The ``ModuleHost`` whose registered Modules will be scanned.
        title: ``info.title`` for the AsyncAPI document.
        version: ``info.version`` for the AsyncAPI document.

    Returns:
        A ``dict`` shaped as a valid AsyncAPI 3.0 document. The dict is
        owned by the caller — emitting again returns a freshly-constructed
        equal dict (idempotent, no shared mutable state).

    The function is pure: no I/O, no logging, no host mutation.
    """
    events = _collect_published_events(host)

    channels: dict[str, Any] = {}
    operations: dict[str, Any] = {}
    messages: dict[str, Any] = {}
    schemas: dict[str, Any] = {}

    for event_cls in events:
        chan = _channel_name(event_cls)
        msg_name = event_cls.__name__
        schema_name = event_cls.__name__

        payload_schema = _schema_for_dataclass(event_cls)
        # ``context`` (``CommandContext``) and ``name`` are framework-level
        # fields every Event carries; we keep them in the payload schema
        # rather than hiding them, so codegen receives the same shape that
        # crosses the wire.
        schemas[schema_name] = payload_schema

        messages[msg_name] = {
            "name": msg_name,
            "title": event_cls.__name__,
            "contentType": "application/json",
            "payload": {"$ref": f"#/components/schemas/{schema_name}"},
        }

        channels[chan] = {
            "address": chan,
            "messages": {
                msg_name: {"$ref": f"#/components/messages/{msg_name}"},
            },
        }

        operations[f"receive{chan}"] = {
            "action": "receive",
            "channel": {"$ref": f"#/channels/{chan}"},
            "messages": [
                {"$ref": f"#/channels/{chan}/messages/{msg_name}"},
            ],
        }

    # The CommandContext schema is referenced by every Event payload's
    # ``context`` field; emit it once so the codegen side gets a single
    # canonical definition rather than N inlined copies. (We still inline
    # it inside each payload's properties — the components entry is
    # purely a documentation convenience for now; later slices may switch
    # to ``$ref`` once codegen depends on it.)
    if events:
        schemas.setdefault(
            CommandContext.__name__,
            _schema_for_dataclass(CommandContext),
        )

    document: dict[str, Any] = {
        "asyncapi": "3.0.0",
        "info": {
            "title": title,
            "version": version,
        },
        "channels": channels,
        "operations": operations,
        "components": {
            "messages": messages,
            "schemas": schemas,
        },
    }
    return document


__all__ = ["emit_asyncapi"]
