"""Background lesson intelligence: lesson state, incremental notes, task
extraction, flashcards. All of it runs at lower priority than answers and is
pre-emptible; outputs use JSON-schema constrained decoding."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
import uuid
from typing import Any, Callable

from rapidfuzz import fuzz

from . import prompts as P
from .llm import NOTES, REVISION, TASK, OllamaClient, Scheduler, reraise_if_cancelled
from .memory import LessonMemory

log = logging.getLogger("glass.intel")

STATE_SCHEMA = {
    "type": "object",
    "properties": {"subject_id": {"type": "string"}, "topic": {"type": "string"}, "subtopic": {"type": "string"},
                   "summary": {"type": "string"}},
    "required": ["subject_id", "topic", "subtopic", "summary"],
}

_src = {"type": "string", "enum": ["teacher", "inferred"]}
NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "objectives": {"type": "array", "items": {"type": "string"}},
        "summary_update": {"type": "string"},
        "definitions": {"type": "array", "items": {"type": "object", "properties": {
            "term": {"type": "string"}, "definition": {"type": "string"}, "source": _src}, "required": ["term", "definition", "source"]}},
        "explanations": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "text": {"type": "string"}, "source": _src}, "required": ["title", "text", "source"]}},
        "examples": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "text": {"type": "string"}, "source": _src}, "required": ["title", "text", "source"]}},
        "formulas": {"type": "array", "items": {"type": "object", "properties": {
            "formula": {"type": "string"}, "meaning": {"type": "string"}, "units": {"type": "string"}, "source": _src},
            "required": ["formula", "meaning", "units", "source"]}},
        "exam_tips": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "source": _src}, "required": ["text", "source"]}},
        "questions": {"type": "array", "items": {"type": "object", "properties": {
            "q": {"type": "string"}, "a": {"type": "string"}, "answered_by": {"type": "string", "enum": ["teacher", "student", "unanswered"]}},
            "required": ["q", "a", "answered_by"]}},
        "corrections": {"type": "array", "items": {"type": "string"}},
        "revisit": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["topic", "objectives", "summary_update", "definitions", "explanations", "examples", "formulas",
                 "exam_tips", "questions", "corrections", "revisit"],
}

TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "is_task": {"type": "boolean"},
        "relation": {"type": "string", "enum": ["new", "replace", "supplement", "update"]},
        "description": {"type": "string"},
        "source": {"type": ["string", "null"]},
        "questions": {"type": ["string", "null"]},
        "first_question": {"type": ["string", "null"]},
        "time_minutes": {"type": ["number", "null"]},
        "timer_started": {"type": "boolean"},
        "deadline": {"type": ["string", "null"]},
        "is_homework": {"type": "boolean"},
        "instructions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["is_task", "relation", "description", "source", "questions", "first_question", "time_minutes",
                 "timer_started", "deadline", "is_homework", "instructions"],
}

CARDS_SCHEMA = {
    "type": "object",
    "properties": {"cards": {"type": "array", "items": {"type": "object", "properties": {
        "front": {"type": "string"}, "back": {"type": "string"}}, "required": ["front", "back"]}}},
    "required": ["cards"],
}

SECTIONS = ["definitions", "explanations", "examples", "formulas", "exam_tips", "questions", "corrections", "revisit"]


def loads_lenient(raw: str) -> dict:
    """Parse model JSON; if generation was cut off, keep every complete item."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    s = raw
    for _ in range(80):
        cut = max(s.rfind("},"), s.rfind('",'), s.rfind("],"))
        if cut <= 0:
            break
        s = s[:cut + 1]
        stack, in_str, esc = [], False, False
        for ch in s:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack:
                stack.pop()
        try:
            return json.loads(s + "".join(reversed(stack)))
        except json.JSONDecodeError:
            s = s[:-1]
    raise json.JSONDecodeError("unrecoverable", raw, 0)


def empty_notes(subject: str, date: str) -> dict:
    return {"subject": subject, "topic": "", "date": date, "objectives": [], "summary": "", **{k: [] for k in SECTIONS}}


def _key(item: Any) -> str:
    if isinstance(item, str):
        return item
    return item.get("term") or item.get("title") or item.get("formula") or item.get("q") or item.get("text") or ""


def notes_markdown(n: dict) -> str:
    tag = lambda it: " *(inferred)*" if isinstance(it, dict) and it.get("source") == "inferred" else ""
    out = [f"# {n.get('topic') or 'Lesson notes'}", f"**{n.get('subject', '')}** · {n.get('date', '')}", ""]
    if n.get("objectives"):
        out += ["## Learning objectives"] + [f"- {o}" for o in n["objectives"]] + [""]
    if n.get("summary"):
        out += ["## Overview", n["summary"], ""]
    if n.get("definitions"):
        out += ["## Key definitions"] + [f"- **{d['term']}** — {d['definition']}{tag(d)}" for d in n["definitions"]] + [""]
    if n.get("explanations"):
        out += ["## Explanations"] + [f"### {e['title']}{tag(e)}\n{e['text']}" for e in n["explanations"]] + [""]
    if n.get("formulas"):
        out += ["## Formulas"] + [f"- `{f['formula']}` — {f['meaning']}" + (f" ({f['units']})" if (f.get('units') or '').strip().lower() not in ('', 'none', 'n/a', '-', 'no units') else "") + tag(f)
                                  for f in n["formulas"]] + [""]
    if n.get("examples"):
        out += ["## Worked examples"] + [f"### {e['title']}{tag(e)}\n{e['text']}" for e in n["examples"]] + [""]
    if n.get("exam_tips"):
        out += ["## Exam tips"] + [f"- {t['text']}{tag(t)}" for t in n["exam_tips"]] + [""]
    if n.get("questions"):
        out += ["## Questions from class"]
        for q in n["questions"]:
            a = q.get("a") or ""
            who = q.get("answered_by", "")
            out.append(f"- **Q:** {q['q']}" + (f"\n  **A:** {a}" + (" *(GLASS suggestion — not confirmed in class)*" if who == "unanswered" else "") if a else ""))
        out.append("")
    if n.get("corrections"):
        out += ["## Corrections made in class"] + [f"- {c}" for c in n["corrections"]] + [""]
    if n.get("revisit"):
        out += ["## Revision checklist"] + [f"- [ ] {r}" for r in n["revisit"]] + [""]
    return "\n".join(out).strip() + "\n"


class LessonIntel:
    def __init__(self, llm: OllamaClient, sched: Scheduler, memory: LessonMemory, settings, db, emit: Callable[..., None]):
        self.llm = llm
        self.sched = sched
        self.mem = memory
        self.settings = settings
        self.db = db
        self.emit = emit
        self.model = "qwen3:4b-instruct"
        self.num_ctx = 8192
        self.session_id: str | None = None
        self.notes: dict = {}
        self.notes_edited = False
        self._notes_running = False
        self._last_notes_t = 0.0
        self._state_running = False
        self.tasks_history: list[dict] = []
        self._last_task_extract = 0.0
        self.after_job = None  # callback after a background LLM job (CPU prefix re-warm)

    def _after(self) -> None:
        if self.after_job:
            asyncio.ensure_future(self.after_job())

    def begin(self, session_id: str, subject_name: str) -> None:
        self.session_id = session_id
        self.notes = empty_notes(subject_name, dt.date.today().isoformat())
        self.notes_edited = False
        self._last_notes_t = time.time()

    # -------------------------------------------------------------- lesson state
    async def update_state(self, force: bool = False) -> None:
        if self._state_running or (not force and self.mem.chars_since_state < int(self.settings.profile().get("state_chars", 350))):
            return
        self._state_running = True
        self.mem.chars_since_state = 0
        try:
            ids = [s["id"] for s in self.settings.get("subjects")]
            ctx = self.mem.recent(seconds=150, max_chars=3000, include_me=False)
            screen = self.mem.screen_context(800)
            if not ctx and not screen:
                return
            user = f"PREVIOUS TOPIC: {self.mem.topic or 'unknown'}\nRECENT SPEECH:\n{ctx}\n\nSCREEN:\n{screen or '(none)'}"
            msgs = [{"role": "system", "content": P.SYSTEM_STATE.format(subject_ids=ids)}, {"role": "user", "content": user}]

            async def job():
                return await self.llm.chat(self.model, msgs, think=False, num_ctx=self.num_ctx, num_predict=120,
                                           temperature=0.1, fmt=STATE_SCHEMA)

            raw = await self.sched.submit(TASK, job, key="state", max_age=20)
            st = json.loads(raw)
            if not self.mem.subject_locked and st.get("subject_id") in ids:
                self.mem.subject_id = st["subject_id"]
            if st.get("topic"):
                self.mem.topic = st["topic"][:60]
            self.mem.subtopic = (st.get("subtopic") or "")[:60]
            self.mem.summary = (st.get("summary") or "")[:200]
            self.emit("lesson.state", state=self.mem.state())
            self._after()
            if self.db and self.session_id:
                self.db.update_session(self.session_id, topic=self.mem.topic, subject_id=self.mem.subject_id)
        except asyncio.CancelledError:
            reraise_if_cancelled()
        except json.JSONDecodeError:
            pass
        except Exception as e:
            log.warning("state update failed: %s", e)
        finally:
            self._state_running = False

    # -------------------------------------------------------------------- notes
    def notes_due(self) -> bool:
        new_chars = sum(len(u.text) for u in self.mem.utterances[self.mem.notes_cursor:])
        p = self.settings.profile()
        if p.get("notes_paused"):
            return False
        gap = float(p.get("notes_interval", 150))
        return not self._notes_running and (new_chars > 1500 and time.time() - self._last_notes_t > gap / 3
                                            or (new_chars > 250 and time.time() - self._last_notes_t > gap))

    async def update_notes(self, final: bool = False) -> None:
        if self._notes_running:
            return
        start = self.mem.notes_cursor
        chunk = self.mem.utterances[start:]
        if not chunk:
            return
        self._notes_running = True
        progressed = False
        try:
            # split very long backlogs so each call stays inside the context window
            text, end = "", start
            for u in chunk:
                line = f"{'Student' if u.source == 'me' else 'Teacher'}: {u.text}\n"
                if len(text) + len(line) > 7000:
                    break
                text += line
                end += 1
            screen_bits = []
            for obs in list(self.mem.screen)[-4:]:
                if obs.t >= (chunk[0].t0 - 30):
                    screen_bits.append(obs.text[:900])
            existing = {k: [_key(i) for i in self.notes.get(k, [])][-25:] for k in ("definitions", "formulas", "explanations", "examples")}
            qa = [f"Q: {q['resolved'] or q['text']} -> GLASS suggested: {q['say']}" for q in self.mem.questions
                  if q["t"] >= chunk[0].t0 - 5][-8:]
            subj = self.notes.get("subject", "")
            user = (f"EXISTING NOTE HEADINGS (do not repeat): {json.dumps(existing)}\n"
                    f"LESSON SUMMARY SO FAR: {self.notes.get('summary', '')[-800:]}\n\n"
                    f"NEW TEACHER SPEECH (automatic transcription, may contain errors):\n{text}\n"
                    + (f"\nSCREEN TEXT SHOWN DURING THIS PART:\n" + "\n---\n".join(screen_bits) if screen_bits else "")
                    + (f"\nQUESTIONS ASKED IN THIS PART (GLASS suggestions are NOT teacher statements):\n" + "\n".join(qa) if qa else ""))
            msgs = [{"role": "system", "content": P.SYSTEM_NOTES.format(subject=subj)}, {"role": "user", "content": user}]

            async def job():
                return await self.llm.chat(self.model, msgs, think=False, num_ctx=self.num_ctx, num_predict=1800,
                                           temperature=0.2, fmt=NOTES_SCHEMA)

            raw = await self.sched.submit(NOTES, job, key="notes", requeue=True)
            delta = loads_lenient(raw)
            progressed = True
            self._merge(delta)
            self.mem.notes_cursor = end
            self._last_notes_t = time.time()
            self.persist()
            self.emit("notes.update", session_id=self.session_id, notes=self.notes, markdown=notes_markdown(self.notes))
            self._after()
        except asyncio.CancelledError:
            reraise_if_cancelled()
        except json.JSONDecodeError as e:
            log.warning("notes JSON invalid: %s", e)
        except Exception as e:
            log.warning("notes update failed: %s", e)
        finally:
            self._notes_running = False
        # long backlog: keep folding chunks in, but only while making progress
        if final and progressed and self.mem.notes_cursor < len(self.mem.utterances):
            await self.update_notes(final=True)

    def _merge(self, d: dict) -> None:
        n = self.notes
        if d.get("topic") and not n.get("topic"):
            n["topic"] = d["topic"]
        for o in d.get("objectives", []):
            if all(fuzz.ratio(o, x) < 80 for x in n["objectives"]):
                n["objectives"].append(o)
        if d.get("summary_update"):
            n["summary"] = (n.get("summary", "") + " " + d["summary_update"]).strip()
            self.mem.lesson_summary = n["summary"][-1200:]
        for sec in SECTIONS:
            for item in d.get(sec, []) or []:
                k = _key(item)
                if not k:
                    continue
                dup = next((x for x in n[sec] if fuzz.token_set_ratio(_key(x), k) > 88), None)
                if dup is None:
                    n[sec].append(item)
                elif sec in ("definitions", "questions") and isinstance(item, dict) and item.get("source", "") == "teacher":
                    dup.update(item)  # teacher wording overrides an inferred version
        for f in d.get("formulas", []) or []:
            if f["formula"] not in self.mem.formulas:
                self.mem.formulas.append(f["formula"])
                if self.db:
                    self.db.add_formula(self.mem.subject_id, self.session_id, f["formula"], "", f.get("meaning", ""))
        for t in d.get("definitions", []) or []:
            self.mem.key_terms[t["term"]] = t["definition"]

    def persist(self) -> None:
        if self.db and self.session_id:
            self.db.save_notes(self.session_id, self.notes, notes_markdown(self.notes), self.mem.subject_id,
                               self.notes.get("topic") or self.mem.topic or "Lesson", self.notes_edited)

    # -------------------------------------------------------------------- tasks
    async def extract_task(self, text: str) -> dict | None:
        cur = self.mem.current_task
        cur_s = json.dumps({k: cur.get(k) for k in ("description", "source", "questions", "time_minutes", "deadline")}) if cur else "null"
        # instructions are often split across utterances ("...questions 1 to 6." / "You have 15 minutes.")
        # and queued extraction jobs coalesce, so give the model everything said since the last extraction
        since = max(self._last_task_extract, time.time() - 45)
        new_speech = " ".join(u.text for u in self.mem.utterances if u.source == "class" and u.t1 >= since) or text
        earlier = "\n".join(f"Teacher: {u.text}" for u in self.mem.utterances
                            if u.source == "class" and time.time() - 120 <= u.t1 < since)[-1200:]
        self._last_task_extract = time.time()
        msgs = [{"role": "system", "content": P.SYSTEM_TASK.format(current=cur_s)},
                {"role": "user", "content": f"EARLIER SPEECH (context only):\n{earlier or '(none)'}\n\nNEW SPEECH: {new_speech}"}]

        async def job():
            return await self.llm.chat(self.model, msgs, think=False, num_ctx=self.num_ctx, num_predict=300,
                                       temperature=0.1, fmt=TASK_SCHEMA)

        try:
            raw = await self.sched.submit(TASK, job, key="task", max_age=30)
            t = json.loads(raw)
        except asyncio.CancelledError:
            reraise_if_cancelled()
            return None
        except json.JSONDecodeError:
            return None
        except Exception as e:
            log.warning("task extraction failed: %s", e)
            return None
        if not t.get("is_task") or not t.get("description"):
            return None
        now = time.time()
        rel = t.get("relation", "new")
        if cur and rel in ("supplement", "update"):
            task = dict(cur)
            if rel == "update":
                for k in ("source", "questions", "first_question", "time_minutes", "deadline"):
                    if t.get(k) is not None:
                        task[k] = t[k]
            else:
                task.setdefault("extra", []).append({k: t.get(k) for k in ("description", "source", "questions", "deadline")})
            task["instructions"] = list(dict.fromkeys((task.get("instructions") or []) + (t.get("instructions") or [])))
        else:
            if cur:
                cur["status"] = "replaced"
                cur["ended_at"] = now
                self.tasks_history.append(cur)
                if self.db:
                    self.db.save_task(cur)
            task = {"id": "t_" + uuid.uuid4().hex[:10], "session_id": self.session_id, "subject_id": self.mem.subject_id,
                    "created_at": now, "status": "active", **{k: t.get(k) for k in TASK_SCHEMA["properties"] if k != "is_task"}}
        if task.get("deadline") and re.search(r"\b(minute|min|hour|second)s?\b", str(task["deadline"]), re.I):
            task["deadline"] = None  # a duration is a time limit, not a due date
        # drop "instructions" that merely restate the page / questions / time
        restated = re.compile(r"^(open|turn|go) (your|to)|^(do|answer|complete) (questions?|numbers?)|^you have \w+ minutes", re.I)
        task["instructions"] = [i for i in (task.get("instructions") or []) if not restated.search(i.strip())]
        task["relation"] = rel
        task["updated_at"] = now
        task["evidence"] = text
        if t.get("timer_started") and t.get("time_minutes"):
            task["timer_start"] = now
            task["timer_end"] = now + float(t["time_minutes"]) * 60
        self.mem.current_task = task
        if self.db:
            self.db.save_task(task)
        self.emit("task.update", task=task, history=self.tasks_history[-10:])
        self._after()
        return task

    def complete_task(self, task_id: str) -> None:
        t = self.mem.current_task
        if t and t["id"] == task_id:
            t["status"] = "done"
            t["completed_at"] = time.time()
            self.tasks_history.append(t)
            self.mem.current_task = None
            if self.db:
                self.db.save_task(t)
            self.emit("task.update", task=None, history=self.tasks_history[-10:])

    def start_timer(self, task_id: str) -> None:
        t = self.mem.current_task
        if t and t["id"] == task_id and t.get("time_minutes"):
            t["timer_start"] = time.time()
            t["timer_end"] = t["timer_start"] + float(t["time_minutes"]) * 60
            self.emit("task.update", task=t, history=self.tasks_history[-10:])

    # --------------------------------------------------------------- flashcards
    async def flashcards(self, session_id: str | None = None) -> list[dict]:
        sid = session_id or self.session_id
        notes = self.notes if sid == self.session_id else (self.db.get_notes(sid) or {}).get("data", {})
        if not notes:
            return []
        md = notes_markdown(notes)
        msgs = [{"role": "system", "content": P.SYSTEM_FLASHCARDS},
                {"role": "user", "content": f"NOTES:\n{md[:6000]}\n\nMake 6-15 concise cards (definitions, formulas, key facts)."}]

        async def job():
            return await self.llm.chat(self.model, msgs, think=False, num_ctx=self.num_ctx, num_predict=1200,
                                       temperature=0.2, fmt=CARDS_SCHEMA)

        raw = await self.sched.submit(REVISION, job, key=f"cards:{sid}")
        cards = [c for c in json.loads(raw).get("cards", []) if c.get("front") and c.get("back")]
        subj = (self.db.get_session(sid) or {}).get("subject_id") if self.db else None
        return self.db.add_flashcards(sid, subj, cards) if self.db else cards
