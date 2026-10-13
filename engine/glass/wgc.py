"""Windows Graphics Capture (WGC) sessions.

WGC is the compositor-level capture API used by the Snipping Tool, Teams and OBS.
Unlike GDI (BitBlt / PrintWindow) it includes hardware-accelerated video layers,
so Meet cameras and screen shares are not grey. A session stays open and the OS
delivers a frame only when the content changes (at most every
`min_interval_ms`), which is far cheaper than polling and re-rendering.
While a session is open Windows draws its own capture border around the
window — a built-in, honest "GLASS can see this" indicator.
"""
from __future__ import annotations

import logging
import threading
import time

import numpy as np

log = logging.getLogger("glass.wgc")


class WGCSession:
    def __init__(self, hwnd: int | None = None, monitor: int | None = None, min_interval_ms: int = 1000):
        from windows_capture import WindowsCapture

        self.hwnd, self.monitor = hwnd, monitor
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self.frame_time = 0.0
        self.closed = False
        self._stop = False
        # the OS-level minimum_update_interval needs Windows 11 24H2; throttle here instead
        self.min_interval = min_interval_ms / 1000.0
        cap = WindowsCapture(cursor_capture=False, draw_border=None,
                             monitor_index=monitor if hwnd is None else None, window_hwnd=hwnd)

        @cap.event
        def on_frame_arrived(frame, control):  # runs on the capture thread
            if self._stop:
                control.stop()
                return
            if time.time() - self.frame_time < self.min_interval:
                return  # drop without copying: keeps CPU use tiny while video plays
            img = np.array(frame.frame_buffer, copy=True)  # the native buffer is reused
            with self._lock:
                self._frame = img
                self.frame_time = time.time()

        @cap.event
        def on_closed():
            self.closed = True

        self._control = cap.start_free_threaded()

    def latest(self) -> tuple[np.ndarray | None, float]:
        with self._lock:
            return self._frame, self.frame_time

    def wait_first(self, timeout: float = 2.0) -> np.ndarray | None:
        end = time.time() + timeout
        while time.time() < end and not self.closed:
            f, _ = self.latest()
            if f is not None:
                return f
            time.sleep(0.02)
        return self.latest()[0]

    @property
    def alive(self) -> bool:
        try:
            return not self.closed and not self._control.is_finished()
        except Exception:
            return False

    def close(self) -> None:
        self._stop = True
        try:
            self._control.stop()
        except Exception:
            pass
        self.closed = True


def grab_monitor_once(monitor: int, timeout: float = 2.5) -> np.ndarray | None:
    """One frame of a whole monitor (1-based index, same order as mss)."""
    s = WGCSession(monitor=monitor, min_interval_ms=16)
    try:
        return s.wait_first(timeout)
    finally:
        s.close()
