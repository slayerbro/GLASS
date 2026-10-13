"""Streaming speech recognition: VAD segmentation + faster-whisper worker.

Pipeline (per audio source):
  AudioChunk -> Segmenter (Silero VAD, 32 ms frames) -> utterance buffers
  Segmenter emits partial jobs every ~0.7 s while someone is talking and a
  final job when the utterance ends. One GPU worker thread serves all jobs,
  finals before partials, and partials coalesce so the queue can never grow
  without bound.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .capture import TARGET_SR, AudioChunk
from .vad import SileroVAD

log = logging.getLogger("glass.asr")

FRAME = 512  # 32 ms at 16 kHz


def _add_cuda_dll_dirs() -> None:
    """ctranslate2 needs cuBLAS + cuDNN 9; the pip nvidia-* wheels ship them."""
    roots = [Path(p) for p in sys.path if p.endswith("site-packages")]
    extra = []
    for root in roots:
        for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_runtime/bin", "nvidia/cuda_nvrtc/bin"):
            d = root / sub
            if d.is_dir():
                extra.append(str(d))
    cuda = os.environ.get("CUDA_PATH")
    if cuda and Path(cuda, "bin").is_dir():
        extra.append(str(Path(cuda, "bin")))
    for d in extra:
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
    if extra:
        os.environ["PATH"] = os.pathsep.join(extra) + os.pathsep + os.environ.get("PATH", "")


_add_cuda_dll_dirs()

# Phrases Whisper is known to hallucinate on silence, music or noise.
HALLUCINATIONS = {
    "thank you", "thank you.", "thanks for watching", "thanks for watching!", "thank you for watching",
    "please subscribe", "subscribe", "you", "bye", "bye.", "so", "okay", ".", "...", "the end",
    "subtitles by the amara.org community", "ترجمة نانسي قنقر", "اشتركوا في القناة",
}


@dataclass
class Word:
    text: str
    prob: float
    start: float
    end: float


@dataclass
class TranscriptSegment:
    id: str
    source: str
    text: str
    t0: float
    t1: float
    final: bool
    words: list[Word] = field(default_factory=list)
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    latency_ms: float = 0.0


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    source: str = field(compare=False)
    utt_id: str = field(compare=False)
    audio: np.ndarray = field(compare=False)
    t0: float = field(compare=False)
    t1: float = field(compare=False)
    final: bool = field(compare=False)
    enqueued: float = field(compare=False, default=0.0)


class Segmenter:
    """Turns a continuous chunk stream into utterances using Silero VAD."""

    def __init__(self, source: str, vad: SileroVAD, submit: Callable[[_Job], None], on_activity: Callable[[str, bool, float], None]):
        self.source = source
        self.vad = vad
        self.submit = submit
        self.on_activity = on_activity
        self.start_th = 0.5
        self.end_th = 0.35
        self.end_silence = 0.55  # seconds of silence that closes an utterance
        self.max_utt = 18.0
        self.partial_every = 0.7
        self._pending = np.zeros(0, np.float32)
        self._preroll: deque[np.ndarray] = deque(maxlen=10)  # 320 ms
        self._utt: list[np.ndarray] = []
        self._speaking = False
        self._silence = 0.0
        self._utt_len = 0.0
        self._since_partial = 0.0
        self._utt_t0 = 0.0
        self._seq = 0
        self._utt_id = ""
        self._last_low_idx = 0  # frame index of the last low-probability frame (for forced cuts)
        self.vad_ms = 0.0

    def reset(self) -> None:
        self._pending = np.zeros(0, np.float32)
        self._utt = []
        self._speaking = False
        self.vad.reset()

    def feed(self, chunk: AudioChunk) -> None:
        x = np.concatenate([self._pending, chunk.samples]) if len(self._pending) else chunk.samples
        n = len(x) // FRAME
        self._pending = x[n * FRAME:]
        t_frame_end = chunk.t_end - len(self._pending) / TARGET_SR - n * FRAME / TARGET_SR
        for i in range(n):
            frame = x[i * FRAME:(i + 1) * FRAME]
            t_frame_end += FRAME / TARGET_SR
            t = time.perf_counter()
            p = self.vad(frame)
            self.vad_ms = 0.9 * self.vad_ms + 0.1 * (time.perf_counter() - t) * 1000
            self._step(frame, p, t_frame_end)

    def _step(self, frame: np.ndarray, p: float, t_end: float) -> None:
        dt = FRAME / TARGET_SR
        if not self._speaking:
            self._preroll.append(frame)
            if p >= self.start_th:
                self._speaking = True
                self._seq += 1
                self._utt_id = f"{self.source}-{int(t_end * 1000)}-{self._seq}"
                self._utt = list(self._preroll)
                self._preroll.clear()
                self._utt_len = len(self._utt) * dt
                self._utt_t0 = t_end - self._utt_len
                self._silence = 0.0
                self._since_partial = 0.0
                self._last_low_idx = 0
                self.on_activity(self.source, True, t_end)
            return
        self._utt.append(frame)
        self._utt_len += dt
        self._since_partial += dt
        if p < self.end_th:
            self._silence += dt
            self._last_low_idx = len(self._utt)
        else:
            self._silence = 0.0
        if self._silence >= self.end_silence:
            keep = len(self._utt) - int((self._silence - 0.2) / dt)
            self._finish(t_end, keep)
            self.on_activity(self.source, False, t_end)
        elif self._utt_len >= self.max_utt:
            cut = self._last_low_idx if self._last_low_idx > len(self._utt) // 2 else len(self._utt)
            rest = self._utt[cut:]
            self._finish(t_end, cut)
            # the speaker is still talking: continue a fresh utterance with the remainder
            self._speaking = True
            self._seq += 1
            self._utt_id = f"{self.source}-{int(t_end * 1000)}-{self._seq}"
            self._utt = rest
            self._utt_len = len(rest) * dt
            self._utt_t0 = t_end - self._utt_len
            self._since_partial = 0.0
        elif self.partial_every > 0 and self._since_partial >= self.partial_every and self._utt_len > 0.6:
            self._since_partial = 0.0
            # only the last 8 s: enough for live captions and the early name alert, and it
            # stops partials getting more expensive the longer the teacher talks
            tail = self._utt[-int(8.0 / (FRAME / TARGET_SR)):]
            audio = np.concatenate(tail)
            self.submit(_Job(1, 0, self.source, self._utt_id, audio, t_end - len(audio) / TARGET_SR, t_end, False))

    def _finish(self, t_end: float, keep: int) -> None:
        frames = self._utt[: max(keep, 1)]
        self._speaking = False
        self._utt = []
        audio = np.concatenate(frames) if frames else np.zeros(0, np.float32)
        if len(audio) < int(0.35 * TARGET_SR):
            return  # clicks / coughs
        self.submit(_Job(0, 0, self.source, self._utt_id, audio, self._utt_t0, self._utt_t0 + len(audio) / TARGET_SR, True))


class ASRWorker:
    """Owns the Whisper model and serves transcription jobs on one thread."""

    def __init__(self, on_segment: Callable[[TranscriptSegment], None], on_status: Callable[[str, dict], None]):
        self.on_segment = on_segment
        self.on_status = on_status
        self.model = None
        self.model_name = ""
        self.device = ""
        self.compute = ""
        self.beam = 2
        self.language: str | None = "en"
        self.hotwords: str = ""
        self._q: "queue.PriorityQueue[_Job]" = queue.PriorityQueue()
        self._latest_partial: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._seq = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rtf = 0.0
        self.last_latency_ms = 0.0
        self._recent_finals: deque[tuple[float, str, str]] = deque(maxlen=12)

    # -- model ---------------------------------------------------------------
    def load(self, model_name: str, compute: str = "float16") -> None:
        from faster_whisper import WhisperModel
        from ..config import MODELS_DIR

        self.on_status("loading", {"model": model_name})
        t = time.perf_counter()
        try:
            try:  # offline first: no network request when the model is already cached
                model = WhisperModel(model_name, device="cuda", compute_type=compute,
                                     download_root=str(MODELS_DIR / "whisper"), local_files_only=True)
            except Exception as e:
                if "cuda" in str(e).lower() or "cudnn" in str(e).lower() or "cublas" in str(e).lower():
                    raise
                self.on_status("loading", {"model": model_name, "note": "downloading speech model (first run)"})
                model = WhisperModel(model_name, device="cuda", compute_type=compute, download_root=str(MODELS_DIR / "whisper"))
            # warm-up so the first real utterance is not slow
            list(model.transcribe(np.zeros(TARGET_SR, np.float32), beam_size=1, language="en")[0])
            self.device, self.compute = "cuda", compute
        except Exception as e:
            log.warning("CUDA whisper failed (%s); falling back to CPU int8", e)
            self.on_status("loading", {"model": model_name, "note": "GPU unavailable, using CPU"})
            model = WhisperModel(model_name if model_name != "large-v3-turbo" else "small", device="cpu", compute_type="int8",
                                 download_root=str(MODELS_DIR / "whisper"))
            self.device, self.compute = "cpu", "int8"
        self.model = model
        self.model_name = model_name
        self.on_status("ready", {"model": model_name, "device": self.device, "compute": self.compute,
                                 "load_s": round(time.perf_counter() - t, 1)})

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="asr", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put(_Job(-1, 0, "", "", np.zeros(0, np.float32), 0, 0, True))
        if self._thread:
            self._thread.join(timeout=5)

    def unload(self) -> None:
        self.model = None

    def submit(self, job: _Job) -> None:
        with self._lock:
            self._seq += 1
            job.seq = self._seq
            job.enqueued = time.perf_counter()
            if not job.final:
                # coalesce: only the newest partial per source is worth computing
                self._latest_partial[job.source] = job
            else:
                self._latest_partial.pop(job.source, None)
        self._q.put(job)

    def transcribe_array(self, audio: np.ndarray, final: bool = True) -> tuple[str, list[Word], float, float]:
        """Synchronous helper (also used by tests)."""
        assert self.model is not None
        segments, _info = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam if final else 1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            without_timestamps=not final,
            word_timestamps=final,
            hotwords=self.hotwords or None,
            vad_filter=False,
            no_speech_threshold=0.6,
        )
        texts, words = [], []
        lp, nsp, n = 0.0, 0.0, 0
        for s in segments:
            if s.no_speech_prob > 0.75 and s.avg_logprob < -0.8:
                continue
            if s.compression_ratio > 2.6:
                continue  # repetition loop
            texts.append(s.text.strip())
            lp += s.avg_logprob
            nsp += s.no_speech_prob
            n += 1
            for w in s.words or []:
                words.append(Word(w.word.strip(), float(w.probability), float(w.start), float(w.end)))
        text = " ".join(t for t in texts if t).strip()
        return text, words, (lp / n if n else -2.0), (nsp / n if n else 1.0)

    # -- worker loop --------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            job = self._q.get()
            if job.priority < 0 or self._stop.is_set():
                break
            if not job.final:
                with self._lock:
                    if self._latest_partial.get(job.source) is not job:
                        continue  # superseded
                # a pending final makes this partial pointless
                if any(j.final and j.source == job.source for j in list(self._q.queue)):
                    continue
            if self.model is None or len(job.audio) == 0:
                continue
            t = time.perf_counter()
            try:
                text, words, lp, nsp = self.transcribe_array(job.audio, job.final)
            except Exception as e:
                log.exception("transcription failed")
                self.on_status("error", {"message": str(e)})
                continue
            dt = time.perf_counter() - t
            dur = len(job.audio) / TARGET_SR
            self.rtf = 0.8 * self.rtf + 0.2 * (dt / max(dur, 0.1))
            self.last_latency_ms = (time.perf_counter() - job.enqueued) * 1000
            text = self._clean(text)
            if not text:
                continue
            if job.final and self._is_duplicate(job, text):
                continue
            seg = TranscriptSegment(job.utt_id, job.source, text, job.t0, job.t1, job.final, words, lp, nsp,
                                    self.last_latency_ms)
            self.on_segment(seg)

    @staticmethod
    def _clean(text: str) -> str:
        t = re.sub(r"\s+", " ", text).strip()
        if t.lower().strip(" .!?") in {h.strip(" .!?") for h in HALLUCINATIONS}:
            return ""
        if re.fullmatch(r"[\W_]*", t):
            return ""
        return t

    def _is_duplicate(self, job: _Job, text: str) -> bool:
        """Drop echoes: mic picking up the speakers, or overlapping windows."""
        from rapidfuzz import fuzz

        norm = re.sub(r"[^\w ]", "", text.lower())
        for (t1, src, prev) in self._recent_finals:
            if abs(job.t1 - t1) < 6 and fuzz.ratio(norm, prev) > 88 and (src != job.source or abs(job.t1 - t1) < 1.5):
                return True
        self._recent_finals.append((job.t1, job.source, norm))
        return False
