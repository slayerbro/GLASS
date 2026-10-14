"""Engine orchestrator: owns every service and wires the real-time pipeline.

Threads:  audio callbacks -> audio queue -> DSP thread (VAD/segmenter) -> ASR thread
Asyncio:  transcript handling, detectors, answer/notes/task jobs, screen loop,
          metrics, UI event streaming.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import re
import sys
import threading
import time
from typing import Any

import psutil

from .answer import AnswerEngine
from .audio.asr import ASRWorker, Segmenter, TranscriptSegment
from .audio.capture import AudioSource, list_devices
from .audio.vad import SileroVAD
from .bus import bus
from .config import DB_PATH, Settings
from .db import DB
from .detect import (RHETORICAL_FOLLOW, NameDetector, classify_question, compact_for_context, content_words,
                     detect_task_cue, is_attendance)
from .texts import QUOTE_REQUEST
from .intel import LessonIntel, notes_markdown
from .llm import VISION, OllamaClient, Scheduler
from .mathsolve import MathService
from .memory import LessonMemory, ScreenObs, Utterance, label_key, split_questions
from .screen import ScreenService, list_monitors, list_windows
from .thermal import RUNTIME as THERMAL_RUNTIME
from .thermal import ThermalGuard

log = logging.getLogger("glass.engine")

WAIT_AFTER_QUESTION = 1.4  # s of silence after a question => the teacher is waiting for an answer


class Engine:
    def __init__(self) -> None:
        self.settings = Settings()
        self.db = DB(DB_PATH)
        self.mem = LessonMemory()
        self.llm = OllamaClient(self.settings.get("ollama_url"))
        self.sched = Scheduler()
        self.math = MathService()
        self.answers = AnswerEngine(self.llm, self.sched, self.math, self.mem, self.settings, self.db, bus.emit)
        self.intel = LessonIntel(self.llm, self.sched, self.mem, self.settings, self.db, bus.emit)
        self.intel.after_job = self.answers.warm_prefix
        self.answers.on_answer = self.maybe_ask_for_book
        self.answers.on_topic = self.set_topic
        self.screen = ScreenService(self.settings, self._on_screen_obs, self._screen_status, self._on_vision_request)
        self.names = NameDetector(self.settings.get("student_name"), self.settings.get("name_aliases"))
        self.asr = ASRWorker(self._on_segment_thread, self._asr_status)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.audio_q: "queue.Queue" = queue.Queue(maxsize=600)  # ~19 s of 32 ms chunks: bounded backpressure
        self.sources: dict[str, AudioSource] = {}
        self.segmenters: dict[str, Segmenter] = {}
        self._dsp_thread: threading.Thread | None = None
        self._dsp_stop = threading.Event()
        self.session: dict | None = None
        self.paused = False
        self.mode = "idle"
        self._mode_since = time.time()
        self.speaking: dict[str, bool] = {"class": False, "me": False}
        self.last_activity: dict[str, float] = {"class": 0.0, "me": 0.0}
        self.speech_start: dict[str, float] = {"class": 0.0, "me": 0.0}
        self.pending_q: dict | None = None  # {"answer_id", "t_end", "waiting", "utt_id"}
        self.attention: dict | None = None
        self._last_task_t = 0.0
        self._last_help_t = 0.0
        self._partial_alerted: set[str] = set()
        self.status: dict[str, Any] = {
            "audio": {"state": "off"}, "mic": {"state": "off"}, "asr": {"state": "not_loaded"},
            "llm": {"state": "checking"}, "vision": {"state": "unknown"}, "screen": {"state": "off"},
            "thermal": {"level": "ok"},
        }
        self.thermal = ThermalGuard(self._on_thermal_thread)
        self._thermal_paused = False  # capture paused by the guard (not by the student)
        self._thermal_screen_paused = False
        self.metrics: dict[str, Any] = {}
        self._vision_cache: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._finalising: set[asyncio.Task] = set()
        self._task_pending = False
        self._task_due = 0.0
        self._register_until = 0.0
        self._asked: set[str] = set()
        self._screen_seen: set[str] = set()  # questions on screen already offered this lesson
        self._screen_offer: dict | None = None  # {"id", "text"} of the latest offer

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        bus.bind(self.loop)
        self.sched.start()
        self._spawn(self._init_models())
        self._spawn(self._ticker())
        self._spawn(self._metrics_loop())
        self.db.purge_older_than(int(self.settings.get("retention_days") or 0))

    async def shutdown(self) -> None:
        await self.stop_session()
        # graceful: let the final notes pass of a just-ended lesson finish (bounded)
        if self._finalising:
            await asyncio.wait(list(self._finalising), timeout=30)
        self.asr.stop()
        await self.sched.stop()
        await self.llm.close()
        self.math.close()
        self.db.close()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def _set_status(self, key: str, state: str, **info: Any) -> None:
        self.status[key] = {"state": state, **info}
        bus.emit("status", status=self.status)

    async def _init_models(self) -> None:
        prof = self.settings.profile()
        self.answers.text_model = self.intel.model = prof["llm_model"]
        self.answers.vision_model = self.settings.get("vision_model")
        # the speech model loads on its own thread while the LLM warms up
        self.asr.beam = int(prof.get("asr_beam", 2))
        self._configure_asr()
        self.answers.num_ctx = self.intel.num_ctx = int(prof.get("num_ctx", 8192))
        threading.Thread(target=self._load_asr, args=(prof["asr_model"], prof.get("asr_compute", "float16"),
                                                      prof.get("asr_device", "auto"), int(prof.get("asr_threads", 0))),
                         daemon=True, name="asr-load").start()
        self._spawn(asyncio.to_thread(self.math.warm))
        await self.check_llm()

    def _load_asr(self, name: str, compute: str, device: str = "auto", threads: int = 0) -> None:
        try:
            self.asr.load(name, compute, device, threads)
            if not self.asr._thread or not self.asr._thread.is_alive():
                self.asr.start()
        except Exception as e:
            log.exception("ASR load failed")
            self._asr_status("error", {"message": str(e)})

    async def check_llm(self) -> None:
        ver = await self.llm.version()
        if not ver:
            self._set_status("llm", "unavailable", message="Ollama is not running. Install it from ollama.com or start it.")
            return
        model = self.answers.text_model
        if not await self.llm.has_model(model):
            self._set_status("llm", "missing", model=model, message=f"Model {model} is not downloaded yet")
        else:
            self._set_status("llm", "loading", model=model, ollama=ver)
            try:
                t = time.perf_counter()
                await self.llm.warm(model, self.answers.num_ctx)
                self._set_status("llm", "ready", model=model, ollama=ver, load_s=round(time.perf_counter() - t, 1))
            except Exception as e:
                self._set_status("llm", "error", model=model, message=str(e))
        rm = self.settings.get("reasoning_model")
        if rm:
            self._set_status("reasoning", "ready" if await self.llm.has_model(rm) else "missing", model=rm)
        vm = self.answers.vision_model
        if not self.settings.profile().get("vision_enabled", True):
            self._set_status("vision", "off", model=vm, message="Diagram reading is off on this computer (CPU only)")
        else:
            self._set_status("vision", "ready" if await self.llm.has_model(vm) else "missing", model=vm)

    async def pull_model(self, name: str) -> None:
        key = "vision" if name == self.answers.vision_model else \
            "reasoning" if name == self.settings.get("reasoning_model") and name != self.answers.text_model else "llm"

        def prog(j: dict) -> None:
            total, done = j.get("total") or 0, j.get("completed") or 0
            self._set_status(key, "downloading", model=name, status=j.get("status", ""),
                             progress=round(done / total, 3) if total else None)

        try:
            await self.llm.pull(name, prog)
            await self.check_llm()
        except Exception as e:
            self._set_status(key, "error", model=name, message=str(e))

    def _configure_asr(self) -> None:
        lang = self.settings.get("language") or "auto"
        if lang == "auto":
            self.asr.language = None if self.mem.subject_id == "arabic" else "en"
        else:
            self.asr.language = lang
        aliases = self.settings.get("name_aliases") or []
        terms = list(self.mem.key_terms)[-6:]
        self.asr.hotwords = " ".join(dict.fromkeys([self.settings.get("student_name"), *aliases[:2], *terms]))

    def _asr_status(self, state: str, info: dict) -> None:
        self.status["asr"] = {"state": state, **info}
        bus.emit("status", status=self.status)

    # ------------------------------------------------------------------ sessions
    async def start_session(self, subject_id: str | None, title: str = "", capture: bool = True) -> dict:
        if self.session:
            return self.session
        if not self.settings.get("consent_acknowledged"):
            raise PermissionError("Capture consent has not been confirmed in GLASS settings")
        self.mem.reset()
        self._clear_lesson_state()
        subj = self.settings.subject_by_id(subject_id) if subject_id else None
        self.mem.subject_id = subject_id
        self.mem.subject_locked = bool(subject_id)
        sid = self.db.create_session(subject_id, title or (subj["name"] if subj else "Lesson"))
        self.session = {"id": sid, "subject_id": subject_id, "started_at": time.time(), "title": title}
        self.answers.session_id = sid
        self.intel.begin(sid, subj["name"] if subj else "")
        self.intel.tasks_history = []
        self.names.configure(self.settings.get("student_name"), self.settings.get("name_aliases"))
        self._configure_asr()
        self.paused = False
        self._start_audio(capture)
        if capture and self.settings.get("screen_enabled"):
            self.screen.start()
        bus.emit("lesson.reset", session_id=sid)
        bus.emit("session.update", session=self.session_public())
        bus.emit("lesson.state", state=self.mem.state())
        self._set_mode("passive")
        return self.session

    async def stop_session(self) -> dict | None:
        if not self.session:
            return None
        sess = self.session
        self._stop_audio()
        await self.screen.stop()
        self.session = None
        # answers still being written belong to the lesson that just ended
        self.sched.cancel_where(lambda key, p: key.split(":", 1)[0] in (
            "answer", "verify", "snip", "snipv", "ask", "question", "explain", "catchup", "tutor", "state", "task"))
        # The final notes pass keeps the ended lesson's memory and notes to itself; the
        # engine gets fresh ones at once, so nothing from this lesson (topic, subject,
        # late background results) can leak into the next one.
        old_mem, old_intel = self.mem, self.intel
        self.mem = LessonMemory()
        self.answers.mem = self.mem
        self.intel = LessonIntel(self.llm, self.sched, self.mem, self.settings, self.db, bus.emit)
        self.intel.model, self.intel.num_ctx = old_intel.model, old_intel.num_ctx
        self.intel.after_job = self.answers.warm_prefix
        self._clear_lesson_state()
        self.answers.session_id = None
        self._set_mode("idle")
        bus.emit("lesson.reset", session_id=None, ended=sess["id"])
        bus.emit("session.update", session=None, ended=sess["id"])
        bus.emit("lesson.state", state=self.mem.state())
        # final notes pass in the background (the Notes view shows them updating)
        if old_mem.utterances:
            t = self._spawn(self._finalise(sess["id"], old_mem, old_intel))
            self._finalising.add(t)
            t.add_done_callback(self._finalising.discard)
        else:
            self.db.update_session(sess["id"], ended_at=time.time())
        return sess

    def _clear_lesson_state(self) -> None:
        """Everything the overlay shows about a lesson starts empty."""
        self._asked = set()
        self.answers.answers.clear()
        self.pending_q = None
        self.attention = None
        self._partial_alerted.clear()
        self._register_until = 0.0
        self._task_pending = False
        self._task_due = 0.0
        self._screen_seen = set()
        self._screen_offer = None
        self.screen.reset_state()

    async def _finalise(self, sid: str, mem: LessonMemory, intel: LessonIntel) -> None:
        try:
            # a regular notes pass may still be running; the final pass would skip if started now
            for _ in range(600):
                if not intel._notes_running:
                    break
                await asyncio.sleep(0.1)
            await intel.update_notes(final=True)
        finally:
            topic = mem.topic or (intel.notes or {}).get("topic", "")
            self.db.update_session(sid, ended_at=time.time(), topic=topic, summary=mem.lesson_summary[:600])
            bus.emit("session.finalised", session_id=sid)

    def session_public(self) -> dict | None:
        if not self.session:
            return None
        return {**self.session, "paused": self.paused, "subject_id": self.mem.subject_id, "topic": self.mem.topic}

    def set_paused(self, paused: bool) -> None:
        if not paused and self._thermal_paused:
            # the student resumes after a protective pause: trust them, but stay in reduced mode
            self._thermal_paused = False
            self.thermal.clear_fault()
            self.thermal.level = "hot"
            self._apply_thermal("hot", {**self.thermal.info, "level": "hot", "reason": "resumed after a protective pause"})
        self.paused = paused
        if self.screen.running:
            self.screen.pause(paused)
            if not paused and self.thermal.level in ("hot", "critical"):
                self.screen.pause(True)  # stays off until the laptop cools
                self._thermal_screen_paused = True
        if paused:
            for s in self.segmenters.values():
                s.reset()
        bus.emit("session.update", session=self.session_public())

    def set_subject(self, subject_id: str | None) -> None:
        self.mem.subject_id = subject_id
        self.mem.subject_locked = bool(subject_id)
        if self.session:
            self.session["subject_id"] = subject_id
            self.db.update_session(self.session["id"], subject_id=subject_id)
            subj = self.settings.subject_by_id(subject_id)
            if subj:
                self.intel.notes["subject"] = subj["name"]
        self._configure_asr()
        bus.emit("lesson.state", state=self.mem.state())

    def set_topic(self, topic: str | None, subject_id: str | None = None) -> None:
        """The student said what the lesson is about: it outranks automatic topic tracking."""
        if subject_id and self.settings.subject_by_id(subject_id):
            self.set_subject(subject_id)
        if topic:
            self.mem.topic = topic[:60]
            self.mem.topic_locked = True
            self.mem.subtopic = ""
            self.mem.key_terms.setdefault(topic[:40], "")  # helps speech recognition spell it
            if self.session:
                self.db.update_session(self.session["id"], topic=self.mem.topic)
            if self.intel.notes is not None and not self.intel.notes.get("topic"):
                self.intel.notes["topic"] = self.mem.topic
            self._configure_asr()
        bus.emit("lesson.state", state=self.mem.state())

    # ------------------------------------------------------------------ audio path
    def _start_audio(self, capture: bool = True) -> None:
        vad_class = SileroVAD()
        self.segmenters = {"class": Segmenter("class", vad_class, self.asr.submit, self._on_activity)}
        self.segmenters["class"].partial_every = float(self.settings.profile().get("partial_every", 0.7))
        if sys.platform == "darwin":
            from .macos import MacInputAudio, MacSystemAudio

            system_src, mic_src = (lambda *a: MacSystemAudio(*a)), (lambda *a: MacInputAudio(*a))
        else:
            system_src = lambda s, d, q, cb: AudioSource(s, "loopback", d, q, cb)  # noqa: E731
            mic_src = lambda s, d, q, cb: AudioSource(s, "mic", d, q, cb)  # noqa: E731
        self.sources = {"class": system_src("class", self.settings.get("audio_device"), self.audio_q,
                                            lambda st, info: self._audio_status("audio", st, info))}
        if self.settings.get("mic_enabled"):
            self.segmenters["me"] = Segmenter("me", SileroVAD(), self.asr.submit, self._on_activity)
            self.sources["me"] = mic_src("me", self.settings.get("mic_device"), self.audio_q,
                                         lambda st, info: self._audio_status("mic", st, info))
        self._dsp_stop.clear()
        self._dsp_thread = threading.Thread(target=self._dsp_loop, name="dsp", daemon=True)
        self._dsp_thread.start()
        if not capture:  # test harness feeds audio through inject_audio()
            self.sources = {}
            return
        for s in self.sources.values():
            s.start()

    def inject_audio(self, chunk) -> None:
        """Feed an AudioChunk as if it came from the capture device (tests, replay)."""
        self.audio_q.put(chunk)

    def _stop_audio(self) -> None:
        for s in self.sources.values():
            s.stop()
        self.sources = {}
        self._dsp_stop.set()
        if self._dsp_thread:
            self._dsp_thread.join(timeout=2)
        self._set_status("audio", "off")
        self._set_status("mic", "off")

    def _audio_status(self, key: str, state: str, info: dict) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(lambda: self._set_status(key, state, **info))

    def _dsp_loop(self) -> None:
        import numpy as np

        from .audio.capture import TARGET_SR, AudioChunk

        last: dict[str, float] = {}
        while not self._dsp_stop.is_set():
            try:
                chunk = self.audio_q.get(timeout=0.1)
            except queue.Empty:
                chunk = None
            now = time.time()
            if chunk is None:
                # WASAPI loopback sends nothing while no app is playing sound (e.g. the
                # teacher stops talking and Meet goes quiet). Feed silence for the gap so
                # the VAD can close the utterance instead of waiting for the next sound.
                for src, t_last in list(last.items()):
                    gap = now - t_last
                    if gap > 0.15 and not self.paused and src in self.segmenters:
                        n = int(min(gap, 1.0) * TARGET_SR)
                        try:
                            self.segmenters[src].feed(AudioChunk(src, np.zeros(n, np.float32), now))
                        except Exception:
                            log.exception("segmenter failed")
                        last[src] = now
                continue
            last[chunk.source] = max(now, chunk.t_end)
            if self.paused:
                continue
            seg = self.segmenters.get(chunk.source)
            if seg:
                try:
                    seg.feed(chunk)
                except Exception:
                    log.exception("segmenter failed")

    def _on_activity(self, source: str, speaking: bool, t: float) -> None:
        self.speaking[source] = speaking
        self.last_activity[source] = t
        if speaking:
            self.speech_start[source] = t
        bus.emit("speech.activity", source=source, speaking=speaking)

    def _on_segment_thread(self, seg: TranscriptSegment) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(lambda: self._spawn(self._on_segment(seg)))

    # ------------------------------------------------------------------ transcripts
    async def _on_segment(self, seg: TranscriptSegment) -> None:
        if not self.session or self.paused:
            return
        if not seg.final:
            bus.emit("transcript.partial", id=seg.id, source=seg.source, text=seg.text, t0=seg.t0)
            # early warning: the name in a partial transcript, before the sentence ends
            if seg.source == "class" and seg.id not in self._partial_alerted:
                hit = self.names.scan(seg.text)
                if hit and hit.confidence >= 0.6:
                    self._partial_alerted.add(seg.id)
                    self.attention = {"t": time.time(), "confidence": hit.confidence, "level": "possible", "text": seg.text}
                    bus.emit("attention", **self.attention)
            return
        bus.emit("transcript.final", id=seg.id, source=seg.source, text=seg.text, t0=seg.t0, t1=seg.t1,
                 latency_ms=round(seg.latency_ms),
                 noise=not compact_for_context(seg.text) or (time.time() < self._register_until and len(seg.text.split()) <= 8))
        # compaction: register / tech chatter / small talk never reaches the AI's context
        in_register = time.time() < self._register_until or is_attendance(seg.text)
        ctx = "" if in_register and len(seg.text.split()) <= 8 else compact_for_context(seg.text)
        u = Utterance(seg.id, seg.source, seg.text, seg.t0, seg.t1, ctx)
        self.mem.chars_since_state -= len(seg.text) - len(ctx)
        self.mem.add_utterance(u)
        if self.settings.get("store_transcript", True) is not False:
            self.db.add_segment(self.session["id"], seg.id, seg.source, seg.text, seg.t0, seg.t1)

        if seg.source == "me":
            if self.pending_q:  # the student answered out loud
                self._resolve_pending("student", seg.text)
            return

        # teacher continued straight after a question: rhetorical / self-answered
        if self.pending_q and seg.t0 - self.pending_q["t_end"] < 2.5 and RHETORICAL_FOLLOW.match(seg.text.strip()):
            self._resolve_pending("teacher", seg.text)

        word_probs = [(w.text, w.prob) for w in seg.words]
        hit = self.names.scan(seg.text, word_probs)
        sig = classify_question(seg.text)
        if hit:
            self.attention = {"t": time.time(), "confidence": hit.confidence, "level": hit.level, "text": seg.text}
            bus.emit("attention", **self.attention)

        directed = bool(hit and hit.confidence >= 0.45)
        recent_q = self.mem.active_question and time.time() - self.mem.active_question["t"] < 120
        if sig.is_question and not sig.check and self._ignored(sig.text) and not directed:
            log.info("ignored (student marked similar as not a question): %r", sig.text)
        elif is_attendance(seg.text):
            in_register = time.time() < self._register_until
            self._register_until = time.time() + 90
            if directed and (in_register or not recent_q):
                # "Sanad?" during the register: answer "here", not a question
                bus.emit("register", text=seg.text, t=time.time())
            elif directed:
                self._spawn(self._escalate_recent(hit, seg))
        elif sig.is_question and not sig.check and not sig.rhetorical and sig.generic:
            # "Anyone?" / "What's the answer?" / "Where is it?": no lesson content of its own.
            # It can only be about the last real question — never start a new answer for it.
            if recent_q or directed:
                self._spawn(self._escalate_recent(hit, seg) if directed else self._mark_waiting(seg))
            else:
                log.info("ignored content-free question: %r", sig.text)
        elif sig.is_question and not sig.check and not sig.rhetorical:
            self._spawn(self._answer_question(sig, hit, seg))
        elif hit and hit.confidence >= 0.45 and not sig.is_question:
            # "Sanad?" / "Sanad, go on" -> the most recent meaningful question is now his
            self._spawn(self._escalate_recent(hit, seg))

        if detect_task_cue(seg.text) or (self._task_due and time.time() - self._task_due < 8):
            # debounce: instructions often continue over several sentences
            # ("...questions 1 to 6." / "You have fifteen minutes."), so extract
            # once the teacher pauses instead of on the first fragment
            self._task_due = self._task_due or time.time()
            if not self._task_pending:
                self._task_pending = True
                self._spawn(self._task(seg.text))
        if self.mem.chars_since_state >= int(self.settings.profile().get("state_chars", 350)) \
                or not self.mem.topic and len(self.mem.utterances) >= 2:
            self._spawn(self.intel.update_state())
        self._maybe_ask_subject()
        if self.intel.notes_due():
            self._spawn(self.intel.update_notes())

    # ------------------------------------------------------------------ learning from the student
    def _ignored(self, text: str) -> bool:
        """The student marked a similar question as "not a real question"."""
        words = set(content_words(text, {self.settings.get("student_name", "").lower()}))
        for entry in self.settings.get("ignore_questions") or []:
            e = set(entry.split())
            if e and words and len(e & words) / len(e | words) >= 0.75:
                return True
        return False

    async def feedback(self, answer_id: str, rating: str, reason: str = "") -> dict | None:
        ans = self.answers.answers.get(answer_id)
        if not ans:
            return None
        self.db.add_feedback(answer_id, rating, reason, ans.get("resolved") or ans.get("question", ""),
                             ans.get("say") or ans.get("body", ""))
        ans["feedback"] = rating
        self.answers._push(ans, force=True)
        prefs = list(self.settings.get("answer_prefs") or [])

        def add_pref(p: str) -> None:
            if p not in prefs:
                prefs.append(p)
                self.settings.update({"answer_prefs": prefs[-6:]})

        if reason == "not_question":
            words = " ".join(sorted(set(content_words(ans.get("question", "")))))
            if words:
                ign = list(self.settings.get("ignore_questions") or [])
                if words not in ign:
                    self.settings.update({"ignore_questions": (ign + [words])[-200:]})
            ans["rhetorical"] = True
            self.answers._push(ans, force=True)
            return {"hidden": True}
        if reason == "too_long":
            add_pref("keep the spoken answer under 15 words and the explanation to one or two sentences")
            return {"id": (await self.answers._redo(ans, ["shorter"], ""))["id"]}
        if reason == "too_simple":
            add_pref("use precise exam-level terminology and a bit more depth")
            return {"id": (await self.answers._redo(ans, ["detailed"], ""))["id"]}
        if reason == "not_answer":
            add_pref("always state the actual answer first; never just describe or restate the question")
            return {"id": (await self.answers._redo(ans, ["answer"], ""))["id"]}
        if reason == "wrong":
            return {"id": (await self.answers._redo(ans, ["wrong"], ""))["id"]}
        return {"ok": True}

    def glass_ask(self, key: str, question: str, options: list[dict]) -> None:
        """GLASS asks the student something (once per lesson per key)."""
        if key in self._asked:
            return
        self._asked.add(key)
        bus.emit("glass.ask", id=key, question=question, options=options, t=time.time())

    def _maybe_ask_subject(self) -> None:
        if self.session and not self.mem.subject_id and not self.mem.subject_locked and \
                time.time() - self.session["started_at"] > 150 and len(self.mem.utterances) >= 6:
            stats = sorted(self.db.subject_stats(), key=lambda r: -(r["lessons"] or 0))
            ids = [r["subject_id"] for r in stats if r["subject_id"]][:3]
            ids += [s["id"] for s in self.settings.get("subjects") if s["id"] not in ids][: 4 - len(ids)]
            opts = [{"label": (self.settings.subject_by_id(i) or {}).get("name", i), "action": "subject", "value": i} for i in ids]
            self.glass_ask("subject", "Which lesson is this? It helps me answer the right way.", opts)

    def maybe_ask_for_book(self, ans: dict) -> None:
        subj = self.mem.subject_id
        if subj in ("english_lit", "english_lang") or QUOTE_REQUEST.search(ans.get("question", "")):
            if not self.db.texts(subj) if subj else not self.db.texts():
                name = (self.settings.subject_by_id(subj) or {}).get("name", "this subject")
                self.glass_ask(f"book:{subj}", f"Add your copy of the book (PDF, EPUB, Word or text) so I can give real, "
                                               f"checked quotes for {name}?",
                               [{"label": "Add book", "action": "add_text", "value": subj or ""}])

    async def add_text(self, path: str, subject_id: str | None, title: str = "") -> dict:
        from pathlib import Path

        from .texts import chunk_text, extract_text

        p = Path(path)
        raw = await asyncio.to_thread(extract_text, p)
        chunks = await asyncio.to_thread(chunk_text, raw)
        if len(chunks) < 2:
            raise ValueError("Couldn't find readable text in that file.")
        info = self.db.add_text(subject_id or self.mem.subject_id, title or p.stem.replace("_", " "), p.name, chunks)
        self.answers.invalidate_texts()
        bus.emit("texts.update", texts=self.db.texts())
        return info

    async def _answer_question(self, sig, hit, seg) -> None:
        # register the pending question first so silence detection starts immediately
        self.pending_q = {"answer_id": None, "t_end": seg.t1, "waiting": False, "utt_id": seg.id, "text": sig.text}
        # directed questions elevate immediately; class questions only once the
        # teacher is actually waiting (so rhetorical questions never raise the panel)
        if hit and hit.confidence >= 0.45:
            self._set_mode("direct")
        task = asyncio.create_task(self.answers.on_question(sig, hit, seg.text))
        await asyncio.sleep(0)
        if self.mem.active_question:
            self.pending_q["answer_id"] = self.mem.active_question.get("answer_id")
        await task

    async def _mark_waiting(self, seg) -> None:
        """The teacher nudges ("Anyone?") about the question already on the overlay."""
        aq = self.mem.active_question
        ans = self.answers.answers.get((aq or {}).get("answer_id", ""))
        if not ans:
            return
        ans["waiting"] = True
        self.answers._push(ans, force=True)
        self.pending_q = {"answer_id": ans["id"], "t_end": seg.t1, "waiting": True, "utt_id": seg.id, "text": aq["text"]}
        bus.emit("question.waiting", answer_id=ans["id"], text=aq["text"])
        if self.mode != "direct":
            self._set_mode("question")

    async def _escalate_recent(self, hit, seg) -> None:
        aq = self.mem.active_question
        if aq and time.time() - aq["t"] < 150:
            ans = self.answers.answers.get(aq.get("answer_id", ""))
            if ans:
                ans["directed"] = hit.to_dict()
                ans["waiting"] = True
                self.answers._push(ans, force=True)
                self.pending_q = {"answer_id": ans["id"], "t_end": seg.t1, "waiting": True, "utt_id": seg.id, "text": aq["text"]}
                self._set_mode("direct")
                return
        # no question yet: look back in the transcript for the last question-like utterance
        for u in reversed(self.mem.window(120)[:-1]):
            if u.source != "class":
                continue
            sig = classify_question(u.text)
            if sig.is_question and not sig.check and not sig.generic:
                await self._answer_question(sig, hit, seg)
                return
        self._set_mode("direct")  # addressed, but no question yet; the overlay says so

    def _resolve_pending(self, by: str, text: str) -> None:
        pq = self.pending_q
        self.pending_q = None
        if not pq:
            return
        ans = self.answers.answers.get(pq.get("answer_id") or "")
        if ans:
            ans["answered_by"] = by
            ans["waiting"] = False
            if by == "teacher":
                ans["teacher_answer"] = text
                ans["rhetorical"] = True  # the teacher answered it themselves
                if ans.get("qid") and self.session:
                    self.db.set_teacher_answer(ans["qid"], text)
            self.answers._push(ans, force=True)

    async def _task(self, text: str) -> None:
        lesson_intel = self.intel
        # wait for the teacher to finish the instruction: quiet for 1.2 s and no
        # speech still being transcribed (bounded, so a monologue can't stall it)
        t0 = time.time()
        while time.time() - t0 < 12:
            quiet = not self.speaking.get("class") and time.time() - self.last_activity.get("class", 0) > 1.2
            if quiet and self.asr._q.empty():
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.15)  # let the last final transcript land in memory
        if lesson_intel is not self.intel or not self.session:
            return  # the lesson ended meanwhile
        self._task_pending = False
        self._task_due = 0.0
        t = await lesson_intel.extract_task(text)
        if t:
            self._last_task_t = time.time()
            self._set_mode("task")

    # ------------------------------------------------------------------ screen
    def _screen_status(self, state: str, info: dict) -> None:
        self._set_status("screen", state, **info)

    def _on_screen_obs(self, obs: ScreenObs) -> None:
        self.mem.add_screen(obs)
        bus.emit("screen.observation", text=obs.text[:3000], items={str(k): v for k, v in obs.items.items()},
                 equations=obs.equations, title=obs.title, t=obs.t, ocr_ms=round(self.screen.ocr.last_ms))
        self._offer_screen_questions(obs)

    def _offer_screen_questions(self, obs: ScreenObs) -> None:
        """New questions appeared on the shared screen (a worksheet, an exam question, an
        equation on a slide): offer to solve them, or solve them straight away if the
        student turned that on. Deterministic and cheap — no model runs until asked."""
        if not self.session or self.paused:
            return
        if obs.items:
            qs = [q for q in split_questions(obs.text) if q["label"][:1].isdigit()]
        elif obs.equations:
            lines = obs.text.splitlines()
            qs = [{"label": str(i + 1), "text": next((ln.strip() for ln in lines if eq.replace(" ", "") in ln.replace(" ", "")), eq)}
                  for i, eq in enumerate(obs.equations[:6])]
        else:
            return
        keys = {q["label"]: label_key(q["label"]) + "|" + re.sub(r"\W", "", q["text"].lower())[:80] for q in qs}
        new = [q for q in qs if keys[q["label"]] not in self._screen_seen]
        if not new:
            return  # same worksheet as before (scrolling, re-read): don't nag
        self._screen_seen |= set(keys.values())
        oid = f"scr_{int(time.time() * 1000)}"
        self._screen_offer = {"id": oid, "text": obs.text}
        bus.emit("screen.questions", id=oid, t=time.time(),
                 questions=[{"label": q["label"], "text": q["text"][:300]} for q in qs[:12]],
                 new=[q["label"] for q in new])
        if self.settings.get("screen_autosolve") and self.thermal.level not in ("hot", "critical"):
            self._spawn(self.solve_screen(oid, [q["label"] for q in new]))

    async def solve_screen(self, offer_id: str | None, labels: list[str] | None) -> dict:
        offer = self._screen_offer
        text = offer["text"] if offer and (not offer_id or offer["id"] == offer_id) else \
            (self.mem.latest_screen().text if self.mem.latest_screen() else "")
        if not text:
            raise ValueError("Nothing has been read from the screen yet.")
        self._last_help_t = time.time()
        self._set_mode("confusion")
        return await self.answers.solve_screen(text, labels)

    def _on_vision_request(self, obs: ScreenObs, png: bytes) -> None:
        if self.answers.vision_model != self.answers.text_model:
            return  # automatic diagram reading only with a unified model (no VRAM swapping mid-lesson)
        h = ScreenService.image_hash(png)
        if h in self._vision_cache:
            obs.vision = self._vision_cache[h]
            return
        self._spawn(self._describe(obs, png, h))

    async def _describe(self, obs: ScreenObs, png: bytes, h: str) -> None:
        msgs = [{"role": "user", "content": "This is a frame from an online lesson. In 2-3 sentences describe the educational "
                                            "content (diagram, graph, equation, labels, values). Ignore video-call UI."}]

        async def job():
            return await self.llm.chat(self.answers.vision_model, msgs, think=False, num_ctx=self.answers.num_ctx,
                                       num_predict=160, temperature=0.1, images=[png])

        try:
            desc = await self.sched.submit(VISION, job, key="vision", max_age=10)
            self._vision_cache[h] = desc
            obs.vision = desc
            bus.emit("screen.vision", text=desc)
        except asyncio.CancelledError:
            from .llm import reraise_if_cancelled
            reraise_if_cancelled()
        except Exception:
            pass

    async def snip(self, x: int, y: int, w: int, h: int, use_vision: bool = False) -> dict:
        png, text, blank = await self.screen.snip(x, y, w, h)
        if blank:
            # never let a model "read" an empty capture: it would invent a question
            ans = self.answers._new("snip", question="Selected region", status="error",
                                    error="The selected area is blank — the screen may be locked, or the window is "
                                          "minimised or protected from capture.")
            self.answers._push(ans, force=True)
            return ans
        self._last_help_t = time.time()
        self._set_mode("confusion")
        powers = dict(self.screen.last_snip_powers)
        return await self.answers.solve_snip(png, text, use_vision, powers=powers)

    # ------------------------------------------------------------------ thermal protection
    def _on_thermal_thread(self, level: str, info: dict) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self._apply_thermal, level, dict(info))

    def _apply_thermal(self, level: str, info: dict) -> None:
        """Back off as the laptop heats up (see thermal.py); restore when it cools."""
        self.settings.runtime = dict(THERMAL_RUNTIME.get(level, {}))
        prof = self.settings.profile()
        for seg in self.segmenters.values():
            seg.partial_every = float(prof.get("partial_every", 0.7))
        self.asr.beam = int(prof.get("asr_beam", 1))
        if level in ("hot", "critical"):
            if self.screen.running and not self.screen._paused:
                self.screen.pause(True)
                self._thermal_screen_paused = True
        elif self._thermal_screen_paused:
            self._thermal_screen_paused = False
            if self.screen.running and not self.paused:
                self.screen.pause(False)
        if level == "critical" and self.session and not self.paused:
            self._thermal_paused = True
            self.set_paused_raw(True)
        msg = {
            "ok": "",
            "warm": "Your laptop is warm, so GLASS is doing less in the background.",
            "hot": "Your laptop is hot. GLASS paused screen reading and notes; answers still work.",
            "critical": "GLASS paused listening to protect your laptop. Let it cool down, then press Resume.",
        }[level]
        self.status["thermal"] = {**info, "level": level, "message": msg}
        bus.emit("status", status=self.status)

    def set_paused_raw(self, paused: bool) -> None:
        self.paused = paused
        if self.screen.running:
            self.screen.pause(paused)
        if paused:
            for s in self.segmenters.values():
                s.reset()
        bus.emit("session.update", session=self.session_public())

    # ------------------------------------------------------------------ modes
    def _set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self._mode_since = time.time()
            bus.emit("mode", mode=mode)

    def note_help_request(self) -> None:
        self._last_help_t = time.time()
        self._set_mode("confusion")

    async def _ticker(self) -> None:
        """Teacher-waiting detection + display mode upkeep (every 200 ms)."""
        while True:
            await asyncio.sleep(0.2)
            now = time.time()
            if not self.session:
                continue
            pq = self.pending_q
            # waiting = nobody has started speaking since the question ended
            if pq and not pq["waiting"] and not self.speaking.get("class") and not self.speaking.get("me") and \
                    now - pq["t_end"] >= WAIT_AFTER_QUESTION and \
                    self.speech_start.get("class", 0) < pq["t_end"] and self.speech_start.get("me", 0) < pq["t_end"]:
                pq["waiting"] = True
                ans = self.answers.answers.get(pq.get("answer_id") or "")
                if ans:
                    ans["waiting"] = True
                    self.answers._push(ans, force=True)
                bus.emit("question.waiting", answer_id=pq.get("answer_id"), text=pq["text"])
                if self.mode not in ("direct",):
                    self._set_mode("question")
            if pq and now - pq["t_end"] > 90:
                self.pending_q = None
            if now - self.answers.last_llm_use > 900:  # idle 15 min mid-lesson: refresh keep_alive
                self.answers.last_llm_use = now
                self._spawn(self.llm.warm(self.answers.text_model, self.answers.num_ctx))
            # mode decay
            since = now - self._mode_since
            last_speech = max(self.last_activity.values())
            if self.mode == "direct" and since > 120:
                self._set_mode("passive")
            elif self.mode == "question" and since > 60 and not (pq and pq["waiting"]):
                self._set_mode("passive")
            elif self.mode == "task" and since > 45:
                self._set_mode("passive")
            elif self.mode == "confusion" and now - self._last_help_t > 180:
                self._set_mode("passive")
            elif self.mode == "passive" and now - last_speech > 90 and since > 30:
                self._set_mode("idle")
            elif self.mode == "idle" and self.speaking.get("class"):
                self._set_mode("passive")

    async def _metrics_loop(self) -> None:
        proc = psutil.Process()
        psutil.cpu_percent(None)
        nv = None
        try:
            import pynvml

            pynvml.nvmlInit()
            nv = pynvml.nvmlDeviceGetHandleByIndex(0)
            gpu_name = pynvml.nvmlDeviceGetName(nv)
        except Exception:
            gpu_name = None
        ollama_procs: list[psutil.Process] = []
        while True:
            await asyncio.sleep(2)
            try:
                cpu_now = psutil.cpu_percent(None)
                th = await asyncio.to_thread(self.thermal.sample, cpu_now)
                m: dict[str, Any] = {
                    "cpu": cpu_now,
                    "gpu_temp": th.get("gpu_temp"),
                    "cpu_perf": round(th["cpu_perf"]) if th.get("cpu_perf") is not None else None,
                    "thermal": th.get("level"),
                    "ram_used_gb": round(psutil.virtual_memory().used / 2**30, 1),
                    "ram_total_gb": round(psutil.virtual_memory().total / 2**30, 1),
                    "engine_rss_mb": round(proc.memory_info().rss / 2**20),
                    "engine_cpu": proc.cpu_percent(None),
                }
                if not ollama_procs or not all(p.is_running() for p in ollama_procs):
                    ollama_procs = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().startswith("ollama")]
                m["ollama_rss_mb"] = round(sum(p.memory_info().rss for p in ollama_procs if p.is_running()) / 2**20)
                if nv is not None:
                    import pynvml

                    util = pynvml.nvmlDeviceGetUtilizationRates(nv)
                    mem = pynvml.nvmlDeviceGetMemoryInfo(nv)
                    m.update({"gpu": gpu_name, "gpu_util": util.gpu, "vram_used_mb": mem.used // 2**20,
                              "vram_total_mb": mem.total // 2**20})
                m.update({
                    "asr_rtf": round(self.asr.rtf, 3), "asr_latency_ms": round(self.asr.last_latency_ms),
                    "vad_ms": round(next(iter(self.segmenters.values())).vad_ms, 3) if self.segmenters else None,
                    "llm_first_token_ms": round(self.llm.last_first_token_ms), "llm_tok_s": round(self.llm.last_tok_s, 1),
                    "ocr_ms": round(self.screen.ocr.last_ms), "screen": self.screen.stats,
                    "scheduler": self.sched.snapshot(), "audio_queue": self.audio_q.qsize(),
                    "audio_level": {k: round(s.level, 4) for k, s in self.sources.items()},
                    "audio_dropped": sum(s.dropped for s in self.sources.values()),
                    "asr_device": self.asr.device, "asr_model": self.asr.model_name,
                })
                last = [a for a in self.answers.answers.values() if a["mode"] == "question" and a["status"] == "done"][-10:]
                if last:
                    says = [a["latency"].get("say_ms") for a in last if a["latency"].get("say_ms")]
                    m["answer_say_ms_avg"] = round(sum(says) / len(says)) if says else None
                self.metrics = m
                bus.emit("metrics", metrics=m)
            except Exception:
                log.debug("metrics failed", exc_info=True)

    # ------------------------------------------------------------------ queries
    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "session": self.session_public(),
            "mode": self.mode,
            "lesson": self.mem.state(),
            "task": self.mem.current_task,
            "task_history": self.intel.tasks_history[-10:],
            "answers": [self.answers._public(a) for a in list(self.answers.answers.values())[-12:]],
            "transcript": [{"id": u.id, "source": u.source, "text": u.text, "t0": u.t0, "t1": u.t1}
                           for u in self.mem.utterances[-80:]],
            "notes": self.intel.notes if self.session or self.intel.notes else None,
            "notes_md": notes_markdown(self.intel.notes) if self.intel.notes else "",
            "formulas": self.db.formulas(sid=self.session["id"]) if self.session else [],
            "settings": self.settings.all(),
            "metrics": self.metrics,
        }

    def devices(self) -> dict:
        try:
            if sys.platform == "darwin":
                from .macos import list_audio_devices

                return list_audio_devices()
            return list_devices()
        except Exception as e:
            return {"loopback": [], "input": [], "error": str(e)}

    def windows(self) -> dict:
        return {"windows": list_windows(), "monitors": list_monitors()}
