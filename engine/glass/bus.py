"""Thread-safe event bus bridging worker threads and the asyncio loop.

Events are plain dicts: {"type": "<domain>.<name>", ...payload}. Every event is
broadcast to connected UI clients and to in-process subscribers.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

log = logging.getLogger("glass.bus")

Handler = Callable[[dict[str, Any]], Awaitable[None] | None]


class EventBus:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self._sinks: list[Callable[[dict[str, Any]], Awaitable[None]]] = []

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._subs[event_type].append(handler)

    def add_sink(self, sink: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._sinks.append(sink)

    def remove_sink(self, sink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    def emit(self, type_: str, **payload: Any) -> None:
        """Emit from any thread."""
        evt = {"type": type_, "ts": time.time(), **payload}
        loop = self.loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(self._dispatch(evt))
        elif not loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._dispatch(evt), loop)

    async def _dispatch(self, evt: dict[str, Any]) -> None:
        for sink in list(self._sinks):
            try:
                await sink(evt)
            except Exception:  # a dead client must not break the pipeline
                log.debug("sink failed", exc_info=True)
        for handler in self._subs.get(evt["type"], []) + self._subs.get("*", []):
            try:
                res = handler(evt)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("handler for %s failed", evt["type"])


bus = EventBus()
