"""Local LLM access (Ollama) and a priority scheduler for the shared GPU.

Priorities (lower = more urgent):
  0 DIRECT   student was addressed
  1 ANSWER   class question / student request
  2 TASK     assignment extraction
  3 VISION   screen understanding
  4 NOTES    background note synthesis / lesson summary
  5 REVISION flashcards etc.

A more urgent job pre-empts (cancels) a running background job, which is
re-queued. Queued jobs with the same key coalesce, and the queue is bounded,
so inference can never pile up behind a talkative teacher.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

log = logging.getLogger("glass.llm")

DIRECT, ANSWER, TASK, VISION, NOTES, REVISION = range(6)
PRIORITY_NAMES = ["direct", "answer", "task", "vision", "notes", "revision"]


class LLMError(RuntimeError):
    pass


def reraise_if_cancelled() -> None:
    """Call inside `except CancelledError`: re-raise if *this task* is being
    cancelled (shutdown / caller gave up), as opposed to the awaited job having
    been superseded or pre-empted. Swallowing a real cancellation can make a
    coroutine loop forever and block interpreter shutdown."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


class OllamaClient:
    def __init__(self, base_url: str = "http://127.0.0.1:11434"):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(120, connect=3))
        self.last_first_token_ms = 0.0
        self.last_tok_s = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def version(self) -> str | None:
        try:
            r = await self._client.get("/api/version")
            return r.json().get("version")
        except Exception:
            return None

    async def list_models(self) -> list[dict]:
        r = await self._client.get("/api/tags")
        r.raise_for_status()
        return r.json().get("models", [])

    async def loaded(self) -> list[dict]:
        try:
            r = await self._client.get("/api/ps")
            return r.json().get("models", [])
        except Exception:
            return []

    async def has_model(self, name: str) -> bool:
        try:
            names = {m["name"] for m in await self.list_models()}
        except Exception:
            return False
        return name in names or f"{name}:latest" in names

    async def pull(self, name: str, on_progress: Callable[[dict], None]) -> None:
        async with self._client.stream("POST", "/api/pull", json={"model": name, "stream": True},
                                       timeout=httpx.Timeout(None, connect=5)) as r:
            async for line in r.aiter_lines():
                if line:
                    j = json.loads(line)
                    if "error" in j:
                        raise LLMError(j["error"])
                    on_progress(j)

    async def warm(self, model: str, num_ctx: int = 8192) -> None:
        """Load the model into VRAM and keep it resident.

        num_ctx must match the value every later request uses: Ollama reloads
        the model whenever it changes, and without it Ollama 0.3x allocates the
        model's full (262k) context, which spills onto the CPU.
        """
        await self._client.post("/api/chat", json={"model": model, "messages": [], "keep_alive": "60m",
                                                   "options": {"num_ctx": num_ctx}},
                                timeout=httpx.Timeout(300, connect=3))

    async def stream_chat(
        self,
        model: str,
        messages: list[dict],
        *,
        think: bool | None = False,
        temperature: float = 0.2,
        num_ctx: int = 8192,
        num_predict: int = 512,
        stop: list[str] | None = None,
        fmt: Any = None,
        images: list[bytes] | None = None,
    ) -> AsyncIterator[str]:
        msgs = [dict(m) for m in messages]
        if images:
            # attach to the last user message
            for m in reversed(msgs):
                if m["role"] == "user":
                    m["images"] = [base64.b64encode(i).decode() for i in images]
                    break
        body: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "stream": True,
            "keep_alive": "60m",
            "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict, "top_p": 0.9},
        }
        if think is not None:
            body["think"] = think
        if stop:
            body["options"]["stop"] = stop
        if fmt is not None:
            body["format"] = fmt
        t0 = time.perf_counter()
        first = True
        try:
            async with self._client.stream("POST", "/api/chat", json=body) as r:
                if r.status_code != 200:
                    txt = (await r.aread()).decode(errors="replace")
                    raise LLMError(f"ollama {r.status_code}: {txt[:300]}")
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    j = json.loads(line)
                    if "error" in j:
                        raise LLMError(j["error"])
                    chunk = j.get("message", {}).get("content", "")
                    if chunk:
                        if first:
                            self.last_first_token_ms = (time.perf_counter() - t0) * 1000
                            first = False
                        yield chunk
                    if j.get("done"):
                        ec, ed = j.get("eval_count", 0), j.get("eval_duration", 0)
                        if ec and ed:
                            self.last_tok_s = ec / (ed / 1e9)
                        break
        except httpx.ConnectError as e:
            raise LLMError("Ollama is not running (start it from the Start menu or run `ollama serve`)") from e

    async def chat(self, model: str, messages: list[dict], **kw) -> str:
        out = []
        async for c in self.stream_chat(model, messages, **kw):
            out.append(c)
        return "".join(out)


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    key: str = field(compare=False, default="")
    factory: Callable[[], Awaitable[Any]] = field(compare=False, default=None)
    future: asyncio.Future = field(compare=False, default=None)
    requeue: bool = field(compare=False, default=False)
    created: float = field(compare=False, default=0.0)
    max_age: float = field(compare=False, default=0.0)


class Scheduler:
    """Single GPU slot with priority pre-emption."""

    def __init__(self, max_queue: int = 8):
        self.max_queue = max_queue
        self._queue: list[_Job] = []
        self._seq = itertools.count()
        self._wake = asyncio.Event()
        self._running: tuple[_Job, asyncio.Task] | None = None
        self._worker: asyncio.Task | None = None
        self._stopped = False
        self.stats = {"completed": 0, "preempted": 0, "dropped": 0, "coalesced": 0}

    def start(self) -> None:
        self._worker = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._worker:
            self._worker.cancel()
        if self._running:
            self._running[1].cancel()
        for j in self._queue:
            if not j.future.done():
                j.future.cancel()
        self._queue.clear()

    def snapshot(self) -> dict:
        run = self._running[0] if self._running else None
        return {
            "running": PRIORITY_NAMES[run.priority] + (f":{run.key}" if run.key else "") if run else None,
            "queued": [PRIORITY_NAMES[j.priority] + (f":{j.key}" if j.key else "") for j in sorted(self._queue)],
            **self.stats,
        }

    def submit(self, priority: int, factory: Callable[[], Awaitable[Any]], *, key: str = "", requeue: bool = False,
               max_age: float = 0.0) -> asyncio.Future:
        """Schedule `factory()`; returns a future for its result.

        key: jobs sharing a key coalesce (the newer one wins).
        requeue: if pre-empted, run again later instead of failing.
        max_age: drop the job if it waited longer than this (seconds).
        """
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        if self._stopped:
            fut.cancel()
            return fut
        if key:
            for j in list(self._queue):
                if j.key == key:
                    self._queue.remove(j)
                    if not j.future.done():
                        j.future.cancel()
                    self.stats["coalesced"] += 1
            if self._running and self._running[0].key == key and priority <= DIRECT + 1:
                self._running[1].cancel()  # newer question replaces the one being answered
        job = _Job(priority, next(self._seq), key, factory, fut, requeue, time.monotonic(), max_age)
        self._queue.append(job)
        while len(self._queue) > self.max_queue:
            worst = max(self._queue)
            self._queue.remove(worst)
            self.stats["dropped"] += 1
            if not worst.future.done():
                worst.future.cancel()
        if self._running and priority < self._running[0].priority and self._running[0].priority >= TASK:
            run_job, task = self._running
            log.info("pre-empting %s for %s", PRIORITY_NAMES[run_job.priority], PRIORITY_NAMES[priority])
            self.stats["preempted"] += 1
            task.cancel()
        self._wake.set()
        return fut

    async def _loop(self) -> None:
        while True:
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            self._queue.sort()
            job = self._queue.pop(0)
            if job.future.done():
                continue
            if job.max_age and time.monotonic() - job.created > job.max_age:
                self.stats["dropped"] += 1
                job.future.cancel()
                continue
            task = asyncio.create_task(job.factory())
            self._running = (job, task)
            try:
                res = await asyncio.shield(task)
                if not job.future.done():
                    job.future.set_result(res)
                self.stats["completed"] += 1
            except asyncio.CancelledError:
                if not task.cancelled():
                    # we were cancelled ourselves (shutdown)
                    task.cancel()
                    raise
                if job.requeue:
                    job.seq = next(self._seq)
                    self._queue.append(job)
                elif not job.future.done():
                    job.future.cancel()
            except Exception as e:
                if not job.future.done():
                    job.future.set_exception(e)
            finally:
                self._running = None
