"""Drive the RUNNING desktop app end to end (real loopback audio, real window capture).

The Electron app must be started with GLASS_TEST=1 GLASS_DEV_TOKEN=<token>.
Plays classroom clips through the default speakers (so WASAPI loopback hears
them), shows a worksheet in its own window as the screen source, and saves
PrintWindow captures of the overlay after each step to tests/out/.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import winsound
from pathlib import Path

import websockets

HERE = Path(__file__).parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
TOKEN = os.environ.get("GLASS_DEV_TOKEN", "devtoken")
PORT = int(os.environ.get("GLASS_PORT", 47821))
EVENTS: list[dict] = []
rid = 0
replies: dict[int, asyncio.Future] = {}


def show_worksheet() -> int:
    """A plain window showing the worksheet image; returns its hwnd."""
    import tkinter as tk

    from PIL import Image, ImageTk

    ready = threading.Event()
    box = {}

    def run():
        root = tk.Tk()
        root.title("Worksheet - Exercise 4B")
        img = ImageTk.PhotoImage(Image.open(HERE / "fixtures" / "screens" / "worksheet.png").resize((1200, 675)))
        tk.Label(root, image=img).pack()
        root.geometry("+40+60")
        root.update()
        box["hwnd"] = int(root.wm_frame(), 16)
        ready.set()
        root.mainloop()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(10)
    return box["hwnd"]


def capture_overlay(name: str) -> None:
    import psutil
    import win32gui
    import win32process
    from PIL import Image

    sys.path.insert(0, str(HERE.parent))
    from glass.screen import capture_window

    hits = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == "GLASS":
            _, pid = win32process.GetWindowThreadProcessId(h)
            if psutil.Process(pid).name() == "electron.exe":
                l, t, r, b = win32gui.GetWindowRect(h)
                hits.append(((r - l) * (b - t), h))
        return True

    win32gui.EnumWindows(cb, None)
    if not hits:
        print("   (overlay window not found)")
        return
    h = min(hits)[1]  # the overlay is the smaller GLASS window
    img = capture_window(h)
    if img is not None:
        Image.fromarray(img[:, :, [2, 1, 0]]).save(OUT / f"{name}.png")
        print(f"   saved tests/out/{name}.png")


async def call(ws, name: str, **kw):
    global rid
    rid += 1
    fut = asyncio.get_running_loop().create_future()
    replies[rid] = fut
    await ws.send(json.dumps({"cmd": name, "rid": rid, **kw}))
    r = await asyncio.wait_for(fut, 120)
    if not r["ok"]:
        raise RuntimeError(r["error"])
    return r["result"]


async def reader(ws):
    async for raw in ws:
        e = json.loads(raw)
        if e["type"] == "reply" and e["rid"] in replies:
            replies.pop(e["rid"]).set_result(e)
        else:
            e.setdefault("ts", time.time())
            EVENTS.append(e)


def play(name: str) -> float:
    path = HERE / "fixtures" / "audio" / f"{name}.wav"
    winsound.PlaySound(str(path), winsound.SND_FILENAME)  # blocks until played
    return time.time()


def last_answer(after: float):
    out = None
    for e in EVENTS:
        if e["type"] == "answer.update" and e["answer"]["t"] >= after - 1:
            out = e["answer"]
    return out


async def wait_answer(after: float, timeout=25):
    t = time.time()
    while time.time() - t < timeout:
        a = last_answer(after)
        if a and a["status"] in ("done", "error"):
            return a
        await asyncio.sleep(0.1)
    return last_answer(after)


async def main():
    hwnd = show_worksheet()
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/?token={TOKEN}", max_size=2**23) as ws:
        asyncio.create_task(reader(ws))
        snap = await call(ws, "snapshot")
        for _ in range(120):
            st = (await call(ws, "snapshot"))["status"]
            if st["asr"]["state"] == "ready" and st["llm"]["state"] == "ready":
                break
            await asyncio.sleep(1)
        print("engine:", st["asr"].get("model"), st["asr"].get("device"), "|", st["llm"].get("model"), st["llm"]["state"])
        await call(ws, "settings.set", patch={"consent_acknowledged": True, "screen_enabled": True, "mic_enabled": False,
                                              "screen_source": {"kind": "window", "hwnd": hwnd, "title": "Worksheet - Exercise 4B"}})
        capture_overlay("0_ready")
        await call(ws, "session.start", subject_id="maths")
        await asyncio.sleep(4)  # first screen read
        obs = [e for e in EVENTS if e["type"] == "screen.observation"]
        stats = [e["status"] for e in EVENTS if e["type"] == "status"]
        print("screen items seen:", obs[-1]["items"] if obs else "NONE")
        print("audio:", stats[-1]["audio"] if stats else None, "| screen:", stats[-1]["screen"] if stats else None)

        print("\n> playing: 'Right, can you solve question three for me?'")
        t0 = time.time()
        t_end = play("screen_question")
        a = await wait_answer(t0)
        finals = [e["text"] for e in EVENTS if e["type"] == "transcript.final" and e["ts"] >= t0]
        print("   heard via loopback:", finals)
        if a:
            print(f"   answer: {a['say']!r} result={a['result']!r} verified={a['verified']} sources={a['sources']}")
            done = next((e["ts"] for e in EVENTS if e["type"] == "answer.update" and e["answer"]["id"] == a["id"] and e["answer"]["why"]), None)
            if done:
                print(f"   speech end -> spoken answer ready: {done - t_end:.2f}s")
        await asyncio.sleep(1.5)
        capture_overlay("1_screen_question")

        print("\n> playing: 'Sanad, why does that happen?' (vague)")
        await call(ws, "session.subject", subject_id="biology")
        t0 = time.time()
        play("vague")
        a = await wait_answer(t0)
        print(f"   answer: conf={a and a['conf']} unsure={a and a['unsure']!r} directed={a and bool(a['directed'])}")
        await asyncio.sleep(1.8)
        capture_overlay("2_vague_directed")
        t1 = time.time()
        await call(ws, "context", text="natural selection")
        b = await wait_answer(t1)
        print(f"   with context: {b and b['say']!r} conf={b and b['conf']}")
        capture_overlay("3_after_context")

        print("\n> playing task instructions")
        await call(ws, "session.subject", subject_id="maths")
        t0 = time.time()
        play("task")
        for _ in range(60):
            tasks = [e for e in EVENTS if e["type"] == "task.update" and e.get("task") and e["ts"] >= t0]
            if tasks:
                break
            await asyncio.sleep(0.25)
        await asyncio.sleep(3)
        tasks = [e for e in EVENTS if e["type"] == "task.update" and e.get("task") and e["ts"] >= t0]
        t = tasks[-1]["task"] if tasks else {}
        print(f"   task: {t.get('description')!r} source={t.get('source')} q={t.get('questions')} minutes={t.get('time_minutes')}")
        capture_overlay("4_task")

        t0 = time.time()
        await call(ws, "explain")
        await wait_answer(t0)
        capture_overlay("5_explain")

        await call(ws, "session.stop")
        for _ in range(60):
            if any(e["type"] == "session.finalised" for e in EVENTS):
                break
            await asyncio.sleep(0.5)
        notes = await call(ws, "notes.get", session_id=None)
        print("\nnotes markdown (first 600 chars):\n", (notes or {}).get("markdown", "")[:600])
        capture_overlay("6_after_lesson")
        await call(ws, "settings.set", patch={"screen_enabled": False})


if __name__ == "__main__":
    asyncio.run(main())
