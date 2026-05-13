"""
Middleware contracts for the PyModules dispatch chain.

The dispatch pipeline is a middleware chain. Each middleware is an async
callable ``(command, next) -> response``. ``ModuleHost`` composes the chain
once at construction; ``dispatch_async()`` invokes the composed chain and
``dispatch()`` is a thin sync wrapper.

The first middleware in the configured list is the outermost wrapper. The
terminal middleware (always last, built into the host) looks up
``type(command)`` in the dispatch table and calls the claiming handler.

The chain is async-first. Sync ``dispatch()`` does not bridge implicitly:
it raises ``SyncDispatchOnAsyncHandlerError`` if the resolved handler is a
coroutine function and ``SyncDispatchInAsyncContextError`` if a loop is
already running in the calling thread.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from .interfaces import Command

# A "next" call in the middleware chain — invokes the rest of the pipeline
# with the (possibly modified) command, and awaits the response.
NextCall = Callable[[Command[Any, Any]], Awaitable[Any]]

# A middleware is any async callable ``(command, next) -> response``.
Middleware = Callable[[Command[Any, Any], NextCall], Awaitable[Any]]


__all__ = ["Middleware", "NextCall"]
