"""Screen intelligence: selective capture, change detection, OCR.

* Captures ONLY the chosen source: a specific window via PrintWindow
  (PW_RENDERFULLCONTENT — works for occluded Chromium windows and never
  includes windows on top of it), or a whole monitor via mss.
* A cheap 1/8-scale tile diff decides whether anything changed; OCR runs only
  after the image has changed AND settled (no OCR mid-scroll / mid-animation).
* Windows.Media.Ocr (fast, en + ar) with Tesseract as fallback.
* Nothing is written to disk unless the user enabled raw-media storage.
"""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import sys
import io
import os
import logging
import re
import time
from ctypes import wintypes
from typing import Callable

import numpy as np

from .mathsolve import find_problems, normalise
from .memory import ScreenObs, parse_numbered_items

log = logging.getLogger("glass.screen")

IS_MAC = sys.platform == "darwin"

if sys.platform == "win32":
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    dwmapi = ctypes.windll.dwmapi
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor: coordinates are physical pixels
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

MEET_NOISE = re.compile(
    r"^(turn on captions|turn off captions|present now|you are presenting|stop presenting|leave call|meeting details|"
    r"people|chat|activities|host controls|raise hand|lower hand|more options|send a reaction|is presenting|"
    r"\(you\)|you|others might still see|reactions|captions|pin|unpin|mute|unmute|share screen|"
    r"\d{1,2}:\d{2}\s*(am|pm)?(\s*\|.*)?|[a-z]{3}-[a-z]{4}-[a-z]{3})$",
    re.I,
)


# ---------------------------------------------------------------------------- windows
def list_windows() -> list[dict]:
    if IS_MAC:
        from . import macos

        return macos.list_windows()
    import win32gui
    import win32process
    import psutil

    own_pids = {psutil.Process().pid}
    try:
        own_pids |= {p.pid for p in psutil.Process().parent().children(recursive=True)} | {psutil.Process().parent().pid}
    except Exception:
        pass
    out = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title or title in ("Program Manager", "Windows Input Experience"):
            return True
        cloaked = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))  # DWMWA_CLOAKED
        if cloaked.value:
            return True
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        if r - l < 200 or b - t < 150:
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in own_pids:
            return True
        try:
            exe = psutil.Process(pid).name()
        except Exception:
            exe = ""
        if exe.lower() in ("textinputhost.exe", "shellexperiencehost.exe", "searchhost.exe", "startmenuexperiencehost.exe"):
            return True
        is_meet = "meet" in title.lower() and exe.lower() in ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe")
        out.append({"hwnd": hwnd, "title": title, "exe": exe, "w": r - l, "h": b - t, "meet": is_meet,
                    "minimized": bool(win32gui.IsIconic(hwnd))})
        return True

    win32gui.EnumWindows(cb, None)
    out.sort(key=lambda w: (not w["meet"], w["exe"].lower() not in ("chrome.exe", "msedge.exe", "firefox.exe"), w["title"].lower()))
    return out


def list_monitors() -> list[dict]:
    if IS_MAC:
        from . import macos

        return macos.list_monitors()
    import mss

    with mss.mss() as s:
        return [{"index": i, "left": m["left"], "top": m["top"], "w": m["width"], "h": m["height"]}
                for i, m in enumerate(s.monitors) if i > 0]


def capture_window(hwnd: int) -> np.ndarray | None:
    """BGRA image of one window's own content (not what is on top of it)."""
    import win32gui
    import win32ui

    if not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
        return None
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return None
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    try:
        ok = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
        if not ok:
            return None
        raw = bmp.GetBitmapBits(True)
        img = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
        return img.copy()
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def capture_monitor(index: int, mask_rects: list[tuple[int, int, int, int]] | None = None) -> np.ndarray:
    if IS_MAC:
        from . import macos

        img = macos.capture_display(index)
        if img is not None and mask_rects:
            m = next((x for x in macos.list_monitors() if x["index"] == index), None)
            if m:
                img = mask_points(img, m, mask_rects)
        return img
    import mss

    with mss.mss() as s:
        mons = s.monitors
        m = mons[index] if 0 < index < len(mons) else mons[1]
        img = np.array(s.grab(m))  # BGRA
    for (x, y, w, h) in mask_rects or []:
        x0, y0 = x - m["left"], y - m["top"]
        img[max(0, y0):max(0, y0 + h), max(0, x0):max(0, x0 + w)] = 0
    return img


def mask_points(img: np.ndarray, mon: dict, rects) -> np.ndarray:
    """Blank rects given in global points on a Retina (pixel) capture of `mon`."""
    s = img.shape[1] / max(1, mon["w"])
    img = img.copy()
    for (x, y, w, h) in rects:
        x0, y0 = int((x - mon["left"]) * s), int((y - mon["top"]) * s)
        img[max(0, y0):max(0, y0 + int(h * s)), max(0, x0):max(0, x0 + int(w * s))] = 0
    return img


def capture_rect(x: int, y: int, w: int, h: int) -> np.ndarray:
    if IS_MAC:
        from . import macos

        return macos.capture_rect(x, y, w, h)
    import mss

    with mss.mss() as s:
        return np.array(s.grab({"left": x, "top": y, "width": w, "height": h}))


def to_png(img: np.ndarray, max_side: int = 1600) -> bytes:
    from PIL import Image

    im = Image.fromarray(img[:, :, [2, 1, 0]])
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


# ---------------------------------------------------------------------------- OCR
def _tesseract() -> str | None:
    if IS_MAC:
        from . import macos

        return macos.tesseract_cmd()
    p = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    return p if os.path.exists(p) else None


class OCR:
    def __init__(self, engine: str = "windows"):
        self.engine = engine
        self._win = {}
        self.last_ms = 0.0

    def _win_engine(self, lang: str):
        if lang not in self._win:
            from winrt.windows.globalization import Language
            from winrt.windows.media.ocr import OcrEngine

            eng = OcrEngine.try_create_from_language(Language(lang))
            self._win[lang] = eng
        return self._win[lang]

    async def read(self, img: np.ndarray, lang: str = "en-US") -> list[dict]:
        """Returns lines: [{text, x, y, w, h}] in image pixel coordinates."""
        t = time.perf_counter()
        h, w = img.shape[:2]
        scale = 1.0
        if max(w, h) < 1400:
            scale = min(2.0, 2000 / max(w, h))  # small text recognises far better upscaled
        try:
            if IS_MAC and self.engine != "tesseract":
                from . import macos

                langs = ["en-US", "ar-SA"] if lang.startswith("ar") else ["en-US"]
                lines = await asyncio.to_thread(macos.vision_ocr, img, langs)
                if _tesseract():
                    lines = await asyncio.to_thread(self._repair_math_lines, img, lines)
            elif self.engine == "windows":
                lines = await self._read_windows(img, lang, scale)
                lines = await asyncio.to_thread(self._repair_math_lines, img, lines)
            else:
                lines = await asyncio.to_thread(self._read_tesseract, img, lang)
        except Exception as e:
            log.warning("OCR failed (%s); trying tesseract", e)
            if not _tesseract():
                raise
            lines = await asyncio.to_thread(self._read_tesseract, img, lang)
        self.last_ms = (time.perf_counter() - t) * 1000
        return lines

    @staticmethod
    def _math_score(s: str) -> int:
        return len(re.findall(r"[0-9a-zA-Z()=+\-*/^]", s))

    def _repair_math_lines(self, img: np.ndarray, lines: list[dict]) -> list[dict]:
        """Windows OCR drops dictionary-unlike tokens such as '3x' or '4(x'.
        Re-read math-looking lines with Tesseract (single-line mode) and keep
        whichever reading preserves more of the maths."""
        import pytesseract
        from PIL import Image

        if not _tesseract():
            return lines
        pytesseract.pytesseract.tesseract_cmd = _tesseract()
        H, W = img.shape[:2]
        mathy = [l for l in lines if re.search(r"=|\d\s*[-+×÷*/]|[-+]\s*\d|\bsolve\b|\bsimplify\b|\bexpand\b|\bfactori", l["text"], re.I)]
        def fix(l: dict) -> None:
            pad = int(l["h"] * 0.45) + 2
            x0, y0 = max(0, int(l["x"]) - pad * 2), max(0, int(l["y"]) - pad)
            x1, y1 = min(W, int(l["x"] + l["w"]) + pad * 2), min(H, int(l["y"] + l["h"]) + pad)
            crop = img[y0:y1, x0:x1, [2, 1, 0]]
            if crop.size == 0:
                return
            im = Image.fromarray(crop)
            if l["h"] < 28:
                im = im.resize((im.width * 2, im.height * 2), Image.BICUBIC)
            try:
                alt = pytesseract.image_to_string(im, lang="eng", config="--psm 7").strip()
            except Exception:
                return
            alt = re.sub(r"([a-z])\?(?=\s*[-+=)])", r"\1^2", alt)  # x² read as x?
            if self._math_score(alt) > self._math_score(l["text"]) + 1:
                l["text"] = alt

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(fix, mathy[:14]))
        return lines

    async def _read_windows(self, img: np.ndarray, lang: str, scale: float) -> list[dict]:
        from PIL import Image
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter

        if scale != 1.0:
            im = Image.fromarray(img).resize((int(img.shape[1] * scale), int(img.shape[0] * scale)), Image.BICUBIC)
            arr = np.array(im)
        else:
            arr = np.array(img)  # writable, contiguous copy
        arr[:, :, 3] = 255
        h, w = arr.shape[:2]
        if max(w, h) > 10000:
            raise ValueError("image too large for OCR")
        writer = DataWriter()
        writer.write_bytes(arr.tobytes())
        bmp = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h)
        eng = self._win_engine(lang) or self._win_engine("en-US")
        res = await eng.recognize_async(bmp)
        out = []
        for line in res.lines:
            words = list(line.words)
            if not words:
                continue
            xs = [wd.bounding_rect.x for wd in words]
            ys = [wd.bounding_rect.y for wd in words]
            x2 = [wd.bounding_rect.x + wd.bounding_rect.width for wd in words]
            y2 = [wd.bounding_rect.y + wd.bounding_rect.height for wd in words]
            out.append({"text": line.text, "x": min(xs) / scale, "y": min(ys) / scale,
                        "w": (max(x2) - min(xs)) / scale, "h": (max(y2) - min(ys)) / scale})
        return out

    def _read_tesseract(self, img: np.ndarray, lang: str) -> list[dict]:
        import pytesseract
        from PIL import Image

        pytesseract.pytesseract.tesseract_cmd = _tesseract()
        im = Image.fromarray(img[:, :, [2, 1, 0]])
        data = pytesseract.image_to_data(im, lang="ara+eng" if lang.startswith("ar") else "eng",
                                         output_type=pytesseract.Output.DICT)
        lines: dict[tuple, dict] = {}
        for i, word in enumerate(data["text"]):
            if not word.strip() or float(data["conf"][i]) < 40:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            ln = lines.setdefault(key, {"words": [], "x": 1e9, "y": 1e9, "x2": 0, "y2": 0})
            ln["words"].append(word)
            ln["x"] = min(ln["x"], data["left"][i])
            ln["y"] = min(ln["y"], data["top"][i])
            ln["x2"] = max(ln["x2"], data["left"][i] + data["width"][i])
            ln["y2"] = max(ln["y2"], data["top"][i] + data["height"][i])
        return [{"text": " ".join(l["words"]), "x": l["x"], "y": l["y"], "w": l["x2"] - l["x"], "h": l["y2"] - l["y"]}
                for l in lines.values()]


def lines_to_text(lines: list[dict]) -> str:
    """Reading order, Meet UI chrome removed."""
    keep = [l for l in lines if not MEET_NOISE.match(l["text"].strip()) and len(l["text"].strip()) > 1]
    keep.sort(key=lambda l: (round(l["y"] / max(8, l["h"] * 0.6)), l["x"]))
    return "\n".join(normalise(l["text"]) for l in keep)


# ---------------------------------------------------------------------------- service
class ScreenService:
    def __init__(self, settings, on_obs: Callable[[ScreenObs], None], on_status: Callable[[str, dict], None],
                 on_vision: Callable[[ScreenObs, bytes], None] | None = None):
        self.settings = settings
        self.on_obs = on_obs
        self.on_status = on_status
        self.on_vision = on_vision
        self.ocr = OCR(settings.get("ocr_engine") or "windows")
        self.mask_rects: list[tuple[int, int, int, int]] = []  # GLASS's own windows (physical px)
        self._task: asyncio.Task | None = None
        self._paused = False
        self._prev_small: np.ndarray | None = None
        self._last_full: np.ndarray | None = None
        self._last_title = ""
        self._tiles_prev: np.ndarray | None = None
        self._tiles_ocr: np.ndarray | None = None
        self._activity: np.ndarray | None = None
        self._pending_since = 0.0
        self._last_ocr_small: np.ndarray | None = None
        self._pending_change = False
        self._last_text = ""
        self.stats = {"captures": 0, "ocr_runs": 0, "last_change": 0.0, "capture_ms": 0.0, "interval": 0.0}
        self._wgc = None  # open Windows Graphics Capture session for the selected source
        self._wgc_key = None
        self._wgc_seen = 0.0  # timestamp of the last WGC frame we analysed
        self._frozen = None  # (image, monitor_left, monitor_top, time) for Snip & Solve
        self.method = "mac" if IS_MAC else "wgc"
        self.language = "en-US"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._paused = False
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._close_wgc()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self.on_status("off", {})

    def pause(self, paused: bool) -> None:
        self._paused = paused
        self.on_status("paused" if paused else "watching", {})

    def _close_wgc(self) -> None:
        if self._wgc is not None:
            self._wgc.close()
        self._wgc, self._wgc_key = None, None

    def _grab_wgc(self, key: tuple, hwnd: int | None, monitor: int | None) -> np.ndarray | None | str:
        """Latest frame from a persistent WGC session; "same" when nothing changed since last time."""
        from .wgc import WGCSession

        interval_ms = int(float(self.settings.profile().get("screen_interval", 1.5)) * 1000)
        if self._wgc is None or self._wgc_key != key or not self._wgc.alive:
            self._close_wgc()
            self._wgc = WGCSession(hwnd=hwnd, monitor=monitor, min_interval_ms=interval_ms)
            self._wgc_key = key
            self._wgc_seen = 0.0
            return self._wgc.wait_first(2.0)
        frame, t = self._wgc.latest()
        if frame is not None and t <= self._wgc_seen:
            return "same"
        self._wgc_seen = t
        return frame

    def _grab(self) -> tuple[np.ndarray | None | str, str]:
        src = self.settings.get("screen_source") or {"kind": "monitor", "monitor": 1}
        if self.method == "mac":
            return self._grab_mac(src)
        if self.method == "wgc":
            try:
                return self._grab_via_wgc(src)
            except Exception as e:  # WGC unavailable (old Windows, policy): fall back to GDI
                log.warning("Windows Graphics Capture failed (%s); falling back to GDI capture", e)
                self._close_wgc()
                self.method = "gdi"
        return self._grab_gdi(src)

    def _grab_mac(self, src: dict) -> tuple[np.ndarray | None, str]:
        from . import macos

        if src.get("kind") == "window":
            wid = src.get("hwnd")
            wins = macos.list_windows()
            win = next((w for w in wins if w["hwnd"] == wid), None)
            if win is None:  # window recreated (browser restart / new tab window): find it again
                win = next((w for w in wins if src.get("title") and (w["title"] == src["title"] or (src.get("meet") and w["meet"]))), None)
                if win is None:
                    return None, ""
                src["hwnd"] = win["hwnd"]
                self.settings.update({"screen_source": src})
            return macos.capture_window(win["hwnd"]), win["title"]
        mon = int(src.get("monitor", 1))
        return capture_monitor(mon, self.mask_rects), f"Display {mon}"

    def _resolve_hwnd(self, src: dict) -> int | None:
        import win32gui

        hwnd = src.get("hwnd")
        if not hwnd or not win32gui.IsWindow(hwnd):
            # window handle changed (browser restarted): re-find by title
            for w in list_windows():
                if src.get("title") and (w["title"] == src["title"] or (src.get("meet") and w["meet"])):
                    hwnd = w["hwnd"]
                    src["hwnd"] = hwnd
                    self.settings.update({"screen_source": src})
                    break
            else:
                return None
        return hwnd

    def _grab_via_wgc(self, src: dict) -> tuple[np.ndarray | None | str, str]:
        import win32gui

        if src.get("kind") == "window":
            hwnd = self._resolve_hwnd(src)
            if not hwnd or win32gui.IsIconic(hwnd):
                return None, ""
            return self._grab_wgc(("w", hwnd), hwnd, None), win32gui.GetWindowText(hwnd)
        mon = int(src.get("monitor", 1))
        img = self._grab_wgc(("m", mon), None, mon)
        if isinstance(img, np.ndarray) and self.mask_rects:
            mons = list_monitors()
            m = next((x for x in mons if x["index"] == mon), mons[0])
            img = img.copy()
            for (x, y, w, h) in self.mask_rects:  # never read GLASS's own overlay
                x0, y0 = x - m["left"], y - m["top"]
                img[max(0, y0):max(0, y0 + h), max(0, x0):max(0, x0 + w)] = 0
        return img, f"Monitor {mon}"

    def _grab_gdi(self, src: dict) -> tuple[np.ndarray | None, str]:
        if src.get("kind") == "window":
            hwnd = src.get("hwnd")
            import win32gui

            if not hwnd or not win32gui.IsWindow(hwnd):
                # window handle changed (browser restarted): re-find by title
                for w in list_windows():
                    if src.get("title") and (w["title"] == src["title"] or (src.get("meet") and w["meet"])):
                        hwnd = w["hwnd"]
                        src["hwnd"] = hwnd
                        self.settings.update({"screen_source": src})
                        break
            if not hwnd:
                return None, ""
            img = capture_window(hwnd)
            return img, win32gui.GetWindowText(hwnd) if img is not None else ""
        return capture_monitor(int(src.get("monitor", 1)), self.mask_rects), f"Monitor {src.get('monitor', 1)}"

    def _prepare(self, img: np.ndarray) -> np.ndarray:
        h, w = img.shape[:2]
        crop = self.settings.get("screen_crop")
        if crop:
            x, y, cw, ch = crop
            img = img[int(y * h):int((y + ch) * h), int(x * w):int((x + cw) * w)]
            h, w = img.shape[:2]
        zones = self.settings.get("exclusion_zones") or []
        if zones:
            img = img.copy()
            for (x, y, zw, zh) in zones:
                img[int(y * h):int((y + zh) * h), int(x * w):int((x + zw) * w)] = 0
        return img

    @staticmethod
    def _small(img: np.ndarray) -> np.ndarray:
        g = img[::8, ::8, :3].astype(np.float32).mean(axis=2)
        return g

    @staticmethod
    def _diff(a: np.ndarray | None, b: np.ndarray) -> float:
        if a is None or a.shape != b.shape:
            return 1.0
        return float((np.abs(a - b) > 18).mean())

    def _content_change(self, small: np.ndarray) -> tuple[float, float]:
        """(change since last frame, change since last OCR) over STATIC regions only.

        Tiles that keep changing (webcam tiles, a playing video) are learnt as "live"
        and ignored, so a moving face never triggers OCR — only a new slide,
        worksheet or scroll does. This keeps CPU use (and heat) near zero during
        talking-head stretches of a lesson."""
        g = small
        th, tw = max(1, g.shape[0] // 16), max(1, g.shape[1] // 24)
        H, W = th * 16, tw * 24
        tiles = g[:H, :W].reshape(16, th, 24, tw).mean(axis=(1, 3))
        prev = self._tiles_prev
        self._tiles_prev = tiles
        if prev is None or prev.shape != tiles.shape:
            self._activity = np.zeros_like(tiles)
            return 1.0, 1.0
        moved = np.abs(tiles - prev) > 6
        self._activity = 0.75 * self._activity + 0.25 * moved
        static = self._activity < 0.35
        d_prev = float((moved & static).sum()) / max(1, static.sum())
        if self._tiles_ocr is None or self._tiles_ocr.shape != tiles.shape:
            return d_prev, 1.0
        d_ocr = float(((np.abs(tiles - self._tiles_ocr) > 6) & static).sum()) / max(1, static.sum())
        return d_prev, d_ocr

    async def _loop(self) -> None:
        """Change-driven screen reading: OCR runs once after content changes and settles."""
        self.on_status("watching", {})
        fails = 0
        while True:
            base = float(self.settings.profile().get("screen_interval", 1.5))
            # adaptive: poll faster right after a change, back off when static
            since = time.time() - self.stats["last_change"]
            interval = base if since < 20 else min(base * 2.5, 5.0)
            self.stats["interval"] = interval
            await asyncio.sleep(interval)
            if self._paused:
                continue
            try:
                t = time.perf_counter()
                img, title = await asyncio.to_thread(self._grab)
                if isinstance(img, str):
                    # no new frame from WGC = nothing on screen changed: the last frame is stable
                    if self._pending_change and self._last_full is not None:
                        self._pending_change = False
                        self._tiles_ocr = self._tiles_prev
                        await self._analyse(self._last_full, self._last_title)
                    continue
                if img is None:
                    fails += 1
                    if fails == 3:
                        self.on_status("unavailable", {"message": "The selected window is minimised or closed"})
                    continue
                if fails >= 3:
                    self.on_status("watching", {"method": self.method})
                fails = 0
                img = self._prepare(img)
                self._last_full, self._last_title = img, title
                d_prev, d_ocr = self._content_change(self._small(img))
                self.stats["capture_ms"] = (time.perf_counter() - t) * 1000
                self.stats["captures"] += 1
                if d_ocr > 0.02:
                    if not self._pending_change:
                        self._pending_since = time.time()
                    self._pending_change = True
                    self.stats["last_change"] = time.time()
                # OCR once the change has settled (or after 6 s of continuous change, e.g. scrolling)
                if self._pending_change and (d_prev < 0.02 or time.time() - self._pending_since > 6):
                    self._pending_change = False
                    self._tiles_ocr = self._tiles_prev
                    await self._analyse(img, title)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("screen loop error")
                self.on_status("error", {"message": str(e)})
                await asyncio.sleep(2)

    async def _analyse(self, img: np.ndarray, title: str, source: str = "screen") -> ScreenObs:
        lines = await self.ocr.read(img, self.language)
        self.stats["ocr_runs"] += 1
        text = lines_to_text(lines)
        obs = ScreenObs(time.time(), text, parse_numbered_items(text), find_problems(text), title, source=source)
        if source == "screen" and text == self._last_text:
            return obs
        self._last_text = text
        self.on_obs(obs)
        # complex visual content with little text: ask the vision model (Quality profile)
        if self.on_vision and self.settings.profile().get("vision_auto") and len(text) < 120:
            png = await asyncio.to_thread(to_png, img, 1280)
            self.on_vision(obs, png)
        return obs

    def freeze(self, x: int, y: int) -> tuple[np.ndarray, dict] | None:
        """Snap the whole monitor containing (x, y) BEFORE the selection window opens.

        The full-screen selection window makes Chrome believe it is hidden, and it
        stops painting Meet's video (cameras/screen share turn grey). Freezing first —
        like the Windows Snipping Tool — captures what the student actually saw."""
        mons = list_monitors()
        m = next((m for m in mons if m["left"] <= x < m["left"] + m["w"] and m["top"] <= y < m["top"] + m["h"]), mons[0])
        img = None
        if not IS_MAC:
            from .wgc import grab_monitor_once

            try:
                img = grab_monitor_once(m["index"])
            except Exception as e:
                log.warning("WGC freeze failed (%s); using GDI", e)
        if img is None:
            img = capture_monitor(m["index"])
        if img is None:
            return None
        self._frozen = (img, m, time.time())
        # macOS: rects arrive in points, the capture is in (Retina) pixels; Windows: both physical px
        return img, m

    async def snip(self, x: int, y: int, w: int, h: int) -> tuple[bytes, str, bool]:
        """Returns (png, ocr_text, blank). `blank` = uniform image (locked screen, black DRM surface)."""
        img = None
        if self._frozen and time.time() - self._frozen[2] < 120:
            full, m, _ = self._frozen
            self._frozen = None
            s = full.shape[1] / max(1, m["w"]) if IS_MAC else 1.0
            x0, y0 = max(0, int((x - m["left"]) * s)), max(0, int((y - m["top"]) * s))
            img = full[y0:y0 + int(h * s), x0:x0 + int(w * s)]
            if img.size == 0:
                img = None
        if img is None:
            img = await asyncio.to_thread(capture_rect, x, y, w, h)
        gray = img[::4, ::4, :3].astype(np.float32).mean(axis=2)
        if float(gray.std()) < 3.0:
            return b"", "", True
        lines = await self.ocr.read(img, self.language)
        text = "\n".join(normalise(l["text"]) for l in sorted(lines, key=lambda l: (round(l["y"] / 10), l["x"])))
        png = await asyncio.to_thread(to_png, img, 1600)
        return png, text, False

    @staticmethod
    def image_hash(png: bytes) -> str:
        return hashlib.sha1(png).hexdigest()[:16]
