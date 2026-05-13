"""
Dead Letter Queue and middleware.

``DeadLetterQueue`` is externally observable — users drain and replay it —
so the class survives the middleware refactor. ``DLQMiddleware`` wraps it
for the dispatch pipeline.
"""

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..interfaces import Command
from ..logging import get_logger
from ..middleware import NextCall

if TYPE_CHECKING:
    pass

resilience_logger = get_logger("resilience")


@dataclass
class DeadLetterEntry:
    """A single failed-command record in the DLQ."""

    command: "Command[Any, Any]"
    error: Exception
    timestamp: float = field(default_factory=time.time)
    attempts: int = 1
    module_name: str = ""


class DeadLetterQueue:
    """
    Bounded FIFO of failed commands for later inspection or reprocessing.
    """

    def __init__(
        self,
        max_size: int = 1000,
        on_add: Callable[[DeadLetterEntry], None] | None = None,
    ):
        self._entries: deque[DeadLetterEntry] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._on_add = on_add
        self.max_size = max_size

    def add(
        self,
        command: "Command[Any, Any]",
        error: Exception,
        module_name: str = "",
        attempts: int = 1,
    ) -> DeadLetterEntry:
        entry = DeadLetterEntry(
            command=command,
            error=error,
            module_name=module_name,
            attempts=attempts,
        )
        with self._lock:
            self._entries.append(entry)

        resilience_logger.warning(
            "Command %s added to DLQ after %d attempts: %s",
            command.name,
            attempts,
            error,
        )

        if self._on_add:
            try:
                self._on_add(entry)
            except Exception as e:
                resilience_logger.error("DLQ on_add callback failed: %s", e)

        return entry

    @property
    def entries(self) -> list[DeadLetterEntry]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            return count

    def pop(self) -> DeadLetterEntry | None:
        with self._lock:
            if self._entries:
                return self._entries.popleft()
            return None

    def reprocess(
        self,
        handler: Callable[["Command[Any, Any]"], Any],
        max_entries: int | None = None,
    ) -> tuple[int, int]:
        """
        Drain entries and re-invoke ``handler`` (e.g. ``host.dispatch``).

        Returns ``(successful, failed)``. A response of ``None`` is treated
        as no handler claimed the command and re-queues the entry.
        """
        successful = 0
        failed = 0
        processed = 0

        while True:
            if max_entries and processed >= max_entries:
                break

            entry = self.pop()
            if entry is None:
                break

            processed += 1
            try:
                response = handler(entry.command)
                if response is not None:
                    successful += 1
                    resilience_logger.info(
                        "Successfully reprocessed command %s from DLQ",
                        entry.command.name,
                    )
                else:
                    self.add(
                        entry.command,
                        RuntimeError("No handler found on reprocess"),
                        attempts=entry.attempts + 1,
                    )
                    failed += 1
            except Exception as e:
                self.add(
                    entry.command,
                    e,
                    module_name=entry.module_name,
                    attempts=entry.attempts + 1,
                )
                failed += 1

        return successful, failed


class DLQMiddleware:
    """
    Middleware that records exceptions to a ``DeadLetterQueue``.

    Optionally swallows the exception so dispatch returns ``None`` to the
    caller (matching the host's ``propagate_exceptions=False`` semantics).
    """

    def __init__(self, queue: DeadLetterQueue, *, propagate_exceptions: bool = True) -> None:
        self.queue = queue
        self.propagate_exceptions = propagate_exceptions
        self.dead_lettered_count = 0

    async def __call__(self, command: Command[Any, Any], next_call: NextCall) -> Any:
        try:
            return await next_call(command)
        except Exception as e:
            self.queue.add(command=command, error=e)
            self.dead_lettered_count += 1
            if self.propagate_exceptions:
                raise
            return None


__all__ = ["DLQMiddleware", "DeadLetterEntry", "DeadLetterQueue"]
