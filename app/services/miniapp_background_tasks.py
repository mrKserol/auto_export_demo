from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any


logger = logging.getLogger(__name__)

_pending_tasks: set[asyncio.Task[Any]] = set()


def pending_miniapp_task_count() -> int:
    return len(_pending_tasks)


def spawn_miniapp_background_task(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Create an in-process background task with exception logging.

    Tasks are tracked for shutdown diagnostics only. This is not a durable queue:
    process restart still loses in-flight work and relies on stale-batch recovery.
    """
    task = asyncio.create_task(coro, name=name)
    _pending_tasks.add(task)

    def _on_done(done: asyncio.Task[Any]) -> None:
        _pending_tasks.discard(done)
        try:
            exception = done.exception()
        except asyncio.CancelledError:
            return
        if exception is not None:
            logger.exception(
                "Mini App background task failed name=%s",
                done.get_name(),
                exc_info=exception,
            )

    task.add_done_callback(_on_done)
    return task
