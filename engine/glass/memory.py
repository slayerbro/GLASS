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
    ctx: str | None = None  # compacted text for the AI (noise removed); "" = pure noise

    @property
    def useful(self) -> str:
        return self.text if self.ctx is None else self.ctx


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
# "2(a) Work out…", "2 (a) …", "2a) …" — exam papers put the first part on the number's line
ITEM_PART_RE = re.compile(r"^\s*(?:Q(?:uestion)?\s*)?(\d{1,2})\s*(?:\(([a-h])\)|([a-h])[.)])\s*(.*)$", re.I)
# "2  Work out…" — exam papers print the number with no full stop
ITEM_BARE_RE = re.compile(r"^\s*(\d{1,2})\s{1,4}(?=[A-Za-z(])(.+)$")
PART_RE = re.compile(r"^\s*(?:\(([a-h])\)|([a-h])[.)])\s*(.*)$")
SUBPART_RE = re.compile(r"^\s*\((i{1,3}|iv|vi{0,3})\)\s*(.*)$")
# answer lines and mark allocations on printed worksheets
_PAPER_NOISE = re.compile(r"^\s*(\.{4,}|_{4,}|\(total for question.*|\(\d+\s*marks?\)|\(\d\)|answer\s*\.{3,}.*)\s*$", re.I)
_MARKS_TAIL = re.compile(r"\s+\(\d+\s*(marks?)?\)\s*$", re.I)


def label_key(label: str) -> str:
    """'Q2 (b)' / '2b' / '2(b)' -> '2b' so labels written differently still match."""
    return re.sub(r"^(q|question)", "", re.sub(r"[^0-9a-z]", "", label.lower()))


def split_questions(text: str) -> list[dict]:
    """Split worksheet text into separate questions, keeping parts apart.

    "1. Solve 2x+7=19\\n2 f(x)=x^2\\n(a) Find f(3)\\n(b) Solve f(x)=9" ->
      [{label: "1", text: "Solve 2x+7=19"},
       {label: "2(a)", text: "Find f(3)", stem: "f(x)=x^2"},
       {label: "2(b)", text: "Solve f(x)=9", stem: "f(x)=x^2"}]
    Text with no numbering is one question. Preamble ("Solve these equations:")
    becomes the stem of the questions that follow it."""
    numbers: list[dict] = []  # {n, stem, parts: [[letter, text]]}
    preamble: list[str] = []
    cur: dict | None = None

    def open_number(n: int, rest: str) -> dict:
        q = {"n": n, "stem": rest.strip(), "parts": []}
        numbers.append(q)
        return q

    for raw in text.splitlines():
        line = raw.strip()
        if not line or _PAPER_NOISE.match(line):
            continue
        line = _MARKS_TAIL.sub("", line)
        last_n = numbers[-1]["n"] if numbers else 0
        m = ITEM_PART_RE.match(line)
        if m and 0 < int(m.group(1)) < 60:
            cur = open_number(int(m.group(1)), "")
            cur["parts"].append([(m.group(2) or m.group(3)).lower(), m.group(4).strip()])
            continue
        m = ITEM_RE.match(line)
        mp = None if m else ITEM_PAREN.match(line)
        if m or mp:
            n, body = (int(m.group(1) or m.group(2)), m.group(3)) if m else (int(mp.group(1)), mp.group(2))
            if 0 < n < 60 and not (mp and numbers):  # "(2)" after a question is a mark
                cur = open_number(n, body)
                pm = PART_RE.match(cur["stem"])  # "Q3 a) speed"
                if pm:
                    cur["stem"] = ""
                    cur["parts"].append([(pm.group(1) or pm.group(2)).lower(), pm.group(3).strip()])
                continue
        m = ITEM_BARE_RE.match(line)
        if m and (int(m.group(1)) == last_n + 1 or (not numbers and int(m.group(1)) <= 3)):
            cur = open_number(int(m.group(1)), m.group(2))
            continue
        m = PART_RE.match(line)
        if m and (cur is not None or not numbers):
            if cur is None:  # a selection that starts at a part: "(b) Find…"
                cur = open_number(0, "")
            cur["parts"].append([(m.group(1) or m.group(2)).lower(), m.group(3).strip()])
            continue
        if cur is None:
            preamble.append(line)
        elif cur["parts"]:
            cur["parts"][-1][1] = (cur["parts"][-1][1] + "\n" + line).strip()
        else:
            cur["stem"] = (cur["stem"] + "\n" + line).strip()

    # no lettered parts, but a row of blanks to fill in ("fg(2) = ?", "gf(4) = ?"): each is a part
    for q in numbers:
        if not q["parts"]:
            rest, blanks = _fill_ins(q["stem"])
            if len(blanks) >= 2:
                q["stem"], q["parts"] = rest, [[chr(97 + i), b] for i, b in enumerate(blanks[:20])]
    if not numbers:
        rest, blanks = _fill_ins("\n".join(preamble))
        if len(blanks) >= 2:
            return [{"label": f"({chr(97 + i)})", "text": b, "stem": rest} for i, b in enumerate(blanks[:20])]
    pre = "\n".join(preamble).strip()
    if not numbers:
        return [{"label": "1", "text": pre}] if pre else []
    out: list[dict] = []
    for q in numbers:
        stem = "\n".join(s for s in (pre, q["stem"]) if s)
        if not q["parts"]:
            out.append({"label": str(q["n"]), "text": q["stem"], "stem": pre})
            continue
        for letter, body in q["parts"]:
            label = f"{q['n']}({letter})" if q["n"] else f"({letter})"
            out.append({"label": label, "text": body, "stem": stem})
    return [q for q in out if q["text"] or q["stem"]]


# "fg(2) = ?", "gf (x)=|", "3 + 4 × 2 =": a blank to fill in. The answer box often reads as ? | _ [ ]
_FILL = re.compile(r"^\s*(?P<e>[^=]{1,40}?)\s*=\s*[?|_.\[\]\s]*$")
_FILL_CALL = re.compile(r"([A-Za-z0-9]{1,3}\s*\([^()=]{1,12}\))\s*=\s*[?|_\[\]]*")


def _blank(e: str) -> bool:
    """Is the left side of "... =" a maths expression (not a sentence like "Total marks =")?"""
    return len(e.split()) <= 5 and not re.search(r"[A-Za-z]{5,}", e) and bool(re.search(r"[\d(]", e))


def _fill_ins(text: str) -> tuple[str, list[str]]:
    """-> (the other lines, the fill-in blanks in reading order). Two blanks side by side
    on one OCR line ("fg(2) = ?   gf(4) = ?") are split."""
    rest, blanks = [], []
    for line in text.splitlines():
        calls = _FILL_CALL.findall(line)
        if len(calls) >= 2 and not re.sub(_FILL_CALL, "", line).strip(" ?|_[]"):
            blanks += [f"{c.strip()} =" for c in calls]
            continue
        m = _FILL.match(line)
        if m and _blank(m.group("e")):
            blanks.append(f"{m.group('e').strip()} =")
        else:
            rest.append(line)
    return "\n".join(rest).strip(), blanks


def questions_text(qs: list[dict]) -> str:
    """Questions back to plain text, one block per question (for prompts)."""
    blocks, last_stem = [], None
    for q in qs:
        stem = q.get("stem") or ""
        if stem and stem != last_stem:
            blocks.append(stem)
        last_stem = stem
        blocks.append(f"[{q['label']}] {q['text']}".rstrip())
    return "\n".join(blocks)


def parse_numbered_items(text: str) -> dict[int, str]:
    """Group OCR lines into numbered questions: '3. Solve 2x+7=19' -> {3: 'Solve 2x+7=19'}."""
    items: dict[int, str] = {}
    current: int | None = None
    for line in text.splitlines():
        mp = ITEM_PART_RE.match(line)
        if mp and 0 < int(mp.group(1)) < 60:  # "2(a) Find…" keeps its part letter for references
            current = int(mp.group(1))
            items[current] = f"({(mp.group(2) or mp.group(3)).lower()}) {mp.group(4).strip()}"
            continue
        m = ITEM_RE.match(line)
        if m:
            n, body = int(m.group(1) or m.group(2)), m.group(3)
        else:
            m = ITEM_PAREN.match(line)
            n, body = (int(m.group(1)), m.group(2)) if m else (0, "")
            mb = None if m else ITEM_BARE_RE.match(line)
            if mb and int(mb.group(1)) == (current or 0) + 1:  # "2  Work out…" (no full stop)
                m, n, body = mb, int(mb.group(1)), mb.group(2)
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
        self.topic_locked = False  # the student told GLASS the topic: background tracking keeps it
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
            if not u.useful:
                continue  # register, tech chatter, small talk: not lesson context
            line = f"{who}: {u.useful}"
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
                want = f"{n}{ref['part']}"
                for q in split_questions(f"{n}. {text}"):
                    if label_key(q["label"]) == want:
                        stem = f"{q['stem']}\n" if q.get("stem") else ""
                        return f"{n}({ref['part']}) {stem}{q['text']}".strip()
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
            "topic_locked": self.topic_locked,
        }

    def add_question(self, q: dict) -> None:
        self.questions.append(q)
        self.questions = self.questions[-60:]

    def transcript_since(self, t: float) -> str:
        return "\n".join(f"{'Student' if u.source == 'me' else 'Teacher'}: {u.useful}" for u in self.utterances
                         if u.t1 >= t and u.useful)

    def full_transcript_chars(self) -> int:
        return sum(len(u.text) for u in self.utterances)
