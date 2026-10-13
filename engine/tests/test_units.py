"""Fast deterministic tests (no GPU / LLM needed):  pytest -q tests/test_units.py"""
from __future__ import annotations

from pathlib import Path

import asyncio

import pytest

from glass.detect import NameDetector, classify_question, detect_task_cue, extract_reference
from glass.intel import loads_lenient
from glass.llm import ANSWER, DIRECT, NOTES, Scheduler
from glass.mathsolve import MathService, find_problems
from glass.memory import parse_numbered_items

# ---------------------------------------------------------------- name detection
ADDRESSED = [
    "Sanad, can you explain why combustion is exothermic?",
    "So what's the value of x, Sanad?",
    "Sanad, what do you think?",
    "Okay Sanad, go ahead.",
    "Sannad, are you there?",
    "Sanad? Can you hear me?",
    "Senad, can you read question two?",
    "What about you, Sanad?",
    "Sun ad, can you answer this one?",  # imperfect ASR split
]
NOT_ADDRESSED = [
    "The sound travels faster in solids.",
    "Sand is mostly silicon dioxide.",
    "I sent the worksheet yesterday.",
    "The senate passed the law in 1832.",
    "Let's look at the second question.",
    "The standard form is a times ten to the n.",
    "Signed numbers can be negative.",
    "On Sunday we will have a quiz.",
    "Salad dressing is an emulsion.",
]


@pytest.fixture(scope="module")
def names():
    return NameDetector("Sanad", ["Sannad", "Sanaad", "Senad"])


def test_name_recall(names):
    hits = [s for s in ADDRESSED if names.scan(s)]
    assert len(hits) / len(ADDRESSED) >= 0.88, f"missed: {set(ADDRESSED) - set(hits)}"


def test_name_precision(names):
    false = [s for s in NOT_ADDRESSED if names.scan(s)]
    assert not false, f"false positives: {false}"


def test_vocative_is_high(names):
    h = names.scan("Sanad, can you explain this?")
    assert h and h.level == "high" and h.vocative


def test_low_acoustic_probability_lowers_confidence(names):
    hi = names.scan("Sinad, what is it?", [("Sinad", 0.9)])
    lo = names.scan("Sinad, what is it?", [("Sinad", 0.1)])
    assert hi and (lo is None or lo.confidence < hi.confidence)


# ---------------------------------------------------------------- questions
@pytest.mark.parametrize("text,kind", [
    ("Can anyone explain why the reaction is exothermic?", "explanation"),
    ("What's the equation?", "recall"),
    ("Sanad, what's the value of x?", "calculation"),
    ("Why do you think the writer used that technique?", "opinion"),
    ("What is a catalyst?", "definition"),
    ("Can you give me an example of a renewable resource?", "example"),
    ("What's the difference between mitosis and meiosis?", "comparison"),
    ("Solve 2x + 7 = 19.", "calculation"),
])
def test_question_kinds(text, kind):
    sig = classify_question(text)
    assert sig.is_question and sig.kind == kind, sig


@pytest.mark.parametrize("text", ["Does that make sense?", "Any questions?", "Okay?", "Can everyone see my screen?"])
def test_checks_are_not_questions_to_answer(text):
    assert classify_question(text).check


def test_rhetorical_within_utterance():
    sig = classify_question("So why does this matter? Well, because without enzymes these reactions would be far too slow.")
    assert sig.rhetorical


def test_statements_are_not_questions():
    assert not classify_question("Natural selection acts on variation within a population.").is_question


def test_references():
    assert extract_reference("can you solve question three for me") == {"question_number": 3}
    assert extract_reference("look at Q4 b please") == {"question_number": 4, "part": "b"}
    assert extract_reference("try the last one") == {"ordinal": "last"}
    assert classify_question("Why does that happen?").deictic


def test_task_cues():
    assert detect_task_cue("Open your books to page 45 and do questions 1 through 6.")
    assert detect_task_cue("You have fifteen minutes.")
    assert detect_task_cue("For homework, finish the worksheet.")
    assert not detect_task_cue("Photosynthesis happens in the chloroplasts.")


# ---------------------------------------------------------------- maths
def test_find_problems():
    assert find_problems("3. Solve 2x + 7 = 19") == ["2x + 7 = 19"]
    assert find_problems("Find x when 5x − 3 = 2x + 9.") == ["5x - 3 = 2x + 9"]
    assert find_problems("speed = distance / time") == []
    assert find_problems("The meeting is at 3pm = good") == []


@pytest.fixture(scope="module")
def math():
    m = MathService()
    yield m
    m.close()


@pytest.mark.parametrize("expr,expected", [
    ("2x + 7 = 19", "x = 6"),
    ("x^2 - 5x + 6 = 0", "x = 2, x = 3"),
    ("120/8", "15"),
    ("solve([Eq(x+y,10), Eq(x-y,2)], [x,y])", "x = 6, y = 4"),
    ("sind(30)*12", "6"),
    ("sigfig(9.81*2.35, 3)", "23.1"),
])
def test_math_exact(math, expr, expected):
    r = math.run(expr)
    assert r.ok and r.value == expected, r


@pytest.mark.parametrize("evil", ["__import__('os').system('calc')", "x.__class__", "open('a')", "exec(1)", "getattr(x,1)"])
def test_math_sandbox_rejects(math, evil):
    assert not math.run(evil).ok


def test_math_timeout_recovers(math):
    r = math.run("factorint(2^400+1)")  # very slow -> killed, worker restarted
    assert math.run("1+1").value == "2"


# ---------------------------------------------------------------- memory / parsing
def test_numbered_items():
    items = parse_numbered_items("Exercise 4B\n1. Solve 3x - 5 = 10\n2) Solve 4(x+1) = 24\nQ3 a) speed\nb) unit")
    assert items[1].startswith("Solve 3x") and items[2].startswith("Solve 4") and "unit" in items[3]


def test_lenient_json():
    assert loads_lenient('{"a": [1, 2], "b": ["x", "y') == {"a": [1, 2], "b": ["x"]}


# ---------------------------------------------------------------- scheduler
def test_scheduler_preempts_background_for_answers():
    async def run():
        s = Scheduler()
        s.start()
        order = []

        async def slow():
            order.append("notes-start")
            await asyncio.sleep(1.0)
            order.append("notes-end")
            return "notes"

        async def fast():
            order.append("answer")
            return "answer"

        f1 = s.submit(NOTES, slow, key="notes", requeue=True)
        await asyncio.sleep(0.1)
        f2 = s.submit(DIRECT, fast)
        assert await f2 == "answer"
        assert await f1 == "notes"  # pre-empted, then re-run
        await s.stop()
        return order, s.stats

    order, stats = asyncio.run(run())
    assert order[:2] == ["notes-start", "answer"] and stats["preempted"] == 1


def test_scheduler_queue_is_bounded_and_coalesces():
    async def run():
        s = Scheduler(max_queue=3)  # not started: everything stays queued
        futs = [s.submit(ANSWER, lambda: asyncio.sleep(0), key="q") for _ in range(5)]
        others = [s.submit(NOTES, lambda: asyncio.sleep(0)) for _ in range(5)]
        await asyncio.sleep(0)
        return futs, others, s

    futs, others, s = asyncio.run(run())
    assert sum(not f.cancelled() for f in futs) == 1  # coalesced to the newest
    assert len(s._queue) == 3


# ---------------------------------------------------------------- thermal guard (simulated sensors)
def _guard(monkeypatch, temps, errors=None, perf=None):
    from glass import thermal

    changes = []
    g = thermal.ThermalGuard(lambda lvl, info: changes.append(lvl))
    seq = iter(temps)
    errs = iter(errors or [0] * len(temps))
    perfs = iter(perf or [100.0] * len(temps))
    monkeypatch.setattr(g, "_gpu", lambda: (next(seq), False))
    monkeypatch.setattr(g, "_new_driver_errors", lambda: next(errs))
    monkeypatch.setattr(g, "_cpu_perf", lambda: next(perfs))
    return g, changes


def test_thermal_steps_up_immediately(monkeypatch):
    g, changes = _guard(monkeypatch, [60, 80, 86, 92])
    for _ in range(4):
        g.sample(cpu_load=30)
    assert changes == ["warm", "hot", "critical"]


def test_thermal_driver_error_is_critical(monkeypatch):
    g, changes = _guard(monkeypatch, [55, 55], errors=[0, 3])
    g.sample(10)
    g.sample(10)
    assert g.level == "critical" and "driver" in g.info["reason"]


def test_thermal_cools_down_only_after_quiet_period(monkeypatch):
    from glass import thermal

    g, changes = _guard(monkeypatch, [88, 60, 60, 60])
    g.sample(10)
    assert g.level == "hot"
    g.sample(10)
    assert g.level == "hot"  # no flapping
    monkeypatch.setattr(thermal, "COOL_DOWN_S", 0)
    g.sample(10)
    assert g.level == "ok"


def test_thermal_cpu_throttling_detected(monkeypatch):
    g, _ = _guard(monkeypatch, [50] * 4, perf=[40.0] * 4)
    for _ in range(4):
        g.sample(cpu_load=60)
    assert g.level == "hot" and "CPU" in g.info["reason"]


def test_runtime_overrides_reduce_work():
    from glass.config import Settings
    from glass.thermal import RUNTIME
    import tempfile, pathlib

    s = Settings(pathlib.Path(tempfile.mkdtemp()) / "s.json")
    assert s.profile()["partial_every"] > 0
    s.runtime = dict(RUNTIME["hot"])
    p = s.profile()
    assert p["partial_every"] == 0 and p["notes_paused"] and not p["verify_answers"]


# ---------------------------------------------------------------- snip from a frozen frame
def test_snip_crops_frozen_frame(monkeypatch):
    import asyncio, time
    import numpy as np
    from glass.screen import ScreenService

    class S:
        def get(self, k, d=None):
            return {"ocr_engine": "windows"}.get(k, d)

        def profile(self):
            return {}

    svc = ScreenService(S(), lambda o: None, lambda *a: None)
    frame = np.zeros((1200, 1920, 4), np.uint8)
    frame[100:300, 200:600, :3] = 255  # bright box where the student selects
    frame[150:160, 250:550, :3] = 0
    svc._frozen = (frame, {"index": 1, "left": 0, "top": 0, "w": 1920, "h": 1200}, time.time())

    async def fake_read(img, lang="en-US"):
        fake_read.shape = img.shape
        return []

    monkeypatch.setattr(svc.ocr, "read", fake_read)
    png, text, blank = asyncio.run(svc.snip(200, 100, 400, 200))
    assert fake_read.shape[:2] == (200, 400) and not blank and svc._frozen is None


# ---------------------------------------------------------------- relevance: not every question needs an answer
import pytest as _pytest


@_pytest.mark.parametrize("text", [
    "Where is it?", "No questions?", "Any questions?", "Can you see my screen?", "Who is next?", "Have you finished?",
    "Is everyone here?", "Anyone?", "What is the answer?", "Can you hear me?", "What page are we on?", "Are you done?",
    "Shall we move on?", "How long do we have left?", "Is it working now?", "Who wants to go first?",
])
def test_classroom_talk_is_never_answered(text):
    s = classify_question(text)
    assert s.check or s.generic, (text, s)


@_pytest.mark.parametrize("text,kind", [
    ("What does MAC address stand for?", "acronym"),
    ("What does DNA stand for?", "acronym"),
    ("Where is the mitochondria found?", "recall"),
    ("Why is the sky blue?", "explanation"),
    ("How does a router forward packets?", "explanation"),
    ("Where is the CPU located on the motherboard?", "recall"),
])
def test_lesson_questions_are_answered(text, kind):
    s = classify_question(text)
    assert s.is_question and not s.check and not s.generic and s.kind == kind, (text, s)


def test_newest_question_cancels_stale_class_answer_but_not_directed():
    async def run():
        s = Scheduler()
        s.start()

        async def slow():
            await asyncio.sleep(5)
            return "old"

        directed = s.submit(DIRECT, slow, key="answer:q_dir")
        await asyncio.sleep(0.05)
        old = s.submit(ANSWER, slow, key="answer:q_old")
        n = s.cancel_where(lambda k, p: k.startswith("answer:q_") and p != DIRECT and k != "answer:q_new")
        await asyncio.sleep(0)
        res = (old.cancelled(), directed.cancelled(), n)
        await s.stop()
        return res

    old_cancelled, directed_cancelled, n = asyncio.run(run())
    assert old_cancelled and not directed_cancelled and n == 1


def test_similar_but_different_questions_are_not_repeats():
    import tempfile, pathlib
    from glass.answer import AnswerEngine
    from glass.config import Settings

    a = AnswerEngine(None, None, None, None, Settings(pathlib.Path(tempfile.mkdtemp()) / "s.json"), None, lambda *x, **k: None)
    assert not a._same_question("What does RAM stand for?", "What does MAC address stand for?")
    assert not a._same_question("What does ROM stand for?", "What does RAM stand for?")
    assert a._same_question("Sanad, what does RAM stand for?", "What does RAM stand for?")
    assert a._same_question("So what does RAM stand for?", "what does RAM stand for")


# ---------------------------------------------------------------- study texts & quote verification
FIX_TEXT = Path(__file__).parent / "fixtures" / "texts" / "christmas_carol_stave1.txt"


def test_quotes_found_and_verified():
    from glass.texts import find_quotes, normalise, quote_in

    corpus = normalise(FIX_TEXT.read_text(encoding="utf-8"))
    assert find_quotes("I don’t know, it isn’t clear.") == []  # apostrophes are not quotes
    qs = find_quotes('He is "Hard and sharp as flint" and “solitary as an oyster”.')
    assert qs == ["Hard and sharp as flint", "solitary as an oyster"]
    assert all(quote_in(q, corpus) for q in qs)
    assert quote_in("Hard and sharp as flint... solitary as an oyster", corpus)  # elision
    assert not quote_in("I have no use for such people as you", corpus)  # invented


def test_text_library_search(tmp_path):
    from glass.db import DB
    from glass.texts import chunk_text, extract_text

    db = DB(tmp_path / "t.db")
    chunks = chunk_text(extract_text(FIX_TEXT))
    assert chunks and chunks[0][0].startswith("STAVE I")
    db.add_text("english_lit", "A Christmas Carol", FIX_TEXT.name, chunks)
    hits = db.search_texts(["scrooge", "cold", "mean"], "english_lit")
    assert hits and "Scrooge" in hits[0]["content"] and hits[0]["title"] == "A Christmas Carol"
    assert db.search_texts(["scrooge"], "biology") == []


def test_docx_extraction(tmp_path):
    import zipfile

    from glass.texts import extract_text

    p = tmp_path / "notes.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Sampath climbs the guava tree.</w:t></w:r></w:p>"
                                        "<w:p><w:r><w:t>The monkeys arrive.</w:t></w:r></w:p></w:body></w:document>")
    assert "Sampath climbs the guava tree." in extract_text(p) and "monkeys" in extract_text(p)


# ---------------------------------------------------------------- register, compaction, commands
@_pytest.mark.parametrize("text", ["Nicki?", "Is Nicki here?", "And Omar?", "Where is Omar?", "Here, miss.",
                                   "Has anyone seen Ali?", "Okay, let me do the register."])
def test_register_is_attendance(text):
    from glass.detect import is_attendance

    assert is_attendance(text)


@_pytest.mark.parametrize("text", ["Is oxygen present in the reaction?", "Who is Sampath?", "Where is the CPU located?",
                                   "Is Sampath happy in the orchard?", "Correct?"])
def test_lesson_talk_is_not_attendance(text):
    from glass.detect import is_attendance

    assert not is_attendance(text)


def test_compaction_removes_chatter_keeps_lesson():
    from glass.detect import compact_for_context as c

    assert c("Nicki? Is Nicki here?") == ""
    assert c("Sorry, my internet is lagging.") == ""
    out = c("Okay. Can everyone see my screen? Right, so um today we are looking at, you know, the the guava orchard.")
    assert "screen" not in out and " um " not in out and "the the" not in out and "guava orchard" in out
    assert c("What does RAM stand for?") == "What does RAM stand for?"


@_pytest.mark.parametrize("text,cmds,ctx", [
    ("answer the question", ["answer"], ""), ("just answer it", ["answer"], ""), ("shorter", ["shorter"], ""),
    ("give quotes", ["quotes"], ""), ("natural selection", [], "natural selection"), ("question 4", [], "question 4"),
    ("thats wrong, its question 4", ["wrong"], "question 4"),
])
def test_context_box_commands(text, cmds, ctx):
    from glass.answer import parse_commands

    got_cmds, got_ctx = parse_commands(text)
    assert got_cmds == cmds and got_ctx == ctx


def test_meta_answers_are_caught():
    from glass.answer import META_SAY

    assert META_SAY.search("The question is asking about how Sampath feels.")
    assert META_SAY.search("To answer this, you need to look at chapter five.")
    assert not META_SAY.search("Sampath feels free because nobody controls him in the tree.")


def test_thermal_ignores_stray_gpu_flag(monkeypatch):
    from glass import thermal

    g = thermal.ThermalGuard(lambda *a: None)
    seq = iter([(51, True), (50, False), (51, True)])
    monkeypatch.setattr(g, "_gpu", lambda: next(seq))
    monkeypatch.setattr(g, "_new_driver_errors", lambda: 0)
    monkeypatch.setattr(g, "_cpu_perf", lambda: 100.0)
    monkeypatch.setattr(g, "_mac_limit", lambda: None)
    for _ in range(3):
        g.sample(10)
    assert g.level == "ok"
