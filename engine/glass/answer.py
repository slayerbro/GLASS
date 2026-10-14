"""Answer engine: turns a detected question (or a student request) into a
fast, grounded, verified suggestion.

Pipeline for a teacher question:
  1. resolve references ("question 3" -> on-screen text) and gather context
  2. deterministic pre-solve of any equation involved (SymPy, ~10 ms)
  3. stream the LLM answer in a strict line format, rendering progressively
  4. intercept the CALC line, compute it exactly, feed RESULT back (prefill)
  5. optional background verification pass that can revise the answer
Every card carries confidence, assumptions and its sources; the UI never
shows an unfinished answer as verified.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any, Callable

from rapidfuzz import fuzz

from . import prompts as P
from .detect import NameHit, QuestionSignal, content_words, extract_reference, question_kind
from .texts import QUOTE_REQUEST, find_quotes, normalise as norm_text, quote_in
from .llm import ANSWER, DIRECT, REVISION, TASK, VISION, LLMError, OllamaClient, Scheduler, reraise_if_cancelled
from .mathsolve import (MathService, find_problems, fix_italic_g, flat_powers, function_defs, normalise as norm_math,
                        plan_math, tidy_result)
from .memory import LessonMemory, ScreenObs, label_key, parse_numbered_items, questions_text, split_questions

log = logging.getLogger("glass.answer")

# What the student types into the context box can be an instruction, not context.
COMMANDS = [
    ("answer", re.compile(r"\b(just |pls |please )?(answer|solve)( it| this| that| the (question|q))?\b|give (me )?(the |an |a )?"
                          r"(answer|straight answer)|what('s| is) the answer|tell me the answer|actual answer|not explain", re.I)),
    ("shorter", re.compile(r"\b(shorter|short|briefly|brief|one line|one sentence|tl;?dr|summari[sz]e|less)\b", re.I)),
    ("simpler", re.compile(r"\b(simpler|simple|easier|eli5|plain english|don'?t understand|confus)", re.I)),
    ("detailed", re.compile(r"\b(more detail|detailed|longer|elaborate|expand|explain more|in depth|full answer)\b", re.I)),
    ("quotes", re.compile(r"\b(quotes?|quotations?|evidence|textual)\b", re.I)),
    ("example", re.compile(r"\b(an example|examples?|for instance)\b", re.I)),
    ("wrong", re.compile(r"\b(wrong|incorrect|not right|that'?s not it|no that'?s|mistake)\b", re.I)),
]
COMMAND_TEXT = {
    "answer": "Give THE ANSWER itself in SAY — state it directly. Do not restate, describe or explain the question.",
    "shorter": "Make SAY at most 15 words and WHY one short sentence.",
    "simpler": "Use simpler words a younger student would understand.",
    "quotes": "Give 1-3 short exact quotations as evidence (copied word-for-word from the provided text passages), "
              "each followed by a few words on what it shows.",
    "example": "Include one concrete, specific example in SAY.",
    "wrong": "The student says your previous answer was wrong. Re-read the question and context, consider a different "
             "interpretation or fix the error, and give a corrected answer.",
}
_FILLER_WORDS = re.compile(r"\b(it|this|that|the|question|q|please|pls|just|me|give|a|an|answer|solve|now|again|can|you|"
                           r"make|be|more|less|and|then|so|ok|okay|no|not|that's|its|it's)\b", re.I)

# a "spoken answer" that only describes the question is not an answer
META_SAY = re.compile(r"^\s*(the (teacher|question) (is )?(ask|want)|this question|the question (is|asks|wants)|to answer (this|the)|"
                      r"you (need|have|should) to|it('s| is) asking|they('re| are) asking|i (need|would need|can'?t)|"
                      r"let me|we need to|in order to answer|the answer (depends|would depend))", re.I)


# "the topic is concentration", "we're doing moles", "this lesson is about natural selection"
TOPIC_STATEMENT = re.compile(
    r"^\s*(?:(?:the|this|our|today'?s)\s+)?(?:lesson|topic|class|subject|unit|chapter|lecture)(?:\s+today)?\s*"
    r"(?:is|'s|=|:|-)\s*(?:about\s+|on\s+|called\s+)?(?P<a>.+)$"
    r"|^\s*(?:it'?s|this is)\s+(?:a|the)\s+(?:lesson|class)\s+(?:about|on)\s+(?P<b>.+)$"
    r"|^\s*(?:we'?re|we are|we'?ve been|the teacher is|they'?re)\s+(?:doing|learning(?:\s+about)?|studying|covering|"
    r"talking about|on)\s+(?P<c>.+)$",
    re.I)
SUBJECT_WORDS = [
    ("english literature", "english_lit"), ("english lit", "english_lit"), ("english language", "english_lang"),
    ("computer science", "cs"), ("design and technology", "dt"), ("design & technology", "dt"),
    ("mathematics", "maths"), ("maths", "maths"), ("math", "maths"), ("biology", "biology"), ("bio", "biology"),
    ("chemistry", "chemistry"), ("chem", "chemistry"), ("physics", "physics"), ("business studies", "business"),
    ("business", "business"), ("computing", "cs"), ("literature", "english_lit"), ("arabic", "arabic"),
]


def parse_topic(text: str) -> tuple[str | None, str | None]:
    """-> (topic, subject_id) when the student tells GLASS what the lesson is about."""
    m = TOPIC_STATEMENT.match(text.strip())
    if not m:
        return None, None
    rest = (m.group("a") or m.group("b") or m.group("c") or "").strip(" .!,;:")
    rest = re.sub(r"\s+(today|now|right now|in this lesson)$", "", rest, flags=re.I)
    subject = None
    for word, sid in SUBJECT_WORDS:
        sm = re.search(rf"(?:^|\s*(?:\bin\b|\bfor\b|\bfrom\b|,|-|\())\s*\b{re.escape(word)}\b\)?", rest, re.I)
        if sm:
            subject = sid
            rest = (rest[:sm.start()] + " " + rest[sm.end():]).strip(" .,-()")
            rest = re.sub(r"^(a|an|the)\s+(lesson|class)\b\s*(about|on)?\s*", "", rest, flags=re.I).strip()
            break
    rest = re.sub(r"\s+", " ", rest)[:60]
    return (rest or None), subject


_HEAD = re.compile(r"^[ \t]*(?:#{1,4}[ \t]*|\*\*[ \t]*)(?:question|q)?[ \t]*\[?(\d{1,2}[ \t]*(?:\(?[a-h]\)?)?(?:[ \t]*\(?(?:i{1,3}|iv|vi{0,3})\))?"
                   r"|\(?[a-h]\))\]?[ \t]*[.:)]?[ \t]*(?:\*\*)?[ \t]*$", re.I | re.M)


def _paragraphs(md: str) -> str:
    """One line = one line: the model writes working on separate lines, but Markdown
    joins single line breaks into one run-on paragraph."""
    segs = md.strip().split("$$")  # odd segments are display maths: their line breaks stay
    return "$$".join(re.sub(r"(?<!\n)\n(?!\n)", "\n\n", x) if i % 2 == 0 else x for i, x in enumerate(segs))


def pair_parts(questions: list[dict], body: str) -> list[dict]:
    """Split the model's answer into one block per question, matched by label, so each
    question is shown with its own answer. [] when the answer has no blocks."""
    known = {label_key(q["label"]) for q in questions}
    # "### 3" is always a heading; a bold "**12**" line only when 12 is one of the questions
    heads = [h for h in _HEAD.finditer(body) if h.group(0).lstrip().startswith("#") or label_key(h.group(1)) in known]
    if not heads:
        if len(questions) == 1 and body.strip():
            return [{**questions[0], "answer": _paragraphs(body)}]
        return []
    blocks: dict[str, str] = {}
    order: list[tuple[str, str]] = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        label = re.sub(r"\s+", "", h.group(1))
        k = label_key(label)
        if len(questions) == 1 and k not in known:
            k = label_key(questions[0]["label"])
        blocks[k] = (blocks.get(k, "") + "\n" + body[h.end():end]).strip()
        order.append((k, label))
    out, used = [], set()
    for q in questions:
        k = label_key(q["label"])
        out.append({**q, "answer": _paragraphs(blocks.get(k, ""))})
        used.add(k)
    for k, label in order:  # answers to questions we didn't split out ourselves
        if k not in used:
            used.add(k)
            out.append({"label": label, "text": "", "answer": _paragraphs(blocks[k])})
    return out


def parse_commands(text: str) -> tuple[list[str], str]:
    """-> (commands, leftover context). "answer it, it's about natural selection" ->
    (["answer"], "it's about natural selection")."""
    cmds = [name for name, rx in COMMANDS if rx.search(text)]
    if not cmds:
        return [], text
    rest = text
    for _, rx in COMMANDS:
        rest = rx.sub(" ", rest)
    rest = re.sub(r"\s+", " ", _FILLER_WORDS.sub(" ", rest)).strip(" ,.;:-")
    leftover = text if len(rest.split()) >= 2 else ""
    if cmds and leftover:
        # keep the context part only (drop the command phrase itself)
        leftover = re.sub(r"^.*?(about|it'?s|its|is|on|regarding|means?|refers to)\s+", "", text, flags=re.I) if re.search(
            r"\b(about|it'?s|is|on|regarding|refers to)\b", text, re.I) else text
    return cmds, leftover


FIELD_RE = re.compile(r"^\s*(Q|TYPE|CONF|UNSURE|ASK|CALC|RESULT|SAY|WHY)\s*:\s*(.*)$")
NULLISH = {"", "-", "->", "none", "n/a", "null", "nothing", "no"}


def _clean(v: str) -> str:
    return v.strip().strip("`").strip()


class AnswerEngine:
    def __init__(self, llm: OllamaClient, sched: Scheduler, math: MathService, memory: LessonMemory, settings, db,
                 emit: Callable[..., None]):
        self.llm = llm
        self.sched = sched
        self.math = math
        self.mem = memory
        self.settings = settings
        self.db = db
        self.emit = emit
        self.answers: dict[str, dict] = {}
        self.session_id: str | None = None
        self.text_model = "qwen3:4b-instruct"
        self.vision_model = "qwen3-vl:4b-instruct"
        self.num_ctx = 8192
        self._last_emit: dict[str, float] = {}
        self.last_llm_use = time.time()
        self._corpus: dict[str, list[tuple[str, str]]] = {}  # subject -> [(title, normalised text)]
        self.on_answer = None  # engine hook after each finished answer
        self.on_topic = None  # engine hook: the student said what the lesson is about
        self._last_snip: tuple[str, bytes] | None = None  # (answer id, image) for "read again with vision"

    # ------------------------------------------------------------------ helpers
    @property
    def name(self) -> str:
        return self.settings.get("student_name") or "the student"

    def _subject_line(self) -> str:
        s = self.settings.subject_by_id(self.mem.subject_id) if self.mem.subject_id else None
        subj = f"{s['name']} ({s['board']})" if s else "unknown subject"
        topic = self.mem.topic or "not yet clear"
        if self.mem.subtopic:
            topic += f" / {self.mem.subtopic}"
        if self.mem.topic_locked:
            topic += f"  ({self.name} said this is the topic — interpret every question within it)"
        return f"LESSON: {subj} — topic: {topic}"

    def _push(self, ans: dict, force: bool = False) -> None:
        if self.answers.get(ans["id"]) is not ans:
            return  # cleared when the lesson ended: a late update must not bring the card back
        now = time.perf_counter()
        if not force and now - self._last_emit.get(ans["id"], 0) < 0.05:
            return
        self._last_emit[ans["id"]] = now
        self.emit("answer.update", answer=self._public(ans))

    @staticmethod
    def _public(ans: dict) -> dict:
        return {k: v for k, v in ans.items() if not k.startswith("_")}

    def _new(self, mode: str, **kw: Any) -> dict:
        aid = "a_" + uuid.uuid4().hex[:10]
        ans = {
            "id": aid, "mode": mode, "t": time.time(), "status": "thinking", "question": "", "resolved": "",
            "kind": "", "conf": "", "unsure": "", "calc": "", "result": "", "verified": False, "say": "", "why": "",
            "detailed": "", "body": "", "directed": None, "waiting": False, "rhetorical": False, "sources": [],
            "clarifications": [], "latency": {}, "error": "", "revised": False, "level": "quick",
            "subject_id": self.mem.subject_id,
        }
        ans.update(kw)
        self.answers[aid] = ans
        # keep memory bounded
        if len(self.answers) > 80:
            for k in list(self.answers)[:20]:
                self.answers.pop(k, None)
        return ans

    def _context_block(self, ans: dict, question: str, extra: list[str], ref_text: str | None,
                       verified: list[tuple[str, str]], max_chars: int, for_answer: bool = False) -> str:
        parts = [self._subject_line()]
        if self.mem.lesson_summary:
            parts.append("LESSON SO FAR: " + self.mem.lesson_summary[-700:])
        if self.mem.formulas:
            parts.append("FORMULAS USED THIS LESSON: " + "; ".join(self.mem.formulas[-8:]))
        if self.mem.current_task:
            t = self.mem.current_task
            parts.append(f"CURRENT TASK: {t.get('description', '')} {t.get('source', '') or ''} {t.get('questions', '') or ''}".strip())
        past = self._retrieve(question + " " + " ".join(extra))
        if past:
            parts.append("FROM EARLIER LESSONS (same subject; lower priority than this lesson):\n" + past)
        passages = self._text_passages(question + " " + " ".join(extra), wants_quotes=bool(
            QUOTE_REQUEST.search(question + " " + " ".join(extra))), budget=int(max_chars * 0.6))
        if passages:
            parts.append(passages)
            ans["sources"].append("your texts")
        screen = self.mem.screen_context(max_chars=int(max_chars * 0.45))
        if screen:
            parts.append("SCREEN TEXT (OCR of the shared screen; may contain recognition errors):\n" + screen)
            ans["sources"].append("screen")
        if ref_text:
            parts.append(f'THE QUESTION REFERS TO THIS ON-SCREEN ITEM: "{ref_text}"')
            ans["sources"].append("screen item")
        for expr, val in verified:
            hint = "  (already computed: write CALC: - and use this)" if for_answer else ""
            parts.append(f"VERIFIED RESULT (computed exactly by SymPy): {expr}  =>  {val}{hint}")
        recent = self.mem.recent(seconds=90, max_chars=int(max_chars * 0.45))
        if recent:
            parts.append("RECENT SPEECH (oldest first, automatic transcription):\n" + recent)
            ans["sources"].append("transcript")
        prefs = self.settings.get("answer_prefs") or []
        if prefs and for_answer:
            parts.append(f"{self.name.upper()}'S PREFERENCES (learned from feedback): " + "; ".join(prefs))
        if extra:
            parts.append(f"{self.name.upper()}'S EXTRA CONTEXT FOR THIS QUESTION (authoritative — use it to interpret the "
                         f"question; it may also correct your previous interpretation): " + " | ".join(f'"{e}"' for e in extra))
        return "\n\n".join(parts)

    # ---------------------------------------------------------------- study texts
    def invalidate_texts(self) -> None:
        self._corpus.clear()

    def _text_passages(self, query: str, wants_quotes: bool, budget: int) -> str:
        """Most relevant passages from the student's own copies of set texts."""
        if not self.db:
            return ""
        topic = f"{self.mem.topic} {self.mem.subtopic}"
        words = content_words(query) + content_words(topic)
        if not words:
            return ""
        subj = self.mem.subject_id if self.mem.subject_id and self.db.texts(self.mem.subject_id) else None
        hits = self.db.search_texts(words, subj, limit=6 if wants_quotes else 3)
        if not hits:
            return ""
        out, used = [], 0
        for h in hits:
            body = h["content"]
            if used + len(body) > budget and out:
                break
            where = f"{h['title']}" + (f", {h['chapter']}" if h["chapter"] else "")
            out.append(f"[{where}]\n{body}")
            used += len(body)
        return ("PASSAGES FROM THE STUDENT'S OWN COPY OF THE TEXT (exact wording — quotations may ONLY be copied from "
                "here or from the screen):\n" + "\n---\n".join(out))

    def _corpus_for(self, subject_id: str | None) -> list[tuple[str, str]]:
        key = subject_id or "*"
        if key not in self._corpus and self.db:
            rows = self.db.texts(subject_id) if subject_id else self.db.texts()
            if subject_id and not rows:
                rows = self.db.texts()
            self._corpus[key] = [(r["title"], norm_text(self.db.text_full(r["id"]))) for r in rows]
        return self._corpus.get(key, [])

    def check_quotes(self, ans: dict) -> None:
        """Verify every quotation in an answer word-for-word against the student's texts
        and what was on screen. Unverifiable quotes are flagged, never shown as fact."""
        blob = " ".join(ans.get(k) or "" for k in ("say", "why", "body", "detailed"))
        quotes = find_quotes(blob)
        if not quotes:
            ans["quotes"] = []
            return
        corpus = self._corpus_for(self.mem.subject_id)
        screen = norm_text(" ".join(o.text for o in self.mem.screen)) if self.mem.screen else ""
        speech = norm_text(" ".join(u.text for u in self.mem.utterances[-60:]))
        out = []
        for q in quotes[:6]:
            src = next((title for title, text in corpus if quote_in(q, text)), None)
            if not src and screen and quote_in(q, screen):
                src = "the screen"
            if not src and speech and quote_in(q, speech):
                src = "what the teacher said"
            out.append({"q": q, "ok": bool(src), "source": src or "", "texts": len(corpus)})
        ans["quotes"] = out

    def _retrieve(self, query: str) -> str:
        if not self.db or not self.mem.subject_id:
            return ""
        words = [w for w in re.findall(r"[A-Za-z]{4,}", query.lower())
                 if w not in {"what", "this", "that", "does", "with", "from", "have", "your", "about", "would", "could",
                              "there", "their", "which", "sanad", "explain", "think", "question"}]
        if not words:
            return ""
        hits = [h for h in self.db.search(" ".join(words[:8]), self.mem.subject_id, limit=3) if h["session_id"] != self.session_id]
        return "\n".join(f"- {h['title']}: {h['snippet']}" for h in hits[:2])

    async def _solve(self, exprs: list[str], limit: int = 4) -> list[tuple[str, str]]:
        out = []
        for e in exprs[:limit]:
            r = await asyncio.to_thread(self.math.run, e)
            if r.ok and r.value and r.value != "no solution":
                out.append((e, r.value))
        return out

    # ---------------------------------------------------------- teacher questions
    async def on_question(self, sig: QuestionSignal, name_hit: NameHit | None, utterance_text: str) -> dict | None:
        """Entry point from the session when a question is detected in speech."""
        if sig.check:
            return None
        # teacher repeating the active question? reuse, just escalate
        active = self.mem.active_question
        if active and time.time() - active["t"] < 75 and self._same_question(sig.text, active["text"]):
            ans = self.answers.get(active.get("answer_id", ""))
            if ans:
                if name_hit and (not ans["directed"] or name_hit.confidence > ans["directed"]["confidence"]):
                    ans["directed"] = name_hit.to_dict()
                    self._push(ans, force=True)
                return ans
        qid = "q_" + uuid.uuid4().hex[:10]
        ref_n = sig.reference.get("question_number") if sig.reference else None
        self.mem.active_question = {"id": qid, "t": time.time(), "text": sig.text, "kind": sig.kind,
                                    "directed": name_hit.confidence if name_hit else 0.0, "ref_n": ref_n}
        ans = self._new("question", question=sig.text, kind=sig.kind, qid=qid,
                        directed=name_hit.to_dict() if name_hit else None, rhetorical=sig.rhetorical,
                        deictic=sig.deictic)
        self.mem.active_question["answer_id"] = ans["id"]
        self._push(ans, force=True)
        prio = DIRECT if name_hit and name_hit.confidence >= 0.45 else ANSWER
        # newest question wins: an unfinished answer to an older class question is no longer
        # useful and must not make this one wait (answers addressed to the student are kept)
        n = self.sched.cancel_where(lambda key, p: (key.startswith("answer:q_") or key.startswith("verify:"))
                                    and p != DIRECT and key != f"answer:{qid}")
        if n:
            log.info("cancelled %d stale answer job(s) for a newer question", n)
        if prio == DIRECT:
            self.sched.cancel_where(lambda key, p: key.startswith("verify:"))
        await self._run_answer(ans, sig.text, sig.reference, prio, extra=[])
        return ans

    def _same_question(self, a: str, b: str) -> bool:
        """Is the teacher repeating the question (not asking a similar one)?
        "What does RAM stand for?" vs "What does ROM stand for?" share most words
        but differ in the one that matters, so compare the meaningful words."""
        names = {x.lower() for x in [self.settings.get("student_name") or "", *(self.settings.get("name_aliases") or [])] if x}
        ca, cb = set(content_words(a, names)), set(content_words(b, names))
        if not ca or not cb:
            return False
        return ca == cb or (len(ca & cb) / len(ca | cb) >= 0.8 and fuzz.token_sort_ratio(a, b) >= 85)

    async def clarify(self, text: str) -> dict:
        """Instant context: the student typed a few words. They are either context for the
        active question ("natural selection", "question 4"), an instruction about the
        answer ("answer it", "shorter", "give quotes", "that's wrong"), a request for an
        on-screen question ("answer question 2b"), or what the lesson is about."""
        text = text.strip()
        topic, subject = parse_topic(text)
        if topic or subject:
            return await self.set_topic(text, topic, subject)
        cmds, context = parse_commands(text)
        ref = extract_reference(text)
        active = self.mem.active_question
        fresh = bool(active and time.time() - active["t"] < 300)
        last = self._last_answer()
        recent = bool(last and time.time() - last["t"] < 300)
        if ref.get("question_number") and (cmds or not fresh):
            # "answer question 2b" is a new request, not a tweak of the previous answer
            return await self.solve_reference(text, ref)
        if not fresh:
            if recent and last["mode"] in ("question", "ask"):
                if cmds == ["detailed"] and not context:
                    return await self.expand(last["id"])
                return await self._redo(last, cmds, context)
            if cmds and recent:
                return await self._redo(last, cmds, context)
            if not cmds and "?" not in text and len(text.split()) <= 4:
                # nothing to fix: a few words of context describe the lesson itself
                return await self.set_topic(text, text.strip(" .!,;:"), None)
            return await self.ask(text)
        old = self.answers.get(active.get("answer_id", ""))
        if cmds == ["detailed"] and not context and old:
            return await self.expand(old["id"])
        if cmds:
            return await self._redo(old or last, cmds, context, active=active)
        ctx = self.mem.clarifications.setdefault(active["id"], [])
        ctx.append(text)
        ans = self._new("question", question=active["text"], kind=active.get("kind", ""), qid=active["id"],
                        directed=old["directed"] if old else None, clarifications=list(ctx),
                        replaces=old["id"] if old else None)
        active["answer_id"] = ans["id"]
        self._push(ans, force=True)
        if ref.get("question_number"):  # "no, question 4" corrects which item is meant
            active["ref_n"] = ref["question_number"]
        elif active.get("ref_n"):
            ref = {"question_number": active["ref_n"]}
        await self._run_answer(ans, active["text"], ref, DIRECT, extra=list(ctx))
        return ans

    async def set_topic(self, text: str, topic: str | None, subject: str | None) -> dict:
        """The student told GLASS what the lesson is about. It sticks for the whole lesson
        (topic tracking won't overwrite it) and the answer on screen is redone with it."""
        if self.on_topic:
            self.on_topic(topic, subject)
        else:
            if subject:
                self.mem.subject_id, self.mem.subject_locked = subject, True
            if topic:
                self.mem.topic, self.mem.topic_locked = topic, True
        active = self.mem.active_question
        target = None
        if active and time.time() - active["t"] < 300:
            target = self.answers.get(active.get("answer_id", ""))
        last = self._last_answer()
        if target is None and last and last["mode"] in ("question", "ask") and last.get("kind") != "note" \
                and time.time() - last["t"] < 300:
            target = last
        if target is not None and topic:
            return await self._redo(target, [], f"the lesson topic is {topic}",
                                    active=active if active and target.get("qid") == active.get("id") else None)
        subj = (self.settings.subject_by_id(subject) or {}).get("name") if subject else None
        what = " ".join(x for x in [f"this lesson is about {topic}" if topic else "", f"({subj})" if subj and topic else
                                    f"this is a {subj} lesson" if subj else ""] if x)
        ans = self._new("ask", question=text, kind="note", conf="high", status="done",
                        say=f"Got it — {what}. I'll use that for answers and notes.")
        self._push(ans, force=True)
        return ans

    async def solve_reference(self, text: str, ref: dict) -> dict:
        """"answer question 2b": find that question on the screen (or in a snip) and solve it."""
        n, part = ref["question_number"], ref.get("part")
        label = f"{n}({part})" if part else str(n)
        item = self.mem.screen_items().get(n)
        if item:
            qs = split_questions(f"{n}. {item}")
            if part:
                qs = [q for q in qs if label_key(q["label"]) == f"{n}{part}"] or qs
            ans = self._new("snip", question=f"Question {label}", title=f"Question {label}", origin="reference")
            self._push(ans, force=True)
            return await self._solve_selection(ans, questions_text(qs), time.perf_counter(), qs=qs, remember=False)
        if not self.mem.latest_screen():
            ans = self._new("ask", question=text, status="error",
                            error=f"I can't see question {label}. Turn on screen reading (Settings → Screen) or snip the "
                                  f"question (Snip, or Ctrl+Alt+S) — then “answer question {label}” works.")
            self._push(ans, force=True)
            return ans
        ans = self._new("ask", question=f"Question {label}", kind="recall")
        self._push(ans, force=True)
        await self._run_answer(ans, f"question {label} on the screen", {}, DIRECT, extra=[], instructions=[
            f"Find question {label} in the SCREEN TEXT and answer it: SAY is the final answer, WHY the key working. "
            f"If question {label} is not in the screen text, set CONF: low and say you can't see it."])
        return ans

    def _last_answer(self) -> dict | None:
        for a in reversed(list(self.answers.values())):
            if a["status"] == "done" and a["mode"] in ("question", "ask", "snip", "tutor", "explain") and a.get("kind") != "note":
                return a
        return None

    async def _redo(self, old: dict | None, cmds: list[str], context: str, active: dict | None = None) -> dict:
        """Re-answer following the student's instruction, keeping the same question."""
        if old is None:
            return await self.ask(context or " ".join(cmds))
        question = old.get("question") or (active or {}).get("text", "")
        instructions = [COMMAND_TEXT[c] for c in cmds if c in COMMAND_TEXT]
        if "detailed" in cmds:
            instructions.append("Give a fuller explanation in WHY (4-5 sentences).")
        if old.get("say") or old.get("body"):
            instructions.append(f'Your previous reply was: "{(old.get("say") or old.get("body") or "")[:400]}"')
        extra = list(old.get("clarifications") or [])
        if context:
            extra.append(context)
            if active:
                self.mem.clarifications.setdefault(active["id"], []).append(context)
        mode = "question" if old["mode"] == "question" else "ask"
        ans = self._new(mode, question=question, kind=old.get("kind", ""), qid=old.get("qid"),
                        directed=old.get("directed"), clarifications=extra, replaces=old["id"],
                        instruction=", ".join(cmds))
        if active:
            active["answer_id"] = ans["id"]
        self._push(ans, force=True)
        ref = {"question_number": active["ref_n"]} if active and active.get("ref_n") else extract_reference(question)
        await self._run_answer(ans, question, ref, DIRECT, extra=extra, instructions=instructions)
        return ans

    async def ask(self, text: str) -> dict:
        """Free request typed by the student (topic, question, formula, attempt)."""
        topic, subject = parse_topic(text)
        if topic or subject:
            return await self.set_topic(text, topic, subject)
        ref = extract_reference(text)
        if ref.get("question_number") and len(content_words(text)) <= 4:
            return await self.solve_reference(text, ref)
        sig_kind = question_kind(text)
        ans = self._new("ask", question=text, kind=sig_kind)
        self._push(ans, force=True)
        last = None
        for a in reversed(list(self.answers.values())):
            if a["mode"] == "question" and a["say"] and time.time() - a["t"] < 180:
                last = a
                break
        extra = [f'The student typed: "{text}". Interpret it with the lesson context: it may be a topic or term to '
                 f'explain, a question to answer, a correction of your last answer, or their own attempt to check.']
        if last:
            extra.append(f'Your last suggestion was for "{last["resolved"] or last["question"]}": "{last["say"]}"')
        await self._run_answer(ans, text, extract_reference(text), DIRECT, extra=extra)
        return ans

    async def _run_answer(self, ans: dict, question: str, ref: dict, prio: int, extra: list[str],
                          instructions: list[str] | None = None) -> None:
        t0 = time.perf_counter()
        profile = self.settings.profile()
        ref_text = self.mem.resolve_reference(ref) if ref else None
        if ref and not ref_text:
            ans["unsure"] = f"can't see question {ref.get('question_number', '')} on screen".strip()
        # deterministic pre-solve
        cands = []
        for src in ([ref_text] if ref_text else []) + extra[:1] + [question]:
            if src:
                cands += find_problems(src)
        if not cands and ans.get("deictic"):
            obs = self.mem.latest_screen()
            if obs and len(obs.equations) == 1:
                cands = obs.equations[:1]
        verified = await self._solve(cands) if cands else []
        if verified:
            ans["result"] = verified[0][1]
            ans["calc"] = verified[0][0]
            ans["verified"] = True
            ans["sources"].append("sympy")
            ans["latency"]["verified_ms"] = round((time.perf_counter() - t0) * 1000)
            self._push(ans, force=True)

        context = self._context_block(ans, question, extra, ref_text, verified, profile["context_chars"], for_answer=True)
        directed = ans.get("directed")
        if ans["mode"] == "question":
            qline = f"QUESTION TO ANSWER: {question}"
            if directed:
                qline += f"  [the teacher addressed {self.name} directly]" if directed["level"] == "high" else \
                    f"  [{self.name} may have been addressed]"
        else:
            qline = f"{self.name.upper()} ASKS: {question}"
        system = P.SYSTEM_ANSWER.format(name=self.name, guide=P.guide(self.mem.subject_id, calc=True), format=P.FORMAT_RULES)
        instructions = list(instructions or [])
        if QUOTE_REQUEST.search(question + " " + " ".join(extra) + " " + " ".join(instructions)) \
                and "your texts" not in ans["sources"]:
            # no copy of the text: a small model WILL invent plausible-sounding quotes, so forbid them outright
            instructions = [i for i in instructions if not i.startswith("Give 1-3 short exact quotations")]
            instructions.append("There is NO copy of this text available, so write NO quotation marks and NO quoted wording. "
                                "SAY must instead name the best moment to quote, e.g. 'Use the part in chapter five where …, "
                                "because it shows …' (never ask for the passage). Set UNSURE: add the book to GLASS for exact quotes.")
        if instructions:
            qline += f"\n\nINSTRUCTIONS FROM {self.name.upper()} (follow them):\n- " + "\n- ".join(instructions)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": context + "\n\n" + qline}]

        t_sub = time.perf_counter()

        async def job():
            ans["latency"]["queued_ms"] = round((time.perf_counter() - t_sub) * 1000)
            await self._stream_answer(ans, messages, t0)

        fut = self.sched.submit(prio, job, key=f"answer:{ans.get('qid') or ans['id']}", max_age=30)
        try:
            await fut
        except asyncio.CancelledError:
            if ans["status"] not in ("done", "error"):
                ans["status"] = "superseded"
                self._push(ans, force=True)
            reraise_if_cancelled()
            return
        except LLMError as e:
            ans["status"] = "error"
            ans["error"] = str(e)
            self._push(ans, force=True)
            return
        except Exception as e:
            log.exception("answer failed")
            ans["status"] = "error"
            ans["error"] = f"{type(e).__name__}: {e}"
            self._push(ans, force=True)
            return
        # a "spoken answer" that only describes the question: ask once more for the answer itself
        if META_SAY.search(ans["say"] or "") and ans["kind"] not in ("none", "opinion") and not ans.get("_retried"):
            ans["_retried"] = True
            log.info("SAY described the question instead of answering; retrying: %r", ans["say"][:80])
            retry = messages[:1] + [{"role": "user", "content": messages[1]["content"] + "\n\nIMPORTANT: your last attempt "
                                     f'("{ans["say"][:200]}") described the question instead of answering it. SAY must be the answer itself.'}]
            for k in ("say", "why"):
                ans[k] = ""

            async def job2():
                await self._stream_answer(ans, retry, t0)

            try:
                await self.sched.submit(DIRECT, job2, key=f"answer:{ans.get('qid') or ans['id']}:retry")
            except (asyncio.CancelledError, Exception):
                reraise_if_cancelled()
        self._finish(ans)
        if profile.get("verify_answers") and not ans["verified"] and ans["kind"] in ("calculation", "explanation", "comparison") \
                and ans["conf"] != "low" and ans["say"]:
            asyncio.create_task(self._verify(ans, context, question))

    async def _stream_answer(self, ans: dict, messages: list[dict], t0: float) -> None:
        ans["status"] = "streaming"
        text = ""
        prefill = ""
        calc_done = ans["verified"]  # pre-solved exactly: no need to interrupt for a CALC line
        for _round in range(2):
            msgs = messages if not prefill else messages + [{"role": "assistant", "content": prefill}]
            interrupted = False
            async for chunk in self.llm.stream_chat(self.text_model, msgs, think=False, num_ctx=self.num_ctx,
                                                    num_predict=300, temperature=0.2):
                if "first_ms" not in ans["latency"]:
                    ans["latency"]["first_ms"] = round((time.perf_counter() - t0) * 1000)
                text += chunk
                self._parse(ans, text)
                if ans["say"] and "say_ms" not in ans["latency"] and re.search(r"\n\s*WHY\s*:", text):
                    ans["latency"]["say_ms"] = round((time.perf_counter() - t0) * 1000)
                self._push(ans)
                if not calc_done and re.search(r"(^|\n)\s*CALC\s*:[^\n]*\n", text):
                    calc = re.search(r"(?:^|\n)\s*CALC\s*:([^\n]*)\n", text).group(1).strip()
                    if calc.lower().strip(" .") not in NULLISH:
                        calc_done = True
                        interrupted = True
                        break
                    calc_done = True
            if not interrupted:
                break
            # evaluate CALC exactly and continue the same answer with the result injected
            r = await asyncio.to_thread(self.math.run, ans["calc"])
            head = text[: text.index("\n", text.index("CALC")) + 1]
            if r.ok:
                ans["result"] = r.value
                ans["verified"] = True
                if "sympy" not in ans["sources"]:
                    ans["sources"].append("sympy")
                prefill = head + f"RESULT: {r.value}\nSAY:"
            else:
                prefill = head + "RESULT: (could not be computed — check the working carefully)\nSAY:"
            ans["latency"]["calc_ms"] = round((time.perf_counter() - t0) * 1000)
            text = prefill
            self._parse(ans, text)
            self._push(ans, force=True)
        ans["latency"]["total_ms"] = round((time.perf_counter() - t0) * 1000)

    def _parse(self, ans: dict, text: str) -> None:
        current = None
        fields: dict[str, list[str]] = {}
        for line in text.split("\n"):
            m = FIELD_RE.match(line)
            if m:
                current = m.group(1)
                fields[current] = [m.group(2)]
            elif current in ("WHY", "SAY") and line.strip():
                fields[current].append(line.strip())
        get = lambda k: _clean(" ".join(fields.get(k, [])))
        if "Q" in fields:
            ans["resolved"] = get("Q")
        if "TYPE" in fields:
            t = get("TYPE").lower().split()[0] if get("TYPE") else ""
            ans["kind"] = t or ans["kind"]
        if "CONF" in fields:
            c = get("CONF").lower()
            ans["conf"] = "low" if "low" in c else "medium" if "med" in c else "high" if "high" in c else ans["conf"]
        if "UNSURE" in fields:
            u = get("UNSURE")
            if u.lower().strip(" .") not in NULLISH:
                ans["unsure"] = u
        if "ASK" in fields:
            parts = [p.strip(" \"'") for p in get("ASK").split("|")]
            if parts and parts[0] and parts[0].lower().strip(" .") not in NULLISH and len(parts) >= 2:
                ans["ask"] = {"q": parts[0], "options": [p for p in parts[1:4] if p]}
        if "CALC" in fields and not ans["verified"]:
            c = get("CALC")
            if c.lower().strip(" .") not in NULLISH:
                ans["calc"] = c
        if "SAY" in fields:
            ans["say"] = get("SAY")
        if "WHY" in fields:
            ans["why"] = get("WHY")

    def _finish(self, ans: dict) -> None:
        ans["status"] = "done"
        lat = ans["latency"]
        log.info("answer kind=%s queued=%sms first=%sms say=%sms total=%sms model=%s | %s", ans["kind"], lat.get("queued_ms"),
                 lat.get("first_ms"), lat.get("say_ms"), lat.get("total_ms"), self.text_model, (ans["question"] or "")[:60])
        self.last_llm_use = time.time()
        # TYPE none = "not a question needing an answer" — but never hide it when the
        # student was addressed or when the model is unsure what was meant
        if ans["kind"] == "none" and ans["mode"] == "question" and not ans["directed"]:
            ans["rhetorical"] = True  # not a lesson question (attendance, logistics...): keep it off the overlay
        self.check_quotes(ans)
        if not ans["conf"]:
            ans["conf"] = "medium"
        if ans["conf"] == "low" and not ans.get("ask") and ans["mode"] == "question":
            # the model didn't suggest options: offer what this lesson has been about
            opts = [o for o in dict.fromkeys([self.mem.subtopic, self.mem.topic, *list(self.mem.key_terms)[-3:]]) if o][:3]
            if opts:
                ans["ask"] = {"q": "Not sure what the teacher means — is it about:", "options": opts}
        self._push(ans, force=True)
        if self.on_answer:
            try:
                self.on_answer(ans)
            except Exception:
                log.debug("on_answer hook failed", exc_info=True)
        if ans["mode"] == "question" and self.mem.active_question and ans.get("qid") == self.mem.active_question["id"]:
            self.mem.add_question({"id": ans["qid"], "t": ans["t"], "text": ans["question"], "resolved": ans["resolved"],
                                   "kind": ans["kind"], "say": ans["say"], "directed": bool(ans["directed"])})
        if self.db and self.session_id and ans["mode"] in ("question", "ask"):
            self.db.add_question(self.session_id, ans.get("qid") or ans["id"], ans["t"], ans["resolved"] or ans["question"],
                                 ans["kind"], (ans["directed"] or {}).get("confidence", 0.0), self._public(ans))

    async def _verify(self, ans: dict, context: str, question: str) -> None:
        """Second, slower pass: reason step by step and revise if the quick answer was wrong."""
        prompt = (f"{context}\n\nQUESTION: {ans['resolved'] or question}\nPROPOSED ANSWER: {ans['say']}\n\n"
                  "Work through the question carefully step by step (briefly). Then on the last two lines write exactly:\n"
                  "VERDICT: correct | incorrect | uncertain\nFIXED: <if incorrect, one corrected natural sentence the student "
                  "could say; else ->")
        msgs = [{"role": "system", "content": "You are a meticulous IGCSE examiner checking a student's answer. "
                                              + P.guide(self.mem.subject_id)},
                {"role": "user", "content": prompt}]

        async def job():
            return await self.llm.chat(self.text_model, msgs, think=False, num_ctx=self.num_ctx, num_predict=500,
                                       temperature=0.1)

        try:
            out = await self.sched.submit(TASK, job, key=f"verify:{ans['id']}", max_age=20)
        except asyncio.CancelledError:
            reraise_if_cancelled()
            return
        except Exception:
            return
        m = re.search(r"VERDICT\s*:\s*(correct|incorrect|uncertain)", out, re.I)
        f = re.search(r"FIXED\s*:\s*(.+)", out)
        if not m:
            return
        verdict = m.group(1).lower()
        ans["check"] = verdict
        if verdict == "incorrect" and f and f.group(1).strip().lower() not in NULLISH:
            ans["previous_say"] = ans["say"]
            ans["say"] = f.group(1).strip()
            ans["revised"] = True
            ans["conf"] = "medium"
        elif verdict == "uncertain" and ans["conf"] == "high":
            ans["conf"] = "medium"
        self._push(ans, force=True)

    # ------------------------------------------------------------- long-form modes
    async def _stream_markdown(self, ans: dict, system: str, user: str, prio: int, field: str = "body",
                               images: list[bytes] | None = None, model: str | None = None,
                               num_predict: int = 700, on_update: Callable[[dict], None] | None = None,
                               think: bool = False) -> None:
        t0 = time.perf_counter()
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        async def job():
            ans["status"] = "streaming"
            buf = ""
            async for chunk in self.llm.stream_chat(model or self.text_model, msgs, think=think,
                                                    num_ctx=self.num_ctx, num_predict=num_predict, temperature=0.3,
                                                    images=images):
                if "first_ms" not in ans["latency"]:
                    ans["latency"]["first_ms"] = round((time.perf_counter() - t0) * 1000)
                buf += chunk
                ans[field] = buf
                if on_update:
                    on_update(ans)
                self._push(ans)
            ans["latency"]["total_ms"] = round((time.perf_counter() - t0) * 1000)
            return buf

        try:
            await self.sched.submit(prio, job, key=f"{ans['mode']}:{ans['id']}")
            ans["status"] = "done"
            self.check_quotes(ans)
        except asyncio.CancelledError:
            ans["status"] = "superseded"
            self._push(ans, force=True)
            reraise_if_cancelled()
        except Exception as e:
            ans["status"] = "error"
            ans["error"] = str(e)
        self._push(ans, force=True)

    async def expand(self, answer_id: str) -> dict | None:
        ans = self.answers.get(answer_id)
        if not ans:
            return None
        if ans["detailed"]:
            ans["level"] = "detailed"
            self._push(ans, force=True)
            return ans
        ans["level"] = "detailed"
        ans["status"] = "thinking"
        self._push(ans, force=True)
        ctx = self._context_block({"sources": []}, ans["question"], ans.get("clarifications", []), None,
                                  [(ans["calc"], ans["result"])] if ans["verified"] else [], 3500)
        user = (f"{ctx}\n\nQUESTION: {ans['resolved'] or ans['question']}\nSHORT ANSWER ALREADY GIVEN: {ans['say']}\n"
                f"Now give the detailed explanation.")
        await self._stream_markdown(ans, P.SYSTEM_DETAILED.format(name=self.name, guide=P.guide(self.mem.subject_id)),
                                    user, DIRECT, field="detailed", num_predict=900)
        return ans

    async def explain_now(self) -> dict:
        ans = self._new("explain", question="What's being explained right now?")
        self._push(ans, force=True)
        ctx = self._context_block(ans, "", [], None, [], 4000)
        await self._stream_markdown(ans, P.SYSTEM_EXPLAIN.format(name=self.name, guide=P.guide(self.mem.subject_id)),
                                    ctx + "\n\nExplain what the teacher is currently discussing.", DIRECT, num_predict=400)
        return ans

    async def catchup(self, minutes: float = 3) -> dict:
        ans = self._new("catchup", question=f"What did I miss? (last {int(minutes)} min)")
        self._push(ans, force=True)
        since = time.time() - minutes * 60
        tr = self.mem.transcript_since(since)
        if not tr.strip():
            ans["body"] = "Nothing was said in that time (no speech was transcribed)."
            ans["status"] = "done"
            self._push(ans, force=True)
            return ans
        screen = self.mem.screen_context(1200)
        user = f"{self._subject_line()}\n\nTRANSCRIPT (last {int(minutes)} min):\n{tr[-6000:]}\n\nSCREEN NOW:\n{screen or '(none)'}"
        await self._stream_markdown(ans, P.SYSTEM_CATCHUP.format(name=self.name), user, DIRECT, num_predict=450)
        return ans

    async def tutor(self, mode: str, task: dict | None, attempt: str = "", focus: str = "") -> dict:
        task = task or self.mem.current_task or {}
        title = {"hint": "Hint", "step": "Next step", "check": "Checking your work", "solution": "Worked solution",
                 "steps": "How to approach this task", "explain": "What the question asks"}.get(mode, "Help")
        ans = self._new("tutor", question=f"{title}{': ' + focus if focus else ''}", tutor_mode=mode)
        self._push(ans, force=True)
        items = self.mem.screen_items()
        qtext = focus
        m = re.search(r"\d+", focus or "")
        if m and int(m.group()) in items:
            qtext = f"{m.group()}. {items[int(m.group())]}"
        verified = await self._solve(find_problems(qtext)) if qtext else []
        history = [a for a in self.answers.values() if a["mode"] == "tutor" and a.get("focus") == focus and a["body"]]
        ans["focus"] = focus
        ctx = self._context_block(ans, qtext, [], None, verified, 3500)
        user = (f"{ctx}\n\nTASK: {task.get('description', '(none detected)')} {task.get('source') or ''} "
                f"{task.get('questions') or ''}\nQUESTION BEING WORKED ON: {qtext or '(not specified — use the task / screen)'}\n")
        if history:
            user += "WHAT YOU ALREADY SHOWED THEM:\n" + "\n---\n".join(h["body"][-600:] for h in history[-2:]) + "\n"
        if attempt:
            user += f"STUDENT'S ATTEMPT: {attempt}\n"
        await self._stream_markdown(ans, P.SYSTEM_TUTOR.format(name=self.name, mode=mode, guide=P.guide(self.mem.subject_id)),
                                    user, ANSWER, num_predict=700)
        return ans

    def after_vision(self) -> None:
        """The vision model evicts the answer model on 8 GB GPUs; reload it right away
        (in the background) so the next teacher question doesn't pay the reload."""
        self.after_model(self.vision_model)

    def after_model(self, model: str) -> None:
        if model != self.text_model:
            asyncio.create_task(self._rewarm())

    async def _rewarm(self) -> None:
        try:
            await self.llm.warm(self.text_model, self.num_ctx)
        except Exception:
            pass

    async def warm_prefix(self) -> None:
        """CPU-only machines: Ollama caches the most recent prompt prefix. Background
        jobs (notes, topic tracking) replace it, so the next answer would re-read the
        whole system prompt — seconds on a laptop CPU. Re-warm it while idle."""
        if not self.settings.profile().get("rewarm_prefix"):
            return
        system = P.SYSTEM_ANSWER.format(name=self.name, guide=P.guide(self.mem.subject_id, calc=True), format=P.FORMAT_RULES)
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": "ok"}]

        async def job():
            return await self.llm.chat(self.text_model, msgs, think=False, num_ctx=self.num_ctx, num_predict=1)

        try:
            await self.sched.submit(REVISION, job, key="prefix")
        except (asyncio.CancelledError, Exception):
            reraise_if_cancelled()

    async def prepare_vision(self) -> None:
        if not self.settings.profile().get("vision_enabled", True):
            return
        if self.vision_model != self.text_model:
            try:
                await self.llm.warm(self.vision_model, self.num_ctx)
            except Exception:
                pass

    async def _read_with_vision(self, ans: dict, image_png: bytes) -> str | None:
        """Transcribe a selection with the vision model; None (and an error on the card) if unreadable."""
        ans["status"] = "reading"
        self._push(ans, force=True)
        transcribe = ("Transcribe exactly what this image from a school lesson shows: every question (keep the numbering "
                      "and part letters like (a), (b) on their own lines), all maths in plain text (use ^ for powers, e.g. "
                      "x^2 for x squared, and sqrt()), labels and values from any diagram, graph or table. Copy every "
                      "number, sign and power exactly. Then one line starting 'DIAGRAM:' describing any diagram. Do not "
                      "solve anything. Never guess or invent content: if the image is blank, too blurry or has no "
                      "readable content, reply exactly UNREADABLE.")
        msgs = [{"role": "user", "content": transcribe}]

        async def vjob():
            return await self.llm.chat(self.vision_model, msgs, think=False, num_ctx=self.num_ctx, num_predict=700,
                                       temperature=0.1, images=[image_png])

        text = await self.sched.submit(DIRECT, vjob, key=f"snipv:{ans['id']}")
        self.after_vision()
        if "UNREADABLE" in text.upper()[:40]:
            ans["status"] = "error"
            ans["error"] = "The selection couldn't be read. Try selecting a larger, sharper area."
            self._push(ans, force=True)
            return None
        ans["sources"].append("vision")
        return text

    async def solve_snip(self, image_png: bytes, ocr_text: str, use_vision: bool, powers: dict | None = None) -> dict:
        """Snip & Solve: the student selected a region of the screen."""
        powers = powers or {}
        can_vision = bool(self.settings.profile().get("vision_enabled", True))
        ans = self._new("snip", question="Selected region", title="Your selection", ocr=ocr_text[:3000],
                        can_vision=can_vision, powers_read=powers.get("fixed", []),
                        powers_unreadable=powers.get("unreadable", []))
        if powers.get("unreadable") and can_vision and not use_vision:
            # a power is there but couldn't be read: the vision model reads maths layout properly
            use_vision = True
            ans["auto_vision"] = True
        self._last_snip = (ans["id"], image_png)
        self._push(ans, force=True)
        t0 = time.perf_counter()
        problem_text = ocr_text.strip()
        if not self.settings.profile().get("vision_enabled", True):
            if len(re.sub(r"\W", "", problem_text)) < 12:
                ans["status"] = "error"
                ans["error"] = ("No readable text in the selection. Reading diagrams needs the vision model, which this "
                                "computer can't run fast enough — select the typed question instead.")
                self._push(ans, force=True)
                return ans
            use_vision = False
        if use_vision or len(re.sub(r"\W", "", problem_text)) < 12:
            try:
                read = await self._read_with_vision(ans, image_png)
                if read is None:
                    return ans
                problem_text = read
            except Exception as e:
                reraise_if_cancelled()
                log.warning("vision failed: %s", e)
                ans["unsure"] = "vision model unavailable; used OCR text only"
                if not problem_text:
                    ans["status"] = "error"
                    ans["error"] = f"Could not read the selection ({e})"
                    self._push(ans, force=True)
                    return ans
        else:
            ans["sources"].append("ocr")
        return await self._solve_selection(ans, problem_text, t0)

    async def resolve_snip(self, answer_id: str, text: str | None = None, vision: bool = False,
                           think: bool = False) -> dict:
        """The student checked what GLASS read: solve their corrected text, or re-read the
        same selection with the vision model (better with powers, fractions and diagrams)."""
        old = self.answers.get(answer_id)
        if old is None:
            raise ValueError("That answer is no longer available — snip the question again.")
        ans = self._new("snip", question=old["question"], title=old.get("title") or "Your selection",
                        origin=old.get("origin"), replaces=answer_id, can_vision=old.get("can_vision", False))
        old["status"] = "superseded"
        self._push(old, force=True)
        self._push(ans, force=True)
        t0 = time.perf_counter()
        if vision:
            if not self._last_snip or self._last_snip[0] != answer_id:
                raise ValueError("Only the latest snip can be re-read — snip it again with V (diagram mode) on.")
            png = self._last_snip[1]
            self._last_snip = (ans["id"], png)
            try:
                read = await self._read_with_vision(ans, png)
            except Exception as e:
                reraise_if_cancelled()
                ans["status"], ans["error"] = "error", f"The vision model couldn't read it ({e})"
                self._push(ans, force=True)
                return ans
            if read is None:
                return ans
            return await self._solve_selection(ans, read, t0)
        if text:
            ans["sources"].append("edited")
        if self._last_snip and self._last_snip[0] == answer_id:
            self._last_snip = (ans["id"], self._last_snip[1])
        return await self._solve_selection(ans, text or old.get("ocr", ""), t0, think=think)

    async def solve_screen(self, text: str, labels: list[str] | None = None) -> dict:
        """Solve questions GLASS found on the shared screen (all, or the ones tapped)."""
        qs = split_questions(norm_math(text))
        if labels:
            want = {label_key(l) for l in labels}
            # "2" selects every part of question 2
            qs = [q for q in qs if label_key(q["label"]) in want or re.sub(r"[a-z]+$", "", label_key(q["label"])) in want]
        if not qs:
            raise ValueError("Those questions are no longer on the screen.")
        title = "Questions on screen" if len(qs) > 1 else f"Question {qs[0]['label']} on screen"
        ans = self._new("snip", question=title, title=title, origin="screen")
        self._push(ans, force=True)
        return await self._solve_selection(ans, questions_text(qs), time.perf_counter(), qs=qs, remember=False)

    async def _solve_selection(self, ans: dict, problem_text: str, t0: float, qs: list[dict] | None = None,
                               remember: bool = True, think: bool = False) -> dict:
        """Shared by snips, screen questions and "answer question 2b": show the student
        exactly what was read, then answer each question in its own block."""
        problem_text = fix_italic_g(norm_math(problem_text).strip())
        if qs is None:
            qs = split_questions(problem_text)
        else:  # questions picked from the screen: their functions may be defined in the stem
            qs = [{**q, "text": fix_italic_g(q["text"], function_defs(f"{q.get('stem') or ''}\n{q['text']}"))} for q in qs]
        ans["ocr"] = problem_text[:3000]
        ans["questions"] = qs[:20]
        ans["powers"] = flat_powers(problem_text)
        ans["status"] = "thinking"
        ans["latency"]["read_ms"] = round((time.perf_counter() - t0) * 1000)
        self._push(ans, force=True)
        if remember:  # later, "answer question 2b" can refer to what was snipped
            self.mem.add_screen(ScreenObs(time.time(), problem_text, parse_numbered_items(problem_text),
                                          find_problems(problem_text), "Selection", source="snip"))
        # exact answers, question by question (the model explains them; it doesn't do the algebra)
        exact: dict[str, str] = {}
        verified: list[tuple[str, str]] = []
        for q in qs[:12]:
            got = await self._solve(plan_math(q["text"], q.get("stem") or ""), limit=2)
            if got:
                exact[q["label"]] = "; ".join(tidy_result(v) for _, v in got)
                shown = q["text"].strip().rstrip("=?| ").strip() or got[0][0]  # "fg(2)" reads better than its SymPy form
                verified += [(f"[{q['label']}] {shown}", tidy_result(v)) for _, v in got[:1]]
        if not qs:
            verified = await self._solve(find_problems(problem_text), limit=8)
        ans["exact"] = exact
        if verified:
            ans["verified"] = True
            ans["result"] = "; ".join(f"{e} → {v}" for e, v in verified)
            ans["sources"].append("sympy")
        # the selection is the question: keep the lesson context short so it can't hijack the answer
        ctx = self._context_block({"sources": []}, problem_text, [], None, verified, 1200)
        system = P.SYSTEM_SNIP.format(name=self.name, guide=P.guide(self.mem.subject_id))
        user = f"{ctx}\n\nQUESTIONS:\n{questions_text(qs) if qs else problem_text}"
        if ans["powers"]:
            user += (f"\n\nPOSSIBLY FLATTENED POWERS (OCR): {', '.join(ans['powers'])} — most likely "
                     + ", ".join(f"{p[:-1]}^{p[-1]}" for p in ans["powers"]) + "; say so in the working if you assume it.")

        def update(a: dict) -> None:
            a["parts"] = [{**p, "exact": exact.get(p["label"], "")} for p in pair_parts(a["questions"], a["body"])]

        model = None
        if think or self.settings.get("snip_think"):
            rm = self.settings.get("reasoning_model") or ""
            if rm and await self.llm.has_model(rm):
                model, think = rm, True
                ans["thinking_hard"] = True
                ans["sources"].append("thinking model")
                self._push(ans, force=True)
            else:
                think = False
                ans["unsure"] = (f"the thinking model ({rm or 'none set'}) isn't downloaded, so the fast model answered — "
                                 f"download it in Settings → AI models")
        # thinking tokens count towards the limit: give the reasoning model room to work
        budget = min(1500, 220 + 140 * max(1, len(qs))) + (4500 if think else 0)
        await self._stream_markdown(ans, system, user, DIRECT, num_predict=budget, on_update=update, think=think,
                                    model=model)
        if model:
            self.after_model(model)
        update(ans)
        if not ans["parts"]:
            ans["body"] = _paragraphs(ans["body"])
        self._push(ans, force=True)
        return ans
