"""Entry point: python -m glass --port 47821 (token via GLASS_TOKEN env).

Lifecycle: the Electron parent asks for a graceful stop with the authenticated
`shutdown` command; if the parent dies instead, the PID watchdog below stops the
engine. (Do NOT block a thread reading stdin here: on Windows a pending read on
the inherited stdin pipe stalls start-up of every child process we spawn — the
SymPy worker, Tesseract — until the read returns.)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
import time

from .config import APP_DIR


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("GLASS_PORT", 47821)))
    ap.add_argument("--log", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log.upper(), logging.INFO),
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(APP_DIR / "engine.log", encoding="utf-8")],
    )
    for noisy in ("httpx", "httpx2", "httpcore", "websockets", "faster_whisper", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    token = os.environ.get("GLASS_TOKEN", "")
    if not token:
        print("GLASS_TOKEN not set; refusing to start an unauthenticated server", file=sys.stderr)
        sys.exit(2)

    from .engine import Engine
    from .server import Server

    async def run() -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        engine = Engine()
        await engine.start()
        server = Server(engine, args.port, token, on_shutdown=stop.set)

        parent = int(os.environ.get("GLASS_PARENT_PID") or 0)
        if parent:
            import psutil

            def watch_parent() -> None:
                while psutil.pid_exists(parent):
                    time.sleep(2)
                logging.getLogger("glass").info("parent process %d is gone; shutting down", parent)
                loop.call_soon_threadsafe(stop.set)

            threading.Thread(target=watch_parent, daemon=True, name="parent-watch").start()
        try:
            loop.add_signal_handler(signal.SIGTERM, stop.set)
        except NotImplementedError:
            pass
        srv_task = asyncio.create_task(server.serve_forever())
        await stop.wait()
        srv_task.cancel()
        await engine.shutdown()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
