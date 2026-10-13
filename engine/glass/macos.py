"""macOS platform layer (Intel and Apple Silicon, macOS 13+).

* System audio: the `glass-audio` ScreenCaptureKit helper (see engine/macos/),
  or a virtual loopback input such as BlackHole as a fallback.
* Microphone: sounddevice (CoreAudio).
* Windows / displays: Quartz window list; pixels via the built-in `screencapture`
  tool (the CGWindowListCreateImage API is obsolete on macOS 15).
* OCR: Apple Vision (VNRecognizeTextRequest).
* Thermals: `pmset -g therm` (CPU speed limit), no admin rights needed.

Nothing here is imported on Windows.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger("glass.macos")

BROWSERS = {"google chrome", "chrome", "safari", "arc", "microsoft edge", "firefox", "brave browser", "opera", "chromium"}


# ---------------------------------------------------------------------------- windows & displays
def _own_pids() -> set[int]:
    import psutil

    pids = {os.getpid()}
    try:
        p = psutil.Process()
        pids |= {c.pid for c in p.children(recursive=True)}
        for anc in p.parents():
            pids.add(anc.pid)
            if "glass" in anc.name().lower() or "electron" in anc.name().lower():
                pids |= {c.pid for c in anc.children(recursive=True)}
    except Exception:
        pass
    return pids


def list_windows() -> list[dict]:
    import Quartz

    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    infos = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    own = _own_pids()
    out = []
    for w in infos:
        if int(w.get(Quartz.kCGWindowLayer, 0)) != 0:
            continue
        pid = int(w.get(Quartz.kCGWindowOwnerPID, 0))
        if pid in own:
            continue
        b = w.get(Quartz.kCGWindowBounds) or {}
        width, height = int(b.get("Width", 0)), int(b.get("Height", 0))
        if width < 200 or height < 150:
            continue
        owner = str(w.get(Quartz.kCGWindowOwnerName) or "")
        # titles are only visible once Screen Recording permission is granted
        title = str(w.get(Quartz.kCGWindowName) or "")
        meet = "meet" in title.lower() and owner.lower() in BROWSERS
        out.append({"hwnd": int(w[Quartz.kCGWindowNumber]), "title": title or owner, "exe": owner,
                    "w": width, "h": height, "meet": meet, "minimized": False})
    out.sort(key=lambda w: (not w["meet"], w["exe"].lower() not in BROWSERS, w["title"].lower()))
    return out


def window_exists(wid: int) -> bool:
    return any(w["hwnd"] == wid for w in list_windows())


def list_monitors() -> list[dict]:
    """Displays in `screencapture -D` order (1 = main), bounds in points."""
    import Quartz

    err, ids, n = Quartz.CGGetActiveDisplayList(16, None, None)
    ids = list(ids or [])[: int(n or 0)]
    main = Quartz.CGMainDisplayID()
    order = [main] + [i for i in ids if i != main]
    out = []
    for idx, did in enumerate(order, 1):
        r = Quartz.CGDisplayBounds(did)
        out.append({"index": idx, "left": int(r.origin.x), "top": int(r.origin.y),
                    "w": int(r.size.width), "h": int(r.size.height)})
    return out


def _screencapture(args: list[str]) -> np.ndarray | None:
    """Run the built-in screencapture tool; returns a BGRA image (Retina pixels).

    The PNG is written to a private temp file and deleted immediately."""
    from PIL import Image

    fd, path = tempfile.mkstemp(suffix=".png", prefix="glass-")
    os.close(fd)
    try:
        r = subprocess.run(["screencapture", "-x", "-t", "png", *args, path], capture_output=True, timeout=15)
        if r.returncode != 0 or not os.path.getsize(path):
            return None
        with Image.open(path) as im:
            arr = np.array(im.convert("RGBA"))
        return np.ascontiguousarray(arr[:, :, [2, 1, 0, 3]])
    except Exception as e:
        log.warning("screencapture failed: %s", e)
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def capture_window(wid: int) -> np.ndarray | None:
    return _screencapture(["-o", f"-l{int(wid)}"])  # -o: no window shadow


def capture_display(index: int) -> np.ndarray | None:
    return _screencapture([f"-D{int(index)}"])


def capture_rect(x: int, y: int, w: int, h: int) -> np.ndarray | None:
    """Region in global points (top-left origin)."""
    return _screencapture([f"-R{int(x)},{int(y)},{int(w)},{int(h)}"])


# ---------------------------------------------------------------------------- OCR
def vision_boxes_to_pixels(boxes: list[tuple[str, float, float, float, float]], width: int, height: int) -> list[dict]:
    """Vision returns normalised boxes with a bottom-left origin; convert to top-left pixels."""
    out = []
    for text, x, y, w, h in boxes:
        out.append({"text": text, "x": x * width, "y": (1.0 - (y + h)) * height, "w": w * width, "h": h * height})
    return out


def vision_ocr(img_bgra: np.ndarray, languages: list[str] | None = None) -> list[dict]:
    import io

    import Vision
    from Foundation import NSData
    from PIL import Image

    h, w = img_bgra.shape[:2]
    buf = io.BytesIO()
    Image.fromarray(img_bgra[:, :, [2, 1, 0]]).save(buf, format="PNG")
    data = buf.getvalue()
    ns = NSData.dataWithBytes_length_(data, len(data))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(ns, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    # language correction "fixes" maths like 3x into words; screen text is clean enough without it
    req.setUsesLanguageCorrection_(False)
    if languages:
        req.setRecognitionLanguages_(languages)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f"Vision OCR failed: {err}")
    boxes = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        bb = obs.boundingBox()
        boxes.append((str(cands[0].string()), bb.origin.x, bb.origin.y, bb.size.width, bb.size.height))
    return vision_boxes_to_pixels(boxes, w, h)


def tesseract_cmd() -> str | None:
    return shutil.which("tesseract") or next((p for p in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract")
                                              if os.path.exists(p)), None)


# ---------------------------------------------------------------------------- thermals
def parse_pmset_therm(text: str) -> int | None:
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", text)
    return int(m.group(1)) if m else None


def cpu_speed_limit() -> int | None:
    """100 = full speed; lower = macOS is throttling the CPU for heat."""
    try:
        out = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=3).stdout
        return parse_pmset_therm(out)
    except Exception:
        return None


# ---------------------------------------------------------------------------- audio
def helper_path() -> Path | None:
    from .config import APP_DIR

    here = Path(__file__).resolve().parent.parent / "macos" / "glass-audio"
    for p in (APP_DIR / "bin" / "glass-audio", here):
        if p.exists() and os.access(p, os.X_OK):
            return p
    return None


PERMISSION_HINT = ("Allow GLASS in System Settings → Privacy & Security → Screen & System Audio Recording, "
                   "then restart GLASS.")


class MacSystemAudio:
    """System audio via the ScreenCaptureKit helper; falls back to a loopback
    input device (e.g. BlackHole) if the helper is missing or not permitted."""

    def __init__(self, source: str, device_name: str | None, out_queue: "queue.Queue", on_status: Callable[[str, dict], None]):
        self.source = source
        self.device_name = device_name
        self.q = out_queue
        self.on_status = on_status
        self.level = 0.0
        self.dropped = 0
        self.last_frame = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._fallback: MacInputAudio | None = None

    def start(self) -> None:
        self._stop.clear()
        if self.device_name and self.device_name != "System audio":
            self._start_fallback(self.device_name)
            return
        self._thread = threading.Thread(target=self._run, name=f"audio-{self.source}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
        if self._fallback:
            self._fallback.stop()
        if self._thread:
            self._thread.join(timeout=3)
        self.on_status("stopped", {})

    def request_restart(self) -> None:
        if self._proc:
            self._proc.kill()

    def _start_fallback(self, name: str | None) -> bool:
        dev = name or next((d for d in _input_devices() if re.search(r"blackhole|loopback|soundflower", d, re.I)), None)
        if not dev:
            return False
        self._fallback = MacInputAudio(self.source, dev, self.q, self.on_status)
        self._fallback.start()
        return True

    def _run(self) -> None:
        from .audio.capture import TARGET_SR, AudioChunk, _resample

        backoff = 1.0
        while not self._stop.is_set():
            helper = helper_path()
            if helper is None:
                if self._start_fallback(None):
                    return
                self.on_status("error", {"message": "System-audio helper not installed. Run engine/setup_engine.sh "
                                                    "(or install BlackHole and pick it in Settings → Audio)."})
                return
            rate = {"hz": TARGET_SR}
            err_lines: list[str] = []
            try:
                self._proc = subprocess.Popen([str(helper)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, bufsize=0)
            except OSError as e:
                self.on_status("error", {"message": f"Could not start system-audio helper: {e}"})
                return

            def read_err(p=self._proc):
                for raw in iter(p.stderr.readline, b""):
                    line = raw.decode(errors="replace").strip()
                    err_lines.append(line)
                    if line.startswith("RATE"):
                        rate["hz"] = int(line.split()[1])
                    elif line == "READY":
                        self.on_status("running", {"device": "System audio (ScreenCaptureKit)", "rate": rate["hz"]})

            threading.Thread(target=read_err, daemon=True).start()
            fd = self._proc.stdout.fileno()
            pending = b""
            got_audio = False
            while not self._stop.is_set():
                try:
                    data = os.read(fd, 8192)
                except OSError:
                    break
                if not data:
                    break
                got_audio = True
                data = pending + data
                usable = len(data) - (len(data) % 4)
                pending = data[usable:]
                x = np.frombuffer(data[:usable], dtype="<f4").astype(np.float32)
                y = _resample(x, rate["hz"]) if rate["hz"] != TARGET_SR else x
                self.level = float(np.sqrt(np.mean(y * y))) if len(y) else 0.0
                self.last_frame = time.time()
                try:
                    self.q.put_nowait(AudioChunk(self.source, y, self.last_frame))
                except queue.Full:
                    self.dropped += 1
            code = self._proc.wait()
            if self._stop.is_set():
                break
            msg = next((l[6:] for l in err_lines if l.startswith("ERROR")), f"helper exited ({code})")
            if re.search(r"declined|not authori|permission|TCC", msg, re.I):
                self.on_status("error", {"message": PERMISSION_HINT})
                if self._start_fallback(None):
                    return
                self._stop.wait(5)
                continue
            log.warning("system-audio helper stopped: %s", msg)
            self.on_status("error", {"message": msg})
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 10) if not got_audio else 1.0


def _input_devices() -> list[str]:
    try:
        import sounddevice as sd

        return [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]
    except Exception:
        return []


class MacInputAudio:
    """Any CoreAudio input: the microphone, or a loopback device like BlackHole."""

    def __init__(self, source: str, device_name: str | None, out_queue: "queue.Queue", on_status: Callable[[str, dict], None]):
        self.source = source
        self.device_name = device_name
        self.q = out_queue
        self.on_status = on_status
        self.level = 0.0
        self.dropped = 0
        self.last_frame = 0.0
        self._stream = None

    def start(self) -> None:
        import sounddevice as sd

        from .audio.capture import AudioChunk, _resample

        dev = None
        if self.device_name:
            dev = next((i for i, d in enumerate(sd.query_devices())
                        if d["max_input_channels"] > 0 and self.device_name in d["name"]), None)
        info = sd.query_devices(dev, "input")
        sr = int(info["default_samplerate"])

        def cb(indata, frames, t, status):
            x = indata[:, 0].astype(np.float32) if indata.ndim > 1 else indata.astype(np.float32)
            y = _resample(x, sr)
            self.level = float(np.sqrt(np.mean(y * y))) if len(y) else 0.0
            self.last_frame = time.time()
            try:
                self.q.put_nowait(AudioChunk(self.source, y, self.last_frame))
            except queue.Full:
                self.dropped += 1

        try:
            self._stream = sd.InputStream(device=dev, channels=1, samplerate=sr, blocksize=int(sr * 0.032),
                                          dtype="float32", callback=cb)
            self._stream.start()
            self.on_status("running", {"device": info["name"], "rate": sr})
        except Exception as e:
            self.on_status("error", {"message": f"{e}. If this is the microphone, allow GLASS in System Settings → "
                                                "Privacy & Security → Microphone."})

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        self.on_status("stopped", {})

    def request_restart(self) -> None:
        self.stop()
        self.start()


def list_audio_devices() -> dict:
    inputs = _input_devices()
    loop = [{"name": "System audio", "default": True}]
    loop += [{"name": d, "default": False} for d in inputs if re.search(r"blackhole|loopback|soundflower", d, re.I)]
    mics = [{"name": d, "default": i == 0} for i, d in enumerate(inputs) if not re.search(r"blackhole|loopback|soundflower", d, re.I)]
    return {"loopback": loop, "input": mics}
