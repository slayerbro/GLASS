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

    async def fake_read(img, lang="en-US", **kw):
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


# ---------------------------------------------------------------- feedback round 3: snips, references, topics, lesson reset
WORKSHEET = """Solve these equations.
1. 2x + 7 = 19
2 f(x) = x² + 3
(a) Find f(3)
(b) Solve f(x) = 12 (2 marks)
.............
(Total for Question 2 is 4 marks)
3(a) Expand 3(x+2)
(b) Factorise x2 - 9"""


def test_worksheet_splits_into_separate_questions():
    from glass.mathsolve import normalise
    from glass.memory import split_questions

    qs = split_questions(normalise(WORKSHEET))
    assert [q["label"] for q in qs] == ["1", "2(a)", "2(b)", "3(a)", "3(b)"]
    assert qs[1]["text"] == "Find f(3)" and "f(x) = x^2 + 3" in qs[1]["stem"]  # f(3) is not a mark
    assert qs[2]["text"] == "Solve f(x) = 12"  # "(2 marks)" and answer lines removed
    assert split_questions("Find the gradient of y = 3x + 4") == [{"label": "1", "text": "Find the gradient of y = 3x + 4"}]


def test_reference_to_a_part_includes_its_stem():
    import time
    from glass.mathsolve import normalise
    from glass.memory import LessonMemory, ScreenObs, parse_numbered_items

    m = LessonMemory()
    t = normalise(WORKSHEET)
    m.add_screen(ScreenObs(time.time(), t, parse_numbered_items(t)))
    r = m.resolve_reference(extract_reference("answer question 2b"))
    assert r.startswith("2(b)") and "x^2 + 3" in r and "Solve f(x) = 12" in r and "Find f(3)" not in r


def test_superscripts_and_flattened_powers():
    from glass.mathsolve import flat_powers, normalise
    from glass.screen import fix_power_marks

    assert normalise("x² + y³ - z⁻¹ + 2¹⁰") == "x^2 + y^3 - z^(-1) + 2^10"
    assert flat_powers("3. Factorise x2 - 9\nH2O and page 45") == ["x2"]  # only maths lines, lowercase
    assert fix_power_marks("x? + 5x + 6 = 0") == "x^2 + 5x + 6 = 0"
    assert fix_power_marks("Is y = 3 or what is x?") == "Is y = 3 or what is x?"  # a real question mark


@pytest.mark.parametrize("text,topic,subject", [
    ("the topic is concentration", "concentration", None),
    ("topic is concentration in chemistry", "concentration", "chemistry"),
    ("we're doing moles", "moles", None),
    ("this lesson is about natural selection", "natural selection", None),
    ("the lesson is chemistry", None, "chemistry"),
    ("it's about natural selection", None, None),  # context for the answer, not the lesson
    ("what is the topic?", None, None),
])
def test_topic_statements(text, topic, subject):
    from glass.answer import parse_topic

    assert parse_topic(text) == (topic, subject)


def test_snip_answers_pair_with_their_questions():
    from glass.answer import pair_parts

    qs = [{"label": "1", "text": "a"}, {"label": "2(a)", "text": "b"}, {"label": "2(b)", "text": "c"}]
    body = "### 1\n**Answer:** x = 6\nSubtract 7\nDivide by 2\n\n### 2a\n**Answer:** 12\n\n**2(b)**\n**12**\n"
    parts = pair_parts(qs, body)
    assert [p["label"] for p in parts] == ["1", "2(a)", "2(b)"]
    assert parts[0]["answer"] == "**Answer:** x = 6\n\nSubtract 7\n\nDivide by 2"  # each line stays its own line
    assert parts[2]["answer"] == "**12**"  # a bold number is not a question heading
    assert pair_parts([{"label": "1", "text": "q"}], "It shows a cell.")[0]["answer"] == "It shows a cell."


class _FakeSettings:
    def __init__(self):
        self.d = {"student_name": "Sanad", "subjects": [{"id": "chemistry", "name": "Chemistry", "board": ""}]}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def profile(self):
        return {"context_chars": 3000, "vision_enabled": True}

    def subject_by_id(self, sid):
        return next((s for s in self.d["subjects"] if s["id"] == sid), None)


def _answer_engine(monkeypatch):
    from glass.answer import AnswerEngine
    from glass.memory import LessonMemory

    eng = AnswerEngine(None, Scheduler(), None, LessonMemory(), _FakeSettings(), None, lambda *a, **k: None)
    calls = []

    async def fake_run(ans, question, ref, prio, extra, instructions=None):
        calls.append(("run", question, ref, list(extra), instructions))
        ans["status"] = "done"

    async def fake_md(ans, system, user, prio, **kw):
        calls.append(("md", user))
        ans["body"] = "### 2(b)\n**Answer:** x = 3\nx^2 = 9"
        ans["status"] = "done"

    async def fake_solve(exprs, limit=4):
        return []

    monkeypatch.setattr(eng, "_run_answer", fake_run)
    monkeypatch.setattr(eng, "_stream_markdown", fake_md)
    monkeypatch.setattr(eng, "_solve", fake_solve)
    return eng, calls


def test_answer_question_2b_is_not_swallowed_by_the_fix_box(monkeypatch):
    import time
    from glass.mathsolve import normalise
    from glass.memory import ScreenObs, parse_numbered_items

    eng, calls = _answer_engine(monkeypatch)
    # a class question is on the overlay, so the box is on "Fix"
    old = eng._new("question", question="What is a catalyst?", status="done", say="It speeds up a reaction.", qid="q_1")
    eng.mem.active_question = {"id": "q_1", "t": time.time(), "text": "What is a catalyst?", "answer_id": old["id"]}
    t = normalise(WORKSHEET)
    eng.mem.add_screen(ScreenObs(time.time(), t, parse_numbered_items(t)))
    ans = asyncio.run(eng.clarify("answer question 2b"))
    assert ans["mode"] == "snip" and ans["title"] == "Question 2(b)"
    assert [q["label"] for q in ans["questions"]] == ["2(b)"] and "Solve f(x) = 12" in ans["questions"][0]["text"]
    assert ans["parts"][0]["answer"].startswith("**Answer:** x = 3")
    assert not any(c[0] == "run" for c in calls)  # did NOT re-answer the catalyst question


def test_question_reference_without_screen_says_how_to_fix_it(monkeypatch):
    eng, calls = _answer_engine(monkeypatch)
    ans = asyncio.run(eng.ask("answer question 2b"))
    assert ans["status"] == "error" and "Snip" in ans["error"] and not calls


def test_topic_statement_sticks_and_redoes_the_answer(monkeypatch):
    import time

    eng, calls = _answer_engine(monkeypatch)
    old = eng._new("question", question="Why does it change?", status="done", say="Because of heat.", qid="q_2")
    eng.mem.active_question = {"id": "q_2", "t": time.time(), "text": "Why does it change?", "answer_id": old["id"]}
    asyncio.run(eng.clarify("the topic is concentration"))
    assert eng.mem.topic == "concentration" and eng.mem.topic_locked
    assert calls and calls[-1][0] == "run" and "the lesson topic is concentration" in calls[-1][3]
    assert "concentration" in eng._subject_line() and "said this is the topic" in eng._subject_line()


def test_fix_box_with_nothing_to_fix_sets_the_topic(monkeypatch):
    eng, calls = _answer_engine(monkeypatch)
    ans = asyncio.run(eng.clarify("concentration"))
    assert eng.mem.topic == "concentration" and ans["kind"] == "note" and "concentration" in ans["say"] and not calls


def test_ending_a_lesson_clears_it_and_the_next_one_starts_fresh(monkeypatch, tmp_path):
    import sys
    import types

    sys.modules.setdefault("onnxruntime", types.ModuleType("onnxruntime"))  # not needed: audio is not started
    import glass.engine as E
    from glass.bus import bus
    from glass.config import Settings
    from glass.db import DB
    from glass.intel import LessonIntel
    from glass.memory import Utterance

    events = []
    finals = []

    async def fake_notes(self, final=False):
        finals.append((self.mem.topic, self.session_id))

    monkeypatch.setattr(E, "Settings", lambda: Settings(tmp_path / "settings.json"))
    monkeypatch.setattr(E, "DB", lambda path: DB(tmp_path / "glass.db"))
    monkeypatch.setattr(LessonIntel, "update_notes", fake_notes)

    async def run():
        bus.bind(asyncio.get_running_loop())
        bus.subscribe("*", lambda e: events.append(e))
        eng = E.Engine()
        monkeypatch.setattr(eng, "_start_audio", lambda capture=True: None)
        monkeypatch.setattr(eng, "_stop_audio", lambda: None)
        eng.settings.update({"consent_acknowledged": True})
        s1 = await eng.start_session("chemistry", capture=False)
        eng.set_topic("concentration")
        eng.mem.add_utterance(Utterance("u1", "class", "Concentration is moles per volume.", 0, 1))
        eng.answers._new("question", question="What is concentration?")
        await eng.stop_session()
        await asyncio.sleep(0.05)
        assert eng.mem.topic == "" and eng.mem.subject_id is None and not eng.answers.answers
        assert finals == [("concentration", s1["id"])]  # the final notes pass kept the ended lesson's memory
        events.clear()
        s2 = await eng.start_session(None, capture=False)
        await asyncio.sleep(0.05)
        state = [e for e in events if e["type"] == "lesson.state"][-1]["state"]
        assert state["subject_id"] is None and state["topic"] == "" and not state["topic_locked"]
        assert any(e["type"] == "lesson.reset" for e in events) and s2["id"] != s1["id"]
        await eng.stop_session()
        eng.math.close()
        eng.db.close()

    asyncio.run(run())


def test_new_screen_questions_are_offered_once(monkeypatch):
    import time
    import types
    from glass.mathsolve import find_problems, normalise
    from glass.memory import ScreenObs, parse_numbered_items
    import sys

    sys.modules.setdefault("onnxruntime", types.ModuleType("onnxruntime"))
    import glass.engine as E

    sent = []
    monkeypatch.setattr(E.bus, "emit", lambda type_, **p: sent.append((type_, p)))
    eng = E.Engine.__new__(E.Engine)
    eng.session, eng.paused = {"id": "s"}, False
    eng._screen_seen, eng._screen_offer = set(), None
    eng.settings = types.SimpleNamespace(get=lambda k, d=None: False)
    t = normalise(WORKSHEET)
    obs = ScreenObs(time.time(), t, parse_numbered_items(t), find_problems(t))
    eng._offer_screen_questions(obs)
    eng._offer_screen_questions(obs)  # the same worksheet read again: no second offer
    offers = [p for k, p in sent if k == "screen.questions"]
    assert len(offers) == 1 and [q["label"] for q in offers[0]["questions"]] == ["1", "2(a)", "2(b)", "3(a)", "3(b)"]


def test_answer_lines_stay_separate_but_display_maths_is_kept():
    from glass.answer import _paragraphs

    assert _paragraphs("**Answer:** 6\nSubtract 7") == "**Answer:** 6\n\nSubtract 7"
    assert _paragraphs("Working:\n$$\nx = 1\n$$\ndone") == "Working:\n\n$$\nx = 1\n$$\n\ndone"


# ---------------------------------------------------------------- feedback round 4: powers read from the image, exact maths
def _render_line(segments, size=28):
    """Draw one worksheet line with real superscripts (small and raised, as slides and PDFs
    print them) and return (BGRA image, OCR-style line with word boxes). The words carry
    what an OCR engine typically returns for a superscript: junk, a flattened digit or nothing."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    f, fs = ImageFont.load_default(size=size), ImageFont.load_default(size=int(size * 0.62))
    im = Image.new("RGB", (1100, size * 3), "white")
    d = ImageDraw.Draw(im)
    x, y, words = 20, size, []
    for text, sup, ocr in segments:  # ocr: how the OCR engine reports this piece (None = same as text)
        font = fs if sup else f
        yy = y - int(size * 0.14) if sup else y
        start = x
        d.text((x, yy), text, font=font, fill="black")
        x += d.textlength(text, font=font)
        if ocr is None:
            for wd in text.split():
                wx = start + d.textlength(text[:text.index(wd)], font=font)
                words.append({"text": wd, "x": wx, "y": y, "w": d.textlength(wd, font=font), "h": size})
        elif ocr:  # glue onto the previous word, like an engine reading "x²" as "x?"
            words[-1]["text"] += ocr
            words[-1]["w"] = x - words[-1]["x"]
    a = np.array(im)
    img = np.dstack([a[:, :, ::-1], np.full(a.shape[:2], 255, np.uint8)])
    line = {"text": " ".join(w["text"] for w in words), "x": 20, "y": y, "w": x - 20, "h": size, "words": words}
    return img, line


@pytest.mark.parametrize("segments,expected", [
    ([("Solve x", 0, None), ("2", 1, "?"), (" + 5x + 6 = 0", 0, None)], "Solve x^2 + 5x + 6 = 0"),  # junk
    ([("Expand (x + 3)", 0, None), ("2", 1, "")], "Expand (x + 3)^2"),  # dropped entirely
    ([("Simplify 4a", 0, None), ("3", 1, "2"), (" × 2a", 0, None), ("2", 1, "°")], "Simplify 4a^3 × 2a^2"),  # wrong digit
    ([("Work out 5", 0, None), ("2", 1, "2"), (" + 2", 0, None), ("3", 1, "3")], "Work out 5^2 + 2^3"),  # flattened
])
def test_powers_are_read_from_the_image(segments, expected):
    from glass import superscript as S

    img, line = _render_line(segments)
    gray = img[:, :, :3].astype("float32").mean(axis=2)
    exps = S.find_exponents(gray, [line])
    for e in exps:  # no OCR engine here: the font comparison reads them
        e.text = S.classify_digits(e.crop)
    fixed, unreadable = S.apply([line], exps)
    assert line["text"] == expected and not unreadable, (line["text"], fixed, unreadable)


def test_degrees_quotes_and_apostrophes_are_not_powers():
    from glass import superscript as S

    for text in ["The angle is 30° and it's John's “book”.", "Use * to multiply: 10% of 20 = 2", "x - y = -3, ratio 2 : 3"]:
        img, line = _render_line([(text, 0, None)])
        gray = img[:, :, :3].astype("float32").mean(axis=2)
        exps = S.find_exponents(gray, [line])
        for e in exps:
            e.text = S.classify_digits(e.crop)
        before = line["text"]
        S.apply([line], exps)
        assert line["text"] == before, (before, line["text"])


def test_unreadable_power_is_reported_not_guessed():
    from glass import superscript as S

    img, line = _render_line([("Solve x", 0, None), ("2", 1, "?"), (" + 1 = 5", 0, None)])
    gray = img[:, :, :3].astype("float32").mean(axis=2)
    exps = S.find_exponents(gray, [line])
    assert len(exps) == 1
    fixed, unreadable = S.apply([line], exps)  # nothing could read the digit
    assert not fixed and unreadable == ["x"] and line["text"] == "Solve x? + 1 = 5"


@pytest.mark.parametrize("question,stem,expected", [
    ("Solve x^2 + 5x + 6 = 0", "", "x = -3, x = -2"),
    ("Expand (x + 3)^2", "", "x^2 + 6x + 9"),
    ("Simplify 4a^3 x 2a^2", "", "8a^5"),  # OCR read × as x
    ("Work out 5^2 + 2^3", "", "33"),
    ("Factorise x^2 - 9", "", "(x - 3)(x + 3)"),
    ("Find f(3)", "f(x) = x^2 + 3", "12"),
    ("Solve f(x) = 12", "f(x) = x^2 + 3", "x = -3, x = 3"),
    ("Solve 3x + 2 > 11", "", "x > 3"),
    ("Solve the simultaneous equations 2x + y = 7 and x - y = 2", "", "x = 3, y = 1"),
    ("Work out 15% of 80", "", "12"),
    ("Differentiate 3x^3 - 2x + 1", "", "9x^2 - 2"),
    ("Expand and simplify (2x - 1)(x + 4)", "", "2x^2 + 7x - 4"),
    ("Work out the value of 3^4 - 2 x 5", "", "71"),
])
def test_questions_are_worked_out_exactly(math, question, stem, expected):
    from glass.mathsolve import plan_math, tidy_result

    plan = plan_math(question, stem)
    assert plan, question
    r = math.run(plan[0])
    assert r.ok and tidy_result(r.value) == expected, (plan, r)


def test_no_exact_answer_for_wordy_questions():
    from glass.mathsolve import plan_math

    assert plan_math("Describe the trend shown in the graph") == []
    assert plan_math("Factorise x2 - 9") == ["factor(x2 - 9)"]  # a flattened power is refused by SymPy, never mis-solved
