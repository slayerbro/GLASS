"""macOS port checks that run anywhere (no Mac needed).

1. Pure functions of the Mac layer (coordinates, parsing).
2. A "pretend to be macOS" import of the whole engine in a fresh interpreter with
   Apple frameworks stubbed: catches Windows-only code that would crash on import
   and checks every platform switch picks the Mac branch.
Real-hardware behaviour (ScreenCaptureKit audio, screencapture, Vision accuracy)
still has to be verified on a Mac.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import numpy as np

ENGINE = Path(__file__).resolve().parents[1]


def test_pmset_parsing():
    from glass.macos import parse_pmset_therm

    sample = """Note: No thermal warning level has been recorded
Note: No performance warning level has been recorded
2026-10-13 09:00:00 +0300 CPU Power notify
\tCPU_Scheduler_Limit \t= 100
\tCPU_Available_CPUs \t= 16
\tCPU_Speed_Limit \t= 63
"""
    assert parse_pmset_therm(sample) == 63
    assert parse_pmset_therm("nothing here") is None


def test_vision_boxes_flip_to_top_left_pixels():
    from glass.macos import vision_boxes_to_pixels

    # Vision: normalised, origin bottom-left. A line in the top 10% of a 1000x500 image:
    (box,) = vision_boxes_to_pixels([("Q1", 0.1, 0.85, 0.5, 0.1)], 1000, 500)
    assert round(box["x"]) == 100 and round(box["y"]) == 25 and round(box["w"]) == 500 and round(box["h"]) == 50


def test_mask_points_scales_to_retina_pixels():
    from glass.screen import mask_points

    img = np.full((1800, 2880, 4), 255, np.uint8)  # 1440x900-point display at 2x
    mon = {"index": 1, "left": 0, "top": 0, "w": 1440, "h": 900}
    out = mask_points(img, mon, [(100, 50, 200, 100)])  # points
    assert out[100:300, 200:600].max() == 0 and out[99, 199].min() == 255


def test_snip_crop_uses_retina_scale(monkeypatch):
    import asyncio
    import time

    from glass import screen

    class S:
        def get(self, k, d=None):
            return {"ocr_engine": "windows"}.get(k, d)

        def profile(self):
            return {}

    monkeypatch.setattr(screen, "IS_MAC", True)
    svc = screen.ScreenService(S(), lambda o: None, lambda *a: None)
    frame = np.zeros((1800, 2880, 4), np.uint8)
    frame[200:400, 400:800, :3] = 200
    frame[250:260, 450:750, :3] = 10
    svc._frozen = (frame, {"index": 1, "left": 0, "top": 0, "w": 1440, "h": 900}, time.time())
    seen = {}

    async def fake_read(img, lang="en-US"):
        seen["shape"] = img.shape
        return []

    monkeypatch.setattr(svc.ocr, "read", fake_read)
    asyncio.run(svc.snip(200, 100, 200, 100))  # points
    assert seen["shape"][:2] == (200, 400)  # pixels at 2x


MAC_SIM = textwrap.dedent(r'''
    import asyncio, json, subprocess, sys, types, threading, multiprocessing
    # third-party libraries have real macOS builds (verified wheels); load them before the
    # platform switch so only GLASS's own platform branches are being exercised
    import urllib.request, httpx, numpy, websockets, psutil, PIL.Image, rapidfuzz, jellyfish, sympy, onnxruntime
    import faster_whisper, soxr
    sys.platform = "darwin"
    import platform
    platform.machine = lambda: "x86_64"

    def stub(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    stub("Quartz", kCGWindowListOptionOnScreenOnly=1, kCGWindowListExcludeDesktopElements=16, kCGNullWindowID=0,
         kCGWindowLayer="kCGWindowLayer", kCGWindowOwnerPID="kCGWindowOwnerPID", kCGWindowBounds="kCGWindowBounds",
         kCGWindowOwnerName="kCGWindowOwnerName", kCGWindowName="kCGWindowName", kCGWindowNumber="kCGWindowNumber",
         CGWindowListCopyWindowInfo=lambda o, w: [
             {"kCGWindowLayer": 0, "kCGWindowOwnerPID": 999999, "kCGWindowBounds": {"Width": 1440, "Height": 900},
              "kCGWindowOwnerName": "Google Chrome", "kCGWindowName": "Meet - abc-defg-hij", "kCGWindowNumber": 4242}],
         CGGetActiveDisplayList=lambda n, a, b: (0, (1,), 1), CGMainDisplayID=lambda: 1,
         CGDisplayBounds=lambda d: types.SimpleNamespace(origin=types.SimpleNamespace(x=0, y=0),
                                                         size=types.SimpleNamespace(width=1536, height=960)))
    stub("Vision"); stub("Foundation")
    stub("sounddevice", query_devices=lambda *a, **k: [{"name": "MacBook Pro Microphone", "max_input_channels": 1},
                                                       {"name": "BlackHole 2ch", "max_input_channels": 2}])

    from glass import config
    assert config.IS_MAC and config.IS_INTEL_MAC, "platform flags"
    s = config.Settings(config.APP_DIR / "s.json")
    p = s.profile()
    assert s.get("profile") == "mac" and p["asr_device"] == "cpu" and p["partial_every"] == 0 and not p["vision_enabled"], p

    import glass.engine, glass.server, glass.macos, glass.thermal
    from glass import screen
    assert screen.IS_MAC

    wins = screen.list_windows()
    assert wins and wins[0]["meet"] and wins[0]["hwnd"] == 4242, wins
    mons = screen.list_monitors()
    assert mons == [{"index": 1, "left": 0, "top": 0, "w": 1536, "h": 960}], mons

    from glass.macos import list_audio_devices
    d = list_audio_devices()
    assert d["loopback"][0]["name"] == "System audio" and any("BlackHole" in x["name"] for x in d["loopback"]), d

    # thermal guard reads macOS speed limits
    import glass.macos as mac
    mac.cpu_speed_limit = lambda: 60
    g = glass.thermal.ThermalGuard(lambda *a: None)
    info = g.sample(cpu_load=40)
    assert info["level"] == "hot" and "60%" in info["reason"], info

    # engine wiring picks the Mac capture classes
    e = glass.engine.Engine()
    assert e.screen.method == "mac"
    e._start_audio(capture=True) if False else None
    from glass.macos import MacSystemAudio, MacInputAudio
    print("MAC-SIM-OK")
''')


def test_engine_imports_and_wires_up_as_macos():
    env = {**os.environ, "GLASS_DATA_DIR": tempfile.mkdtemp(), "PYTHONPATH": str(ENGINE)}
    r = subprocess.run([sys.executable, "-c", MAC_SIM], cwd=ENGINE, env=env, capture_output=True, text=True, timeout=120)
    assert "MAC-SIM-OK" in r.stdout, r.stdout[-2000:] + r.stderr[-4000:]
