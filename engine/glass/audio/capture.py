"""WASAPI audio capture (system loopback + optional microphone).

Each AudioSource runs its own PortAudio stream on a dedicated callback thread
and pushes 16 kHz mono float32 frames into a bounded queue. A watchdog restarts
the stream when the default output device changes or the stream errors out.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

log = logging.getLogger("glass.audio")

TARGET_SR = 16000

try:
    import soxr  # high quality, fast resampler

    def _resample(x: np.ndarray, sr: int) -> np.ndarray:
        return soxr.resample(x, sr, TARGET_SR).astype(np.float32, copy=False)
except Exception:  # pragma: no cover - fallback path
    def _resample(x: np.ndarray, sr: int) -> np.ndarray:
        if sr == TARGET_SR:
            return x
        n = int(round(len(x) * TARGET_SR / sr))
        return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


@dataclass
class AudioChunk:
    source: str  # "class" | "me"
    samples: np.ndarray  # float32 mono 16 kHz
    t_end: float  # wall-clock time of the last sample


def list_devices() -> dict:
    """Enumerate WASAPI loopback (output) and input devices."""
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
        default_in = None
        if wasapi.get("defaultInputDevice", -1) >= 0:
            default_in = p.get_device_info_by_index(wasapi["defaultInputDevice"])["name"]
        loopbacks, inputs = [], []
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != wasapi["index"]:
                continue
            if d.get("isLoopbackDevice"):
                name = d["name"].replace(" [Loopback]", "")
                loopbacks.append({"name": name, "default": name == default_out})
            elif d["maxInputChannels"] > 0:
                inputs.append({"name": d["name"], "default": d["name"] == default_in})
        return {"loopback": loopbacks, "input": inputs}
    finally:
        p.terminate()


def _default_output_id() -> str | None:
    """Cheap COM query for the current default render endpoint id."""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities

        comtypes.CoInitialize()
        try:
            dev = AudioUtilities.GetSpeakers()
            return dev.GetId() if hasattr(dev, "GetId") else str(dev.id)
        finally:
            comtypes.CoUninitialize()
    except Exception:
        return None


class AudioSource:
    """One capture stream. kind = 'loopback' | 'mic'."""

    def __init__(
        self,
        source: str,
        kind: str,
        device_name: str | None,
        out_queue: "queue.Queue[AudioChunk]",
        on_status: Callable[[str, dict], None],
    ):
        self.source = source
        self.kind = kind
        self.device_name = device_name
        self.q = out_queue
        self.on_status = on_status
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._restart = threading.Event()
        self.level = 0.0  # recent RMS, for the UI meter
        self.last_frame = 0.0
        self.dropped = 0

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"audio-{self.source}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._restart.set()
        if self._thread:
            self._thread.join(timeout=3)

    def request_restart(self) -> None:
        self._restart.set()

    # -- internals -----------------------------------------------------------
    def _pick_device(self, p):
        import pyaudiowpatch as pyaudio

        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        if self.kind == "loopback":
            if not self.device_name:
                return p.get_default_wasapi_loopback()
            for d in p.get_loopback_device_info_generator():
                if self.device_name in d["name"]:
                    return d
            log.warning("loopback device %r not found, using default", self.device_name)
            return p.get_default_wasapi_loopback()
        # microphone
        candidates = []
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] == wasapi["index"] and d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice"):
                candidates.append(d)
        if self.device_name:
            for d in candidates:
                if self.device_name in d["name"]:
                    return d
        idx = wasapi.get("defaultInputDevice", -1)
        if idx is not None and idx >= 0:
            return p.get_device_info_by_index(idx)
        if candidates:
            return candidates[0]
        raise RuntimeError("no microphone available")

    def _run(self) -> None:
        import pyaudiowpatch as pyaudio

        backoff = 1.0
        while not self._stop.is_set():
            p = None
            stream = None
            try:
                p = pyaudio.PyAudio()
                dev = self._pick_device(p)
                sr = int(dev["defaultSampleRate"])
                ch = max(1, int(dev["maxInputChannels"]))
                self._restart.clear()

                def callback(in_data, frame_count, time_info, status):
                    x = np.frombuffer(in_data, dtype=np.float32)
                    if ch > 1:
                        x = x.reshape(-1, ch).mean(axis=1)
                    y = _resample(x, sr)
                    self.level = float(np.sqrt(np.mean(y * y))) if len(y) else 0.0
                    self.last_frame = time.time()
                    try:
                        self.q.put_nowait(AudioChunk(self.source, y, self.last_frame))
                    except queue.Full:
                        self.dropped += 1  # backpressure: drop oldest audio is worse than newest
                    return (None, pyaudio.paContinue)

                stream = p.open(
                    format=pyaudio.paFloat32,
                    channels=ch,
                    rate=sr,
                    input=True,
                    input_device_index=dev["index"],
                    frames_per_buffer=int(sr * 0.032),
                    stream_callback=callback,
                )
                stream.start_stream()
                name = dev["name"].replace(" [Loopback]", "")
                self.on_status("running", {"device": name, "rate": sr, "channels": ch})
                log.info("%s capture started on %s (%d Hz, %d ch)", self.source, name, sr, ch)
                backoff = 1.0
                dev_id = _default_output_id() if self.kind == "loopback" and not self.device_name else None
                while not self._stop.is_set() and not self._restart.wait(2.0):
                    if not stream.is_active():
                        raise RuntimeError("stream stopped unexpectedly")
                    if dev_id is not None:
                        now_id = _default_output_id()
                        if now_id and now_id != dev_id:
                            log.info("default output device changed; restarting capture")
                            self.on_status("switching", {})
                            break
            except Exception as e:
                log.warning("%s capture error: %s", self.source, e)
                self.on_status("error", {"message": str(e)})
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 10)
            finally:
                try:
                    if stream is not None:
                        stream.stop_stream()
                        stream.close()
                except Exception:
                    pass
                if p is not None:
                    p.terminate()
        self.on_status("stopped", {})
