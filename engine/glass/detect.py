"""Fast, deterministic detectors that run on every transcript (no LLM).

* NameDetector: was the student addressed? Combines fuzzy + phonetic matching
  against configured aliases, vocative sentence context and Whisper's per-word
  acoustic probability. Returns a graded confidence, never a bare boolean.
* classify_question: is this a real question, which kind, is it rhetorical or a
  comprehension check, and does it reference something ("question three")?
* detect_task_cue: cheap trigger for the LLM task extractor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import jellyfish
from rapidfuzz.distance import JaroWinkler

from .mathsolve import word_to_int

# Real words that sound like "Sanad" and must not trigger on their own.
NAME_BLOCKLIST = {
    "sand", "sound", "sent", "send", "saint", "said", "sonnet", "senate", "second", "signed", "snack",
    "sanity", "salad", "sandy", "sander", "standard", "stand", "synod", "signal", "sinned", "sunday",
    "senator", "snap", "sad", "and", "an", "canada", "nomad", "sana",
}

VOCATIVE_NEXT = re.compile(
    r"^(can|could|would|will|what|what's|whats|why|how|do|did|does|are|have|is|tell|explain|give|try|"
    r"please|you|your|any|which|who|when|where|read|answer|solve|go|come|share|unmute)\b",
    re.I,
)
THANKS_PREV = re.compile(r"\b(thank you|thanks|well done|good job|great|nice|good|excellent|bye|hi|hello|morning)\W*$", re.I)


@dataclass
class NameHit:
    confidence: float
    token: str
    position: str  # start | end | middle
    vocative: bool
    level: str  # high | possible

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class NameDetector:
    def __init__(self, name: str, aliases: list[str] | None = None):
        self.configure(name, aliases or [])

    def configure(self, name: str, aliases: list[str]) -> None:
        self.name = name.strip()
        self.aliases = sorted({a.strip().lower() for a in [name, *aliases] if a.strip()})
        self.codes = {jellyfish.metaphone(a) for a in self.aliases}

    def _token_score(self, tok: str) -> float:
        t = re.sub(r"[^a-z]", "", tok.lower())
        if len(t) < 3:
            return 0.0
        if t in self.aliases:
            return 0.75
        if t in NAME_BLOCKLIST:
            return 0.0
        jw = max(JaroWinkler.similarity(t, a) for a in self.aliases)
        phon = jellyfish.metaphone(t) in self.codes
        if jw >= 0.9:
            return 0.6
        if jw >= 0.84 and phon:
            return 0.5
        if jw >= 0.8 and phon and len(t) <= 7:
            return 0.4
        return 0.0

    def scan(self, text: str, word_probs: list[tuple[str, float]] | None = None) -> NameHit | None:
        raw = re.findall(r"[\w']+|[,.?!]", text)
        words = [w for w in raw if re.match(r"[\w']", w)]
        if not words:
            return None
        probs = {}
        for w, p in word_probs or []:
            probs.setdefault(re.sub(r"[^a-z]", "", w.lower()), p)
        best: NameHit | None = None
        n = len(words)
        for i, w in enumerate(words):
            cands = [(w, self._token_score(w))]
            if i + 1 < n:  # "Sun ad", "Sa nad"
                joined = w + words[i + 1]
                cands.append((joined, self._token_score(joined) - 0.1))
            tok, score = max(cands, key=lambda c: c[1])
            if score <= 0:
                continue
            # locate in the punctuated token stream for vocative cues
            idx = _find_index(raw, w)
            after = raw[idx + 1] if idx + 1 < len(raw) else ""
            before = raw[idx - 1] if idx > 0 else ""
            rest = " ".join(words[i + 1:i + 4])
            prev_text = " ".join(words[max(0, i - 3):i])
            position = "start" if i == 0 or before in ".?!" else ("end" if i >= n - 1 else "middle")
            vocative = False
            if position == "start" and (after == "," or VOCATIVE_NEXT.match(rest or "")):
                score += 0.2
                vocative = True
            elif position == "end" and before == ",":
                score += 0.2
                vocative = True
            elif after == "," and VOCATIVE_NEXT.match(rest or ""):
                score += 0.15
                vocative = True
            if VOCATIVE_NEXT.match(rest or ""):
                score += 0.1
            if "?" in text:
                score += 0.05
            if THANKS_PREV.search(prev_text):
                score -= 0.05  # "thank you, Sanad" addresses but is not a question
            p = probs.get(re.sub(r"[^a-z]", "", w.lower()))
            if p is not None:
                if p >= 0.7:
                    score += 0.05
                elif p < 0.3:
                    score -= 0.15
            score = max(0.0, min(1.0, score))
            if score >= 0.45 and (best is None or score > best.confidence):
                best = NameHit(round(score, 2), tok, position, vocative, "high" if score >= 0.75 else "possible")
        return best


def _find_index(raw: list[str], w: str) -> int:
    try:
        return raw.index(w)
    except ValueError:
        return 0


# --------------------------------------------------------------------------- questions
CHECK_PATTERNS = re.compile(
    r"\b(does (that|this|everyone) make sense|make sense\??$|any questions|is that clear|everyone (with me|ok|okay|following)|"
    r"got it\??$|you see\??$|right\?$|okay\?$|ok\?$|yeah\?$|alright\?$|all right\?$|understood\??$|can everyone (see|hear)|"
    r"can you (all )?(see|hear) (me|my screen|this)|are you (all )?(there|with me)|is everyone here|ready\?$)",
    re.I,
)
RHETORICAL_FOLLOW = re.compile(
    r"^(well|because|it's|it is|that's|that is|the answer|this is|so,? (it|the|that)|basically|simply|it was|they|we|let me|let's)\b",
    re.I,
)
QUESTION_START = re.compile(
    r"^(what|what's|whats|why|how|when|where|which|who|whose|whom|can|could|would|will|does|do|did|is|are|was|were|"
    r"should|shall|has|have|anyone|anybody|somebody|someone)\b",
    re.I,
)
IMPERATIVE_ASK = re.compile(
    r"\b(can anyone|could anyone|does anyone|does anybody|anybody know|anyone know|who can (tell|explain|give|remember)|"
    r"tell me|explain (why|how|what)|give me (an|a|one|two|three|some)|"
    r"solve|work out|calculate|find the|simplify|factori[sz]e|expand|differentiate|what do you think|"
    r"describe|define|name (a|an|one|two|three|the)|suggest|state (the|a|one|two))\b",
    re.I,
)
TASK_CUE = re.compile(
    r"\b(page \d+|pages? \w+|questions? \w+ (to|through|till|until|and|-) \w+|exercise \w+|worksheet|homework|"
    r"textbook|you have (\w+ )?(\d+|\w+) minutes|for (homework|next lesson)|due (on|by|next|tomorrow)|"
    r"i want you (all )?to|your task|in pairs|in groups|complete (the|questions|all|exercise)|"
    r"(do|answer|attempt|try) (questions?|numbers?|exercises?|all|the first|the next|page)|"
    r"submit|hand in|deadline|by (monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|next week))\b",
    re.I,
)
QREF = re.compile(
    r"\b(?:question|q|number|no\.?|exercise|problem|part)\s*(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|twenty)\s*(?:\(?([a-h])\)?)?\b",
    re.I,
)
ORDINAL_REF = re.compile(r"\bthe (first|second|third|fourth|fifth|last|next) (one|question|problem|part)\b", re.I)
DEICTIC = re.compile(
    r"\b(this|that|these|those|it|here|there|the (graph|diagram|equation|table|slide|picture|image|chart|board|screen|"
    r"example|one on (the )?(screen|board|slide)))\b",
    re.I,
)


@dataclass
class QuestionSignal:
    is_question: bool
    kind: str = "none"
    text: str = ""
    confidence: float = 0.0
    rhetorical: bool = False
    check: bool = False
    reference: dict = field(default_factory=dict)
    deictic: bool = False  # refers to something not in the words ("why does that happen?")

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.?!])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def question_kind(q: str) -> str:
    s = q.lower()
    if re.search(r"\b(what do you think|do you agree|your opinion|how do you feel|would you rather|do you like|evaluate|to what extent|"
                 r"how effective|why do you think|interpret)\b", s):
        return "opinion"
    if re.search(r"\b(solve|calculate|work out|find the value|value of|how (many|much|far|long|fast)|simplify|factori[sz]e|"
                 r"expand|differentiate|convert|estimate|round|what is \d|what's \d|percentage|ratio|area|volume|perimeter|"
                 r"gradient|probability|mean|median|speed|density|moles?|mass of|energy|current|resistance)\b", s) \
            or re.search(r"\d\s*[x+\-*/^=]|\bx\b", s):
        return "calculation"
    if re.search(r"\b(give (me )?(an |one |two |some )?examples?|example of|such as|name (a|an|one|two|three))\b", s):
        return "example"
    if re.search(r"\b(difference between|compare|contrast|similarit)", s):
        return "comparison"
    if re.search(r"\b(what (is|are|was) (a|an|the meaning)|what does .{1,30} mean|define|definition|meaning of|what do we call|"
                 r"what's (a|an) )\b", s):
        return "definition"
    if re.search(r"\b(why|how does|how do|how is|how are|explain|describe|what happens|account for|outline|suggest why)\b", s):
        return "explanation"
    return "recall"


def classify_question(text: str, next_text: str = "") -> QuestionSignal:
    """Classify the most relevant question in an utterance.

    next_text: speech that immediately followed (same speaker) — used to spot
    rhetorical questions the teacher answers themselves.
    """
    sents = split_sentences(text)
    if not sents:
        return QuestionSignal(False)
    # walk backwards: the last question-like sentence is what's being asked
    for idx in range(len(sents) - 1, -1, -1):
        s = sents[idx]
        clean = re.sub(r"^(so|okay|ok|right|now|and|alright|well|um|uh|then)[, ]+", "", s, flags=re.I).strip()
        clean = re.sub(r"^\w+,\s+(?=\w)", lambda m: m.group(0) if QUESTION_START.match(m.group(0)) else "", clean)  # drop vocative
        is_q_mark = s.endswith("?")
        starts = bool(QUESTION_START.match(clean))
        imperative = bool(IMPERATIVE_ASK.search(s))
        if not (is_q_mark or imperative or (starts and len(clean.split()) > 3)):
            continue
        if CHECK_PATTERNS.search(s):
            return QuestionSignal(True, "check", s, 0.8, check=True)
        if TASK_CUE.search(s) and not is_q_mark:
            continue  # instructions handled by the task detector
        following = " ".join(sents[idx + 1:]) or next_text
        rhetorical = bool(following) and bool(RHETORICAL_FOLLOW.match(following.strip())) and len(following.split()) > 3
        conf = 0.5 + (0.25 if is_q_mark else 0) + (0.15 if starts or imperative else 0)
        ref = extract_reference(s)
        deictic = bool(DEICTIC.search(s)) and not ref and len(s.split()) < 12
        return QuestionSignal(True, question_kind(s), s, round(min(conf, 0.95), 2), rhetorical=rhetorical,
                              reference=ref, deictic=deictic)
    return QuestionSignal(False)


def extract_reference(s: str) -> dict:
    """'question three', 'Q4 b', 'number 2', 'the last one' -> {"question_number": 3, "part": "b"} / {"ordinal": ...}"""
    m = QREF.search(s)
    if m:
        n = word_to_int(m.group(1))
        if n is not None:
            ref = {"question_number": n}
            if m.group(2):
                ref["part"] = m.group(2).lower()
            return ref
    m2 = ORDINAL_REF.search(s)
    if m2:
        return {"ordinal": m2.group(1).lower()}
    return {}


def detect_task_cue(text: str) -> bool:
    return bool(TASK_CUE.search(text))
