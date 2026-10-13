"""End-to-end scenario harness: the REAL engine (VAD -> Whisper on GPU ->
detectors -> Ollama LLM -> SymPy -> notes/tasks) driven by synthesized
classroom audio and rendered worksheet images. Nothing is mocked.

  python -m tests.harness            # all scenarios, writes tests/report.json + report.md
  python -m tests.harness direct_name screen_question
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
AUDIO = HERE / "fixtures" / "audio"
SCREENS = HERE / "fixtures" / "screens"
os.environ.setdefault("GLASS_DATA_DIR", tempfile.mkdtemp(prefix="glass-test-"))

from glass.audio.capture import AudioChunk  # noqa: E402
from glass.bus import bus  # noqa: E402
from glass.engine import Engine  # noqa: E402
from glass.memory import ScreenObs, parse_numbered_items  # noqa: E402
from glass.mathsolve import find_problems  # noqa: E402
from glass.screen import OCR, lines_to_text  # noqa: E402

EVENTS: list[dict] = []


async def sink(evt: dict) -> None:
    EVENTS.append(evt)


def load_wav(path: Path) -> np.ndarray:
    import soxr

    with wave.open(str(path)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    return soxr.resample(x, sr, 16000).astype(np.float32)


async def feed(engine: Engine, samples: np.ndarray, source: str = "class", tail_silence: float = 2.2) -> float:
    """Feed audio in real time (32 ms chunks). Returns wall time when speech ended."""
    step = 512
    x = np.concatenate([samples, np.zeros(int(16000 * tail_silence), np.float32)])
    t_speech_end = 0.0
    n_speech = len(samples)
    t_start = time.perf_counter()
    for i in range(0, len(x), step):
        chunk = x[i:i + step]
        engine.inject_audio(AudioChunk(source, chunk, time.time()))
        if i <= n_speech < i + step:
            t_speech_end = time.time()
        target = t_start + (i + step) / 16000
        delay = target - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
    return t_speech_end or time.time()


async def ocr_image(name: str) -> ScreenObs:
    from PIL import Image

    img = np.array(Image.open(SCREENS / f"{name}.png").convert("RGBA"))[:, :, [2, 1, 0, 3]]
    lines = await OCR("windows").read(img)
    text = lines_to_text(lines)
    return ScreenObs(time.time(), text, parse_numbered_items(text), find_problems(text), f"test:{name}")


def answers_since(t: float) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in EVENTS:
        if e["type"] == "answer.update" and e["ts"] >= t and e["answer"]["t"] >= t - 0.5:
            out[e["answer"]["id"]] = e["answer"]
    return out


def first_say_time(aid: str, t_from: float) -> float | None:
    for e in EVENTS:
        if e["type"] == "answer.update" and e["answer"]["id"] == aid and e["answer"].get("say") and e["ts"] >= t_from:
            # wait until SAY line is complete (WHY started or done)
            if e["answer"].get("why") or e["answer"]["status"] == "done":
                return e["ts"]
    return None


async def wait_for(pred, timeout: float = 25.0, poll: float = 0.1):
    t = time.time()
    while time.time() - t < timeout:
        r = pred()
        if r:
            return r
        await asyncio.sleep(poll)
    return None


async def settled(get, timeout: float = 25.0, quiet: float = 3.0):
    """Wait until get() is non-empty and has stopped growing for `quiet` seconds."""
    res = await wait_for(get, timeout)
    if not res:
        return res
    n, t = len(res), time.time()
    while time.time() - t < quiet:
        await asyncio.sleep(0.2)
        cur = get()
        if len(cur) != n:
            n, t = len(cur), time.time()
    return get()


async def wait_ready(engine: Engine) -> None:
    print("waiting for models…", flush=True)
    ok = await wait_for(lambda: engine.status["asr"]["state"] == "ready" and engine.status["llm"]["state"] == "ready",
                        timeout=400, poll=0.5)
    if not ok:
        raise RuntimeError(f"models not ready: {engine.status}")
    print("models ready:", engine.status["asr"], engine.status["llm"], flush=True)


# ------------------------------------------------------------------- scenarios
class Result:
    def __init__(self, name: str):
        self.name = name
        self.checks: list[tuple[str, bool, str]] = []
        self.metrics: dict = {}

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.checks.append((label, bool(ok), detail))
        print(f"   [{'PASS' if ok else 'FAIL'}] {label} {('— ' + detail) if detail else ''}", flush=True)

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


async def new_session(engine: Engine, subject: str | None) -> None:
    if engine.session:
        await engine.stop_session()
    EVENTS.clear()
    await engine.start_session(subject, capture=False)


async def done_answer(t0: float, pred=lambda a: True, timeout: float = 25):
    def get():
        for a in answers_since(t0).values():
            if a["status"] in ("done", "error") and pred(a):
                return a
        return None
    return await wait_for(get, timeout)


async def sc_direct_name(engine: Engine) -> Result:
    r = Result("1. Teacher addresses Sanad directly")
    await new_session(engine, "chemistry")
    t0 = time.time()
    t_end = await feed(engine, load_wav(AUDIO / "direct_name.wav"))
    a = await done_answer(t0)
    att = [e for e in EVENTS if e["type"] == "attention"]
    r.check("name detected (high)", any(e["level"] == "high" for e in att), str([round(e["confidence"], 2) for e in att]))
    r.check("answer generated", a is not None and bool(a.get("say")), (a or {}).get("say", "")[:140])
    r.check("marked as directed", bool(a and a.get("directed")))
    r.check("explanation type", bool(a and a.get("kind") == "explanation"), (a or {}).get("kind", ""))
    r.check("mentions bonds/energy", bool(a and "bond" in (a["say"] + a["why"]).lower()))
    if a:
        ts = first_say_time(a["id"], t0)
        r.metrics["speech_end_to_answer_s"] = round(ts - t_end, 2) if ts else None
        r.metrics["llm_say_ms"] = a["latency"].get("say_ms")
        r.metrics["attention_after_speech_end_s"] = round(min(e["ts"] for e in att) - t_end, 2) if att else None
    w = await wait_for(lambda: any(e["type"] == "question.waiting" for e in EVENTS), 4)
    r.check("teacher-waiting detected after silence", bool(w))
    r.check("overlay mode = direct", engine.mode == "direct", engine.mode)
    return r


async def sc_general(engine: Engine) -> Result:
    r = Result("2. General class question")
    await new_session(engine, "biology")
    t0 = time.time()
    t_end = await feed(engine, load_wav(AUDIO / "general_question.wav"))
    a = await done_answer(t0)
    r.check("answer generated", a is not None and "mitochondri" in (a.get("say", "") + a.get("why", "")).lower(),
            (a or {}).get("say", "")[:120])
    r.check("not marked as directed", a is not None and not a.get("directed"))
    if a:
        ts = first_say_time(a["id"], t0)
        r.metrics["speech_end_to_answer_s"] = round(ts - t_end, 2) if ts else None
    return r


async def sc_rhetorical(engine: Engine) -> Result:
    r = Result("3. Rhetorical question / comprehension check")
    await new_session(engine, "biology")
    t0 = time.time()
    await feed(engine, load_wav(AUDIO / "rhetorical.wav"))
    await asyncio.sleep(3)
    modes = [e["mode"] for e in EVENTS if e["type"] == "mode" and e["ts"] >= t0]
    r.check("answer panel never elevated", not ({"question", "direct"} & set(modes)), str(modes))
    live = [a for a in answers_since(t0).values() if not a.get("rhetorical") and a.get("kind") != "none"]
    r.check("question demoted as self-answered/rhetorical", not live, f"{[(a['question'], a.get('answered_by')) for a in live]}")
    return r


async def sc_screen_question(engine: Engine) -> Result:
    r = Result("4. Question refers to something on screen (fusion)")
    await new_session(engine, "maths")
    obs = await ocr_image("worksheet")
    engine._on_screen_obs(obs)
    r.check("OCR found numbered items", 3 in obs.items, str(obs.items.get(3)))
    t0 = time.time()
    t_end = await feed(engine, load_wav(AUDIO / "screen_question.wav"))
    a = await done_answer(t0)
    r.check("resolved to on-screen question 3", bool(a and "screen item" in a.get("sources", [])), str((a or {}).get("sources")))
    r.check("verified exact result x = 6", bool(a and a.get("verified") and "6" in a.get("result", "")), (a or {}).get("result", ""))
    r.check("spoken answer says six", bool(a and ("6" in a["say"] or "six" in a["say"].lower())), (a or {}).get("say", ""))
    if a:
        r.metrics["verified_ms"] = a["latency"].get("verified_ms")
        ts = first_say_time(a["id"], t0)
        r.metrics["speech_end_to_answer_s"] = round(ts - t_end, 2) if ts else None
    return r


async def sc_equation_spoken(engine: Engine) -> Result:
    r = Result("5. Teacher introduces an equation verbally (calculation)")
    await new_session(engine, "physics")
    t0 = time.time()
    t_end = await feed(engine, load_wav(AUDIO / "equation_spoken.wav"))
    a = await done_answer(t0)
    r.check("calculation verified by SymPy", bool(a and a.get("verified")), f"calc={a and a.get('calc')} result={a and a.get('result')}")
    r.check("answer is 15 m/s", bool(a and "15" in a.get("say", "")), (a or {}).get("say", ""))
    if a:
        ts = first_say_time(a["id"], t0)
        r.metrics["speech_end_to_answer_s"] = round(ts - t_end, 2) if ts else None
    return r


async def sc_written_equation(engine: Engine) -> Result:
    r = Result("6. Equation written on screen, not read aloud")
    await new_session(engine, "maths")
    obs = await ocr_image("worksheet")
    engine._on_screen_obs(obs)
    t0 = time.time()
    a = await engine.answers.ask("question 5")
    a = await done_answer(t0)
    r.check("solves x^2 - 5x + 6 = 0 exactly", bool(a and a.get("verified") and "2" in a["result"] and "3" in a["result"]),
            (a or {}).get("result", ""))
    return r


async def sc_tasks(engine: Engine) -> Result:
    r = Result("7+8. Teacher assigns work, then changes the task")
    await new_session(engine, "maths")
    await feed(engine, load_wav(AUDIO / "task.wav"))
    ev = await settled(lambda: [e for e in EVENTS if e["type"] == "task.update" and e.get("task")], 25)
    t = ev[-1]["task"] if ev else {}
    r.check("task detected", bool(t), t.get("description", ""))
    r.check("page 45", "45" in str(t.get("source", "")), str(t.get("source")))
    r.check("questions 1–6", bool(t.get("questions")) and "1" in str(t["questions"]) and "6" in str(t["questions"]), str(t.get("questions")))
    r.check("15 minutes allowed", t.get("time_minutes") == 15, str(t.get("time_minutes")))
    r.check("timer not auto-started (start not stated)", not t.get("timer_start"))
    n_before = len(EVENTS)
    await feed(engine, load_wav(AUDIO / "task_change.wav"))
    ev2 = await settled(lambda: [e for e in EVENTS[n_before:] if e["type"] == "task.update" and e.get("task")], 25)
    t2 = ev2[-1]["task"] if ev2 else {}
    r.check("change detected as replacement", t2.get("relation") == "replace" and t2.get("id") != t.get("id"), str(t2.get("relation")))
    r.check("homework due Monday", bool(t2.get("is_homework")) and "monday" in str(t2.get("deadline", "")).lower(), str(t2.get("deadline")))
    r.check("old task kept in history", any(h.get("id") == t.get("id") for h in (ev2[-1].get("history") or [])) if ev2 else False)
    return r


async def sc_vague_then_context(engine: Engine) -> Result:
    r = Result("16/17. Vague question -> uncertainty -> instant context")
    await new_session(engine, "biology")
    t0 = time.time()
    await feed(engine, load_wav(AUDIO / "vague.wav"))
    a = await done_answer(t0)
    r.check("flags uncertainty (low conf or UNSURE)", bool(a and (a["conf"] == "low" or a["unsure"])),
            f"conf={a and a['conf']} unsure={a and a['unsure']}")
    t1 = time.time()
    await engine.answers.clarify("natural selection")
    b = await done_answer(t1)
    txt = (b or {}).get("say", "") + (b or {}).get("why", "")
    r.check("re-answered with the student's context", bool(b and b.get("clarifications") == ["natural selection"]))
    r.check("answer now about survival/reproduction", any(k in txt.lower() for k in ("surviv", "reproduc", "adapt")), txt[:160])
    r.check("confidence improved", bool(b and b["conf"] in ("high", "medium")), (b or {}).get("conf", ""))
    if b:
        r.metrics["context_to_answer_ms"] = b["latency"].get("say_ms")
    return r


async def sc_notes_catchup(engine: Engine) -> Result:
    r = Result("9/15/18. Explanation -> notes + catch-up")
    await new_session(engine, "biology")
    await feed(engine, load_wav(AUDIO / "explain.wav"))
    await feed(engine, load_wav(AUDIO / "general_question.wav"))
    await asyncio.sleep(1)
    t0 = time.time()
    await engine.intel.update_notes(final=True)
    n = engine.intel.notes
    terms = " ".join(d["term"].lower() for d in n.get("definitions", []))
    r.check("notes contain a definition of natural selection", "natural selection" in terms, terms)
    r.check("notes produced in reasonable time", time.time() - t0 < 30, f"{time.time() - t0:.1f}s")
    r.metrics["notes_s"] = round(time.time() - t0, 1)
    t1 = time.time()
    await engine.answers.catchup(3)
    c = await done_answer(t1, lambda a: a["mode"] == "catchup")
    r.check("catch-up summary produced", bool(c and len(c.get("body", "")) > 40), (c or {}).get("body", "")[:160].replace("\n", " "))
    return r


async def sc_silence(engine: Engine) -> Result:
    r = Result("11. Audio stops (silence) — no hallucinated speech")
    await new_session(engine, "physics")
    await feed(engine, load_wav(AUDIO / "silence.wav"), tail_silence=1)
    await feed(engine, (np.random.randn(16000 * 3) * 0.003).astype(np.float32), tail_silence=1)  # low noise floor
    finals = [e for e in EVENTS if e["type"] == "transcript.final"]
    r.check("no transcript produced from silence/noise", not finals, str([e["text"] for e in finals]))
    return r


async def sc_blurry(engine: Engine) -> Result:
    r = Result("19. Small worksheet text OCR")
    obs = await ocr_image("small_blurry")
    r.check("numbered item recovered", 3 in obs.items, repr(obs.items.get(3, ""))[:100])
    r.check("120 m and 8 s readable", "120" in obs.text and "8" in obs.text, obs.text[:100].replace("\n", " | "))
    return r


async def sc_snip(engine: Engine) -> Result:
    r = Result("Snip & solve: worksheet (OCR), diagram (vision), blank capture")
    await new_session(engine, "maths")
    from PIL import Image
    import io

    def png_of(name, crop=None):
        im = Image.open(SCREENS / f"{name}.png").convert("RGB")
        if crop:
            im = im.crop(crop)
        b = io.BytesIO(); im.save(b, "PNG")
        return b.getvalue(), np.array(im.convert("RGBA"))[:, :, [2, 1, 0, 3]]

    # 1) worksheet questions 1-3 via OCR
    png, arr = png_of("worksheet", (0, 0, 1600, 560))
    text = lines_to_text(await OCR("windows").read(arr))
    t0 = time.time()
    a = await engine.answers.solve_snip(png, text, use_vision=False)
    body = a.get("body", "")
    r.check("OCR path: all three answers present (x=5, x=5, x=6)",
            a["status"] == "done" and len(re.findall(r"x\s*(?:=|equals)\s*5", body)) >= 2 and re.search(r"x\s*(?:=|equals)\s*6", body) and "CALC" not in body, body[:200].replace(chr(10), " "))
    r.check("OCR path: verified by SymPy", a["verified"], a["result"])
    r.metrics["ocr_snip_s"] = round(time.time() - t0, 2)

    # 2) diagram via the vision model (right-angled triangle, legs 6 and 8 -> x = 10)
    png, _ = png_of("triangle")
    t0 = time.time()
    await engine.answers.prepare_vision()
    a = await engine.answers.solve_snip(png, "", use_vision=True)
    body = a.get("body", "")
    r.check("vision path: reads the diagram and finds x = 10", a["status"] == "done" and "10" in body, body[:220].replace(chr(10), " "))
    r.metrics["vision_snip_s"] = round(time.time() - t0, 2)
    # the answer model is re-warmed in the background right after vision use
    t1 = time.time()
    await asyncio.sleep(0.2)
    b = await engine.answers.ask("what is 12 times 12")
    b = await done_answer(t1)
    r.metrics["next_answer_after_vision_s"] = round(time.time() - t1, 2)

    # 3) blank capture (locked screen): must refuse, never invent a question
    blank = io.BytesIO(); Image.new("RGB", (900, 300), "black").save(blank, "PNG")
    a = await engine.answers.solve_snip(blank.getvalue(), "", use_vision=True)
    r.check("blank image refused (no invented content)", a["status"] == "error", a.get("error") or a.get("body", "")[:120])
    return r


async def sc_relevance(engine: Engine) -> Result:
    r = Result("Relevance + newest question wins")
    await new_session(engine, "cs")
    t0 = time.time()
    t_end = await feed(engine, load_wav(AUDIO / "classroom_talk.wav"))
    await asyncio.sleep(4)
    shown = [a for a in answers_since(t0).values() if a["status"] != "superseded" and not a.get("rhetorical")]
    finals = [e["text"] for e in EVENTS if e["type"] == "transcript.final" and e["ts"] >= t0]
    r.check("only the real question got an answer", len(shown) == 1 and "RAM" in shown[0]["question"],
            f"{[a['question'] for a in shown]} | heard: {finals}")
    r.check("answer is the expansion", bool(shown) and "random access memory" in shown[0]["say"].lower(), shown[0]["say"] if shown else "")
    if shown:
        ts = first_say_time(shown[0]["id"], t0)
        r.metrics["speech_end_to_answer_s"] = round(ts - t_end, 2) if ts else None
    t1 = time.time()
    t_end = await feed(engine, load_wav(AUDIO / "two_questions.wav"))
    await asyncio.sleep(4)
    rom = [a for a in answers_since(t1).values() if "ROM" in a["question"] and a["status"] == "done"]
    r.check("newer question answered (read-only memory)", bool(rom) and "read" in rom[0]["say"].lower(), rom[0]["say"] if rom else "none")
    if rom:
        ts = first_say_time(rom[0]["id"], t1)
        r.metrics["newer_question_answer_s"] = round(ts - t_end, 2) if ts else None
        r.metrics["newer_question_queued_ms"] = rom[0]["latency"].get("queued_ms")
    return r


SCENARIOS = {
    "direct_name": sc_direct_name, "general": sc_general, "rhetorical": sc_rhetorical,
    "screen_question": sc_screen_question, "equation_spoken": sc_equation_spoken, "written_equation": sc_written_equation,
    "tasks": sc_tasks, "vague": sc_vague_then_context, "notes": sc_notes_catchup, "silence": sc_silence, "blurry": sc_blurry, "snip": sc_snip, "relevance": sc_relevance,
}


async def main(names: list[str]) -> int:
    bus.add_sink(sink)
    engine = Engine()
    await engine.start()
    engine.settings.update({"consent_acknowledged": True, "screen_enabled": False})
    await wait_ready(engine)
    results = []
    for n in names or list(SCENARIOS):
        print(f"\n== {n}", flush=True)
        try:
            results.append(await SCENARIOS[n](engine))
        except Exception as e:
            import traceback

            traceback.print_exc()
            res = Result(n)
            res.check("scenario ran", False, f"{type(e).__name__}: {e}")
            results.append(res)
    await engine.stop_session()
    report = {
        "when": time.strftime("%Y-%m-%d %H:%M"),
        "gpu": engine.metrics.get("gpu"), "asr": engine.status["asr"], "llm": engine.status["llm"],
        "scenarios": [{"name": r.name, "passed": r.passed, "checks": r.checks, "metrics": r.metrics} for r in results],
    }
    (HERE / "report.json").write_text(json.dumps(report, indent=2, default=str))
    lines = [f"# GLASS integration report — {report['when']}", "",
             f"GPU: {report['gpu']} · ASR: {report['asr'].get('model')} on {report['asr'].get('device')} · LLM: {report['llm'].get('model')}", ""]
    for r in results:
        lines.append(f"## {'✅' if r.passed else '❌'} {r.name}")
        lines += [f"- {'PASS' if ok else 'FAIL'} — {label}{(': ' + d) if d else ''}" for label, ok, d in r.checks]
        if r.metrics:
            lines.append(f"- metrics: `{json.dumps(r.metrics)}`")
        lines.append("")
    (HERE / "report.md").write_text("\n".join(lines), encoding="utf-8")
    passed = sum(r.passed for r in results)
    print(f"\n{passed}/{len(results)} scenarios passed. Report: tests/report.md")
    t = time.time()
    await engine.shutdown()
    print(f"engine shutdown in {time.time() - t:.2f}s", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    import faulthandler

    code = asyncio.run(main(sys.argv[1:]))
    faulthandler.dump_traceback_later(15, exit=True)  # report anything that blocks interpreter exit
    sys.exit(code)
