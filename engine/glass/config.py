"""Persistent engine settings and paths."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

APP_DIR = Path(os.environ.get("GLASS_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "GLASS")
APP_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = APP_DIR / "settings.json"
DB_PATH = APP_DIR / "glass.db"
# models are shared across data dirs (tests use a temp data dir but must not re-download 1.6 GB)
MODELS_DIR = Path(os.environ.get("GLASS_MODELS_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "GLASS" / "models")
MODELS_DIR.mkdir(parents=True, exist_ok=True)

SUBJECTS = [
    {"id": "maths", "name": "Mathematics", "board": "Edexcel International GCSE"},
    {"id": "biology", "name": "Biology", "board": "OxfordAQA International GCSE"},
    {"id": "chemistry", "name": "Chemistry", "board": "OxfordAQA International GCSE"},
    {"id": "physics", "name": "Physics", "board": "OxfordAQA International GCSE"},
    {"id": "english_lang", "name": "English Language", "board": "Cambridge IGCSE"},
    {"id": "english_lit", "name": "English Literature", "board": "Cambridge IGCSE"},
    {"id": "business", "name": "Business Studies", "board": "IGCSE"},
    {"id": "cs", "name": "Computer Science", "board": "IGCSE"},
    {"id": "dt", "name": "Design & Technology", "board": "Cambridge IGCSE"},
    {"id": "arabic", "name": "Arabic (Second Language)", "board": ""},
]

# Performance profiles. Every value here is a starting point; the Settings
# screen shows measured latency so the student can pick what their machine sustains.
PROFILES: dict[str, dict[str, Any]] = {
    # Least sustained GPU/CPU work: the right default for thin laptops or weak cooling.
    "cool": {
        "asr_model": "large-v3-turbo",  # GPU Whisper is far more energy-efficient than CPU Whisper
        "asr_compute": "float16",
        "asr_beam": 1,
        "partial_every": 1.6,  # live captions / early name alert, over the last 8 s only
        "llm_model": "qwen3:4b-instruct",
        "verify_answers": False,
        "vision_auto": False,
        "screen_interval": 2.5,
        "context_chars": 3000,
        "notes_interval": 300,
        "state_chars": 700,
    },
    "fast": {
        "asr_model": "small",
        "asr_compute": "float16",
        "asr_beam": 1,
        "llm_model": "qwen3:4b-instruct",
        "verify_answers": False,
        "vision_auto": False,
        "screen_interval": 2.5,
        "context_chars": 2500,
    },
    "balanced": {
        "asr_model": "large-v3-turbo",
        "asr_compute": "float16",
        "asr_beam": 2,
        "llm_model": "qwen3:4b-instruct",
        "verify_answers": True,
        "vision_auto": False,
        "screen_interval": 1.5,
        "context_chars": 4000,
    },
    "quality": {
        "asr_model": "large-v3-turbo",
        "asr_compute": "float16",
        "asr_beam": 4,
        # one vision-language model for everything: diagrams on screen are read
        # automatically without swapping models in and out of an 8 GB GPU
        "llm_model": "qwen3-vl:4b-instruct",
        "verify_answers": True,
        "vision_auto": True,
        "screen_interval": 1.0,
        "context_chars": 6000,
    },
}

DEFAULTS: dict[str, Any] = {
    "student_name": "Sanad",
    "name_aliases": ["Sanad", "Sannad", "Sanaad", "Senad", "Sanadh"],
    "profile": "cool",
    "overrides": {},  # per-key overrides of the active profile
    "language": "auto",  # whisper language: auto | en | ar
    "audio_device": None,  # loopback device name; None = follow default output
    "mic_enabled": False,
    "mic_device": None,
    "screen_enabled": False,
    "screen_source": None,  # {"kind": "window"|"monitor", "hwnd"?, "title"?, "monitor"?}
    "screen_crop": None,  # [x, y, w, h] relative 0..1 inside the source
    "exclusion_zones": [],  # list of [x, y, w, h] relative rects to blank out
    "ocr_engine": "windows",  # windows | tesseract
    "vision_model": "qwen3-vl:4b-instruct",
    "ollama_url": "http://127.0.0.1:11434",
    "store_raw_media": False,
    "store_transcript": True,
    "retention_days": 0,  # 0 = keep forever
    "default_answer_length": "quick",
    "auto_subject": True,
    "subjects": SUBJECTS,
    "consent_acknowledged": False,
}


class Settings:
    def __init__(self, path: Path = SETTINGS_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._data = copy.deepcopy(DEFAULTS)
        self.runtime: dict[str, Any] = {}  # non-persisted overrides
        if path.exists():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                self._data.update({k: v for k, v in stored.items() if k in DEFAULTS})
            except Exception:
                pass

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def all(self) -> dict[str, Any]:
        with self._lock:
            out = copy.deepcopy(self._data)
        out["effective"] = self.profile()
        out["profiles"] = PROFILES
        return out

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for k, v in patch.items():
                if k in DEFAULTS:
                    self._data[k] = v
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        return self.all()

    def profile(self) -> dict[str, Any]:
        with self._lock:
            base = {"partial_every": 0.7, "notes_interval": 150, "state_chars": 350}
            base.update(copy.deepcopy(PROFILES.get(self._data["profile"], PROFILES["balanced"])))
            base.update(self._data.get("overrides") or {})
            base.update(self.runtime)  # temporary, e.g. the thermal guard backing off
            return base

    def subject_by_id(self, sid: str | None) -> dict[str, Any] | None:
        for s in self.get("subjects"):
            if s["id"] == sid:
                return s
        return None
