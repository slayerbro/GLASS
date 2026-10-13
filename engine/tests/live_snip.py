"""Snip & Solve against the running app: OCR path (worksheet) and vision path (diagram)."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import websockets

HERE = Path(__file__).parent
TOKEN = os.environ.get("GLASS_DEV_TOKEN", "devtoken")


def show(images: list[str]) -> list[tuple[int, int, int, int]]:
    """One topmost Tk window showing the images stacked; returns each image's screen rect."""
    import tkinter as tk

    import win32gui
    from PIL import Image, ImageTk

    box, ready = {}, threading.Event()

    def run():
        root = tk.Tk()
        root.title("Snip test")
        root.attributes("-topmost", True)
        keep, labels = [], []
        for name in images:
            img = ImageTk.PhotoImage(Image.open(HERE / "fixtures" / "screens" / name), master=root)
            keep.append(img)
            lbl = tk.Label(root, image=img, borderwidth=0)
            lbl.pack()
            labels.append(lbl)
        root.geometry("+60+20")
        root.update()
        rects = []
        for lbl in labels:
            l, t, r, b = win32gui.GetWindowRect(lbl.winfo_id())
            rects.append((l, t, r - l, b - t))
        box["rects"] = rects
        ready.set()
        root.after(90000, root.destroy)
        root.mainloop()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(10)
    return box["rects"]


async def snip(ws, rect, vision: bool, label: str):
    x, y, w, h = rect
    t0 = time.time()
    await ws.send(json.dumps({"cmd": "snip", "rid": 1, "x": x, "y": y, "w": w, "h": h, "vision": vision}))
    final = None
    while time.time() - t0 < 120:
        e = json.loads(await asyncio.wait_for(ws.recv(), 120))
        if e.get("type") == "answer.update" and e["answer"]["mode"] == "snip":
            final = e["answer"]
            if final["status"] in ("done", "error"):
                break
    print(f"\n== {label} ({time.time() - t0:.1f}s, latency {final and final['latency']})")
    print("read:", (final or {}).get("ocr", "")[:300].replace("\n", " | "))
    print("verified:", final and final["result"])
    print("answer:\n", (final or {}).get("body", "")[:900])


async def main():
    ws_rect, tri_rect = show(["worksheet.png", "triangle.png"])
    await asyncio.sleep(1.5)
    async with websockets.connect(f"ws://127.0.0.1:47821/?token={TOKEN}", max_size=2**23) as ws:
        await ws.recv()  # hello
        # top part of the worksheet: questions 1-3
        x, y, w, h = ws_rect
        await snip(ws, (x, y, w, int(h * 0.62)), False, "Worksheet snip (OCR)")
        await snip(ws, tri_rect, True, "Diagram snip (vision model)")


if __name__ == "__main__":
    asyncio.run(main())
