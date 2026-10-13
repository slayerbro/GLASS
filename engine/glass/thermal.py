"""Thermal guard: keep GLASS from cooking a laptop.

Sustained speech + LLM + screen work on a laptop with weak or failed cooling
ends in GPU driver faults (nvlddmkm "Graphics FECS Exception") and a lock-up.
The guard watches, without admin rights:
  * GPU core temperature and thermal-slowdown flags (NVML)
  * CPU performance % (drops when the CPU throttles for heat or power)
  * NVIDIA driver errors in the Windows System log
and steps GLASS down before the machine gets into trouble:

  ok        normal profile
  warm      no live partial captions, no second-check pass, slower screen + notes
  hot       + background notes and screen reading paused; answers still work
  critical  capture paused entirely (driver errors or very high temperature)

Levels go up immediately and come down only after 45 s below the threshold.
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Callable

log = logging.getLogger("glass.thermal")

LEVELS = ["ok", "warm", "hot", "critical"]
WARM_C, HOT_C, CRITICAL_C = 78, 85, 91  # deliberately below the GPU's own 97 °C slowdown
COOL_DOWN_S = 45

RUNTIME = {
    "ok": {},
    "warm": {"partial_every": 0, "verify_answers": False, "vision_auto": False, "screen_interval": 4.0,
             "notes_interval": 600, "state_chars": 1500},
    "hot": {"partial_every": 0, "verify_answers": False, "vision_auto": False, "screen_interval": 6.0,
            "notes_interval": 1800, "notes_paused": True, "state_chars": 4000},
}
RUNTIME["critical"] = RUNTIME["hot"]


class ThermalGuard:
    def __init__(self, on_change: Callable[[str, dict], None]):
        self.on_change = on_change
        self.level = "ok"
        self.info: dict = {"level": "ok"}
        self.started = time.time()
        self._below_since: float | None = None
        self._nv = None
        self._pdh = None
        self._last_log_check = 0.0
        self.driver_errors = 0
        self._driver_fault_at = 0.0
        self._cpu_slow = 0
        self._gpu_slow = 0
        self._init()

    def _init(self) -> None:
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nv = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            self._nv = None
        try:
            import win32pdh

            q = win32pdh.OpenQuery()
            c = win32pdh.AddEnglishCounter(q, r"\Processor Information(_Total)\% Processor Performance")
            win32pdh.CollectQueryData(q)
            self._pdh = (q, c)
        except Exception:
            self._pdh = None

    # ------------------------------------------------------------------ sensors
    def _gpu(self) -> tuple[int | None, bool]:
        if self._nv is None:
            return None, False
        import pynvml

        try:
            t = pynvml.nvmlDeviceGetTemperature(self._nv, 0)
            reasons = pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(self._nv)
            thermal = bool(reasons & (0x20 | 0x40))  # SW / HW thermal slowdown
            return int(t), thermal
        except Exception:
            return None, False

    def _mac_limit(self) -> int | None:
        """macOS only: CPU_Speed_Limit from `pmset -g therm` (100 = not throttled)."""
        if sys.platform != "darwin":
            return None
        from .macos import cpu_speed_limit

        return cpu_speed_limit()

    def _cpu_perf(self) -> float | None:
        if self._pdh is None:
            return None
        import win32pdh

        try:
            win32pdh.CollectQueryData(self._pdh[0])
            return float(win32pdh.GetFormattedCounterValue(self._pdh[1], win32pdh.PDH_FMT_DOUBLE)[1])
        except Exception:
            return None

    def _new_driver_errors(self) -> int:
        """NVIDIA driver errors logged since the last check (and since GLASS started)."""
        now = time.time()
        if now - self._last_log_check < 6:
            return 0
        window_ms = int(min(now - self.started, now - self._last_log_check if self._last_log_check else 60) * 1000) + 1000
        self._last_log_check = now
        try:
            import win32evtlog

            q = win32evtlog.EvtQuery(
                "System", win32evtlog.EvtQueryReverseDirection,
                f"*[System[Provider[@Name='nvlddmkm'] and (Level=1 or Level=2) and TimeCreated[timediff(@SystemTime) <= {window_ms}]]]")
            n = 0
            while True:
                hs = win32evtlog.EvtNext(q, 50, -1, 0)
                if not hs:
                    break
                n += len(hs)
            return n
        except Exception:
            return 0

    # ------------------------------------------------------------------ decision
    def sample(self, cpu_load: float) -> dict:
        """Call every ~2 s (from a worker thread)."""
        now = time.time()
        temp, gpu_flag = self._gpu()
        # one stray "thermal slowdown" flag (e.g. while the GPU wakes from idle) is not heat:
        # require it for 3 samples in a row and a GPU that is actually warm
        self._gpu_slow = self._gpu_slow + 1 if gpu_flag and (temp is None or temp >= 70) else 0
        gpu_thermal = self._gpu_slow >= 3
        perf = self._cpu_perf()
        mac_limit = self._mac_limit()
        new_err = self._new_driver_errors()
        if new_err:
            self.driver_errors += new_err
            self._driver_fault_at = now
            log.warning("NVIDIA driver reported %d error(s) — pausing GPU work", new_err)
        # the CPU running well below its base speed while busy = throttling (heat or power limits)
        if perf is not None and cpu_load > 20 and perf < 55:
            self._cpu_slow += 1
        else:
            self._cpu_slow = max(0, self._cpu_slow - 1)
        cpu_throttled = self._cpu_slow >= 3

        want, reason = "ok", ""
        if self._driver_fault_at and now - self._driver_fault_at < 600:
            want, reason = "critical", "the graphics driver reported errors"
        elif temp is not None and temp >= CRITICAL_C:
            want, reason = "critical", f"GPU at {temp} °C"
        elif (temp is not None and temp >= HOT_C) or gpu_thermal:
            want, reason = "hot", f"GPU at {temp} °C" + (" and slowing down" if gpu_thermal else "")
        elif mac_limit is not None and mac_limit <= 50:
            want, reason = "critical", f"macOS cut the CPU to {mac_limit}% speed to cool down"
        elif mac_limit is not None and mac_limit <= 75:
            want, reason = "hot", f"macOS is slowing the CPU to {mac_limit}% (heat)"
        elif cpu_throttled:
            want, reason = "hot", "the CPU is being slowed down (heat or power saving)"
        elif mac_limit is not None and mac_limit < 100:
            want, reason = "warm", f"macOS is slowing the CPU to {mac_limit}% (heat)"
        elif temp is not None and temp >= WARM_C:
            want, reason = "warm", f"GPU at {temp} °C"

        if LEVELS.index(want) >= LEVELS.index(self.level):
            self._below_since = None
            new = want
        else:  # cooling down: only step back after a sustained quiet period
            self._below_since = self._below_since or now
            new = want if now - self._below_since >= COOL_DOWN_S else self.level
        if perf is None and mac_limit is not None:
            perf = float(mac_limit)
        self.info = {"level": new, "gpu_temp": temp, "gpu_thermal": gpu_thermal, "cpu_perf": perf,
                     "cpu_throttled": cpu_throttled, "driver_errors": self.driver_errors,
                     "reason": reason if new == want else self.info.get("reason", "")}
        if new != self.level:
            log.info("thermal level %s -> %s (%s)", self.level, new, reason)
            self.level = new
            self.on_change(new, self.info)
        return self.info

    def clear_fault(self) -> None:
        """The student chose to resume after a driver-error pause."""
        self._driver_fault_at = 0.0
        self._below_since = None
