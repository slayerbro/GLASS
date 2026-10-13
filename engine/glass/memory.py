"""Three-layer lesson memory.

Layer 1 — immediate: last ~90 s of speech, latest screen observation(s), the
          active question and the current task.
Layer 2 — lesson:    running state (subject/topic/summary), key terms, formulas,
          questions asked, all transcript text (for notes and catch-up).
Layer 3 — subject:   past notes in SQLite (FTS5), retrieved by keyword, same
          subject only, and always ranked below current-lesson material.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .mathsolve import find_problems, word_to_int


@dataclass
class Utterance:
    id: str
    source: str  # class | me
    text: str
    t0: float
    t1: float


@dataclass
class ScreenObs:
    t: float
    text: str
    items: dict[int, str] = field(default_factory=dict)  # numbered questions on screen
    equations: list[str] = field(default_factory=list)
    title: str = ""
    vision: str = ""  # description from the vision model, if any
    source: str = "screen"  # screen | snip


ITEM_RE = re.compile(r"^\s*(?:(?:Q(?:uestion)?|Ex(?:ercise)?)\s*(\d{1,2})\s*[.):]?|(\d{1,2})\s*[.):])\s+(.+)$", re.I)
ITEM_PAREN = re.compile(r"^\s*\((\d{1,2})\)\s+(.+)$")


def parse_numbered_items(text: str) -> dict[int, str]:
    """Group OCR lines into numbered questions: '3. Solve 2x+7=19' -> {3: 'Solve 2x+7=19'}."""
    items: dict[int, str] = {}
    current: int | None = None
    for line in text.splitlines():
        m = ITEM_RE.match(line)
        if m:
            n, body = int(m.group(1) or m.group(2)), m.group(3)
        else:
            m = ITEM_PAREN.match(line)
            n, body = (int(m.group(1)), m.group(2)) if m else (0, "")
        if m and 0 < n < 60:
            current = n
            items[n] = body.strip()
            continue
        if current is not None and line.strip() and len(items[current]) < 400:
            # continuation lines (parts a/b, wrapped text) until the next number
            if re.match(r"^\s*\(?[a-h]\)\s", line) or not re.match(r"^\s*[A-Z][a-z]+ [a-z]+ [a-z]+", line) or len(line) < 60:
                items[current] += "\n" + line.strip()
    return items


class LessonMemory:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.utterances: list[Utterance] = []
        self.screen: deque[ScreenObs] = deque(maxlen=12)
        self.subject_id: str | None = None
        self.subject_locked = False
        self.topic = ""
        self.subtopic = ""
        self.summary = ""  # one-line "what's happening now"
        self.lesson_summary = ""  # running multi-line summary (from notes engine)
        self.key_terms: dict[str, str] = {}
        self.formulas: list[str] = []
        self.questions: list[dict] = []  # {id, t, text, kind, directed, answer_say, teacher_answer}
        self.current_task: dict | None = None
        self.active_question: dict | None = None
        self.clarifications: dict[str, list[str]] = {}  # question id -> student's extra context
        self.started = time.time()
        self.last_speech = 0.0
        self.chars_since_state = 0
        self.notes_cursor = 0  # index into utterances already folded into notes

    # -- layer 1 --------------------------------------------------------------
    def add_utterance(self, u: Utterance) -> None:
        self.utterances.append(u)
        self.last_speech = u.t1
        self.chars_since_state += len(u.text)

    def recent(self, seconds: float = 90, max_chars: int = 2500, include_me: bool = True) -> str:
        now = time.time()
        lines = []
        total = 0
        for u in reversed(self.utterances):
            if now - u.t1 > seconds and lines:
                break
            if u.source == "me" and not include_me:
                continue
            who = "Student" if u.source == "me" else "Teacher"
            line = f"{who}: {u.text}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(reversed(lines))

    def window(self, seconds: float) -> list[Utterance]:
        now = time.time()
        return [u for u in self.utterances if now - u.t1 <= seconds]

    def add_screen(self, obs: ScreenObs) -> None:
        # numbered items persist across observations via screen_items()
        self.screen.append(obs)

    def latest_screen(self) -> ScreenObs | None:
        return self.screen[-1] if self.screen else None

    def screen_items(self) -> dict[int, str]:
        """Union of numbered items seen recently, newest wins."""
        items: dict[int, str] = {}
        cutoff = time.time() - 15 * 60
        for obs in self.screen:
            if obs.t >= cutoff:
                items.update(obs.items)
        return items

    def resolve_reference(self, ref: dict) -> str | None:
        items = self.screen_items()
        if not items:
            return None
        if "question_number" in ref:
            n = ref["question_number"]
            text = items.get(n)
            if text and ref.get("part"):
                m = re.search(rf"\(?{ref['part']}\)\s*(.+?)(?=\n\s*\(?[a-h]\)|\Z)", text, re.S)
                if m:
                    return f"{n}({ref['part']}) {m.group(1).strip()}"
            return f"{n}. {text}" if text else None
        if "ordinal" in ref:
            keys = sorted(items)
            o = ref["ordinal"]
            if o == "last":
                return f"{keys[-1]}. {items[keys[-1]]}"
            if o == "next" and self.active_question and self.active_question.get("ref_n") in items:
                nxt = [k for k in keys if k > self.active_question["ref_n"]]
                return f"{nxt[0]}. {items[nxt[0]]}" if nxt else None
            n = word_to_int(o)
            if n and n in items:
                return f"{n}. {items[n]}"
        return None

    def screen_context(self, max_chars: int = 1800) -> str:
        obs = self.latest_screen()
        if not obs:
            return ""
        parts = []
        if obs.title:
            parts.append(f"[window: {obs.title}]")
        if obs.vision:
            parts.append(f"[visual description] {obs.vision}")
        parts.append(obs.text.strip())
        txt = "\n".join(p for p in parts if p)
        return txt[:max_chars]

    def candidate_problems(self, extra: list[str]) -> list[str]:
        out = []
        for t in extra:
            out += find_problems(t)
        return out

    # -- layer 2 --------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "topic": self.topic,
            "subtopic": self.subtopic,
            "summary": self.summary,
        }

    def add_question(self, q: dict) -> None:
        self.questions.append(q)
        self.questions = self.questions[-60:]

    def transcript_since(self, t: float) -> str:
        return "\n".join(f"{'Student' if u.source == 'me' else 'Teacher'}: {u.text}" for u in self.utterances if u.t1 >= t)

    def full_transcript_chars(self) -> int:
        return sum(len(u.text) for u in self.utterances)
