"""Local WebSocket API between the Electron UI and the engine.

Security: binds to 127.0.0.1 only and requires the per-launch token the
Electron main process generates (so a web page in the student's browser cannot
connect to ws://localhost and read lesson content).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from typing import Any
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.asyncio.server import ServerConnection, serve

from .bus import bus
from .engine import Engine
from .intel import notes_markdown

log = logging.getLogger("glass.server")


class Server:
    def __init__(self, engine: Engine, port: int, token: str, on_shutdown=None):
        self.engine = engine
        self.on_shutdown = on_shutdown
        self.port = port
        self.token = token
        self.clients: set[ServerConnection] = set()

    async def broadcast(self, evt: dict) -> None:
        if not self.clients:
            return
        msg = json.dumps(evt, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def handler(self, ws: ServerConnection) -> None:
        q = parse_qs(urlparse(ws.request.path).query)
        tok = (q.get("token") or [""])[0]
        if not self.token or not hmac.compare_digest(tok, self.token):
            await ws.close(4401, "unauthorised")
            return
        self.clients.add(ws)
        try:
            await ws.send(json.dumps({"type": "hello", "snapshot": self.engine.snapshot()}, default=str))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                asyncio.create_task(self._dispatch(ws, msg))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)

    async def _dispatch(self, ws: ServerConnection, msg: dict) -> None:
        rid = msg.get("rid")
        cmd = msg.get("cmd", "")
        try:
            result = await self.run(cmd, msg)
            reply = {"type": "reply", "rid": rid, "ok": True, "result": result}
        except Exception as e:
            log.warning("command %s failed: %s", cmd, e)
            reply = {"type": "reply", "rid": rid, "ok": False, "error": str(e)}
        try:
            await ws.send(json.dumps(reply, default=str))
        except Exception:
            pass

    async def run(self, cmd: str, m: dict) -> Any:
        e = self.engine
        a = e.answers
        if cmd == "snapshot":
            return e.snapshot()
        if cmd == "shutdown":
            if self.on_shutdown:
                asyncio.get_running_loop().call_later(0.1, self.on_shutdown)
            return True
        if cmd == "session.start":
            return await e.start_session(m.get("subject_id"), m.get("title", ""))
        if cmd == "session.stop":
            return await e.stop_session()
        if cmd == "session.pause":
            e.set_paused(bool(m.get("paused", True)))
            return e.session_public()
        if cmd == "session.subject":
            e.set_subject(m.get("subject_id"))
            return e.mem.state()
        if cmd == "ask":
            e.note_help_request()
            ans = await a.ask(m["text"])
            return {"id": ans["id"]}
        if cmd == "context":
            e.note_help_request() if not e.mem.active_question else None
            ans = await a.clarify(m["text"])
            return {"id": ans["id"]}
        if cmd == "answer.expand":
            ans = await a.expand(m["id"])
            return {"id": ans["id"]} if ans else None
        if cmd == "explain":
            e.note_help_request()
            return {"id": (await a.explain_now())["id"]}
        if cmd == "catchup":
            e.note_help_request()
            return {"id": (await a.catchup(float(m.get("minutes", 3))))["id"]}
        if cmd == "tutor":
            e.note_help_request()
            ans = await a.tutor(m.get("mode", "hint"), m.get("task"), m.get("attempt", ""), m.get("focus", ""))
            return {"id": ans["id"]}
        if cmd == "snip":
            ans = await e.snip(int(m["x"]), int(m["y"]), int(m["w"]), int(m["h"]), bool(m.get("vision")))
            return {"id": ans["id"]}
        if cmd == "snip.freeze":
            # snapshot the monitor BEFORE the selection window opens (see ScreenService.freeze)
            res = await asyncio.to_thread(e.screen.freeze, int(m["x"]), int(m["y"]))
            if not res:
                return None
            img, mon = res
            import base64
            import io

            from PIL import Image

            buf = io.BytesIO()
            Image.fromarray(img[:, :, [2, 1, 0]]).save(buf, format="JPEG", quality=88)
            return {"image": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(),
                    "monitor": mon}
        if cmd == "vision.prepare":
            asyncio.create_task(a.prepare_vision())
            return True
        if cmd == "dismiss":
            e.pending_q = None
            e._set_mode("passive" if e.session else "idle")
            return True
        if cmd == "task.complete":
            e.intel.complete_task(m["id"])
            return True
        if cmd == "task.timer":
            e.intel.start_timer(m["id"])
            return True
        if cmd == "notes.get":
            sid = m.get("session_id")
            if not sid or (e.session and sid == e.session["id"]):
                return {"notes": e.intel.notes, "markdown": notes_markdown(e.intel.notes) if e.intel.notes else ""}
            row = e.db.get_notes(sid)
            return {"notes": row["data"], "markdown": row["markdown"], "edited": row["edited"]} if row else None
        if cmd == "notes.save":
            sid = m["session_id"]
            md = m["markdown"]
            if e.session and sid == e.session["id"]:
                e.intel.notes_edited = True
            row = e.db.get_notes(sid)
            data = row["data"] if row else {}
            sess = e.db.get_session(sid) or {}
            e.db.save_notes(sid, data, md, sess.get("subject_id"), sess.get("topic") or sess.get("title") or "Lesson", True)
            return True
        if cmd == "notes.refresh":
            await e.intel.update_notes(final=True)
            return True
        if cmd == "sessions.list":
            return e.db.list_sessions(m.get("subject_id"))
        if cmd == "session.detail":
            sid = m["session_id"]
            return {"session": e.db.get_session(sid), "notes": e.db.get_notes(sid), "questions": e.db.questions(sid),
                    "tasks": e.db.tasks(sid), "transcript": e.db.segments(sid), "formulas": e.db.formulas(sid=sid),
                    "flashcards": e.db.flashcards(sid=sid)}
        if cmd == "session.delete":
            e.db.delete_session(m["session_id"])
            return True
        if cmd == "search":
            return e.db.search(m["query"], m.get("subject_id"))
        if cmd == "tasks.list":
            return e.db.tasks(status=m.get("status"))
        if cmd == "formulas":
            return e.db.formulas(subject_id=m.get("subject_id"), sid=m.get("session_id"))
        if cmd == "formula.pin":
            e.db.pin_formula(m["id"], bool(m.get("pinned", True)))
            return True
        if cmd == "flashcards.generate":
            return await e.intel.flashcards(m.get("session_id"))
        if cmd == "flashcards":
            return e.db.flashcards(sid=m.get("session_id"), subject_id=m.get("subject_id"))
        if cmd == "subjects.stats":
            return e.db.subject_stats()
        if cmd == "settings.get":
            return e.settings.all()
        if cmd == "settings.set":
            patch = m.get("patch", {})
            out = e.settings.update(patch)
            if {"student_name", "name_aliases"} & patch.keys():
                e.names.configure(e.settings.get("student_name"), e.settings.get("name_aliases"))
                e._configure_asr()
            if "language" in patch:
                e._configure_asr()
            if "screen_enabled" in patch and e.session:
                if patch["screen_enabled"]:
                    e.screen.start()
                else:
                    await e.screen.stop()
            if "ocr_engine" in patch:
                e.screen.ocr.engine = patch["ocr_engine"]
            if "profile" in patch or "overrides" in patch or "vision_model" in patch:
                asyncio.create_task(self._reload_models())
            if "mic_enabled" in patch and e.session:
                asyncio.create_task(self._restart_audio())
            if "audio_device" in patch and e.session:
                asyncio.create_task(self._restart_audio())
            bus.emit("settings", settings=out)
            return out
        if cmd == "devices":
            return await asyncio.to_thread(e.devices)
        if cmd == "windows":
            return await asyncio.to_thread(e.windows)
        if cmd == "overlay.bounds":
            e.screen.mask_rects = [tuple(int(v) for v in r) for r in m.get("rects", [])]
            return True
        if cmd == "models.pull":
            asyncio.create_task(e.pull_model(m["model"]))
            return True
        if cmd == "models.list":
            return await e.llm.list_models()
        if cmd == "llm.check":
            await e.check_llm()
            return e.status
        if cmd == "storage.purge":
            return e.db.purge_older_than(int(m.get("days", 0)) or 1)
        raise ValueError(f"unknown command {cmd!r}")

    async def _reload_models(self) -> None:
        e = self.engine
        prof = e.settings.profile()
        e.answers.text_model = e.intel.model = prof["llm_model"]
        e.answers.vision_model = e.settings.get("vision_model")
        e.asr.beam = int(prof.get("asr_beam", 2))
        if prof["asr_model"] != e.asr.model_name:
            import threading

            threading.Thread(target=e._load_asr, args=(prof["asr_model"], prof.get("asr_compute", "float16")),
                             daemon=True).start()
        await e.check_llm()

    async def _restart_audio(self) -> None:
        e = self.engine
        e._stop_audio()
        await asyncio.sleep(0.2)
        e._start_audio()

    async def serve_forever(self) -> None:
        bus.add_sink(self.broadcast)
        async with serve(self.handler, "127.0.0.1", self.port, max_size=8 * 2**20, ping_interval=20) as srv:
            log.info("GLASS engine listening on ws://127.0.0.1:%d", self.port)
            print(f"GLASS_READY {self.port}", flush=True)
            await srv.serve_forever()
