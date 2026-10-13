"""SQLite persistence: sessions, transcript text, questions, notes, tasks,
formulas, flashcards, with FTS5 search over notes. Raw audio and screenshots
are never stored here."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, subject_id TEXT, title TEXT, topic TEXT, started_at REAL, ended_at REAL, summary TEXT
);
CREATE TABLE IF NOT EXISTS segments(
  id TEXT PRIMARY KEY, session_id TEXT, source TEXT, text TEXT, t0 REAL, t1 REAL
);
CREATE INDEX IF NOT EXISTS seg_session ON segments(session_id, t0);
CREATE TABLE IF NOT EXISTS questions(
  id TEXT PRIMARY KEY, session_id TEXT, t REAL, question TEXT, kind TEXT, directed REAL, answer TEXT, teacher_answer TEXT
);
CREATE TABLE IF NOT EXISTS notes(
  session_id TEXT PRIMARY KEY, data TEXT, markdown TEXT, updated_at REAL, edited INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, session_id TEXT, subject_id TEXT, data TEXT, status TEXT, created_at REAL, completed_at REAL
);
CREATE TABLE IF NOT EXISTS formulas(
  id TEXT PRIMARY KEY, subject_id TEXT, session_id TEXT, text TEXT, latex TEXT, meaning TEXT, pinned INTEGER DEFAULT 0, created_at REAL
);
CREATE TABLE IF NOT EXISTS flashcards(
  id TEXT PRIMARY KEY, session_id TEXT, subject_id TEXT, front TEXT, back TEXT, created_at REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(session_id UNINDEXED, subject_id UNINDEXED, title, content);
"""


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


class DB:
    def __init__(self, path: Path):
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    # -- sessions -------------------------------------------------------------
    def create_session(self, subject_id: str | None, title: str) -> str:
        sid = new_id("s_")
        self._exec("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)", (sid, subject_id, title, "", time.time(), None, ""))
        return sid

    def update_session(self, sid: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid))

    def get_session(self, sid: str) -> dict | None:
        rows = self._all("SELECT * FROM sessions WHERE id=?", (sid,))
        return rows[0] if rows else None

    def list_sessions(self, subject_id: str | None = None, limit: int = 200) -> list[dict]:
        if subject_id:
            return self._all("SELECT * FROM sessions WHERE subject_id=? ORDER BY started_at DESC LIMIT ?", (subject_id, limit))
        return self._all("SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,))

    def delete_session(self, sid: str) -> None:
        for table in ("segments", "questions", "notes", "tasks", "formulas", "flashcards"):
            self._exec(f"DELETE FROM {table} WHERE session_id=?", (sid,))
        self._exec("DELETE FROM notes_fts WHERE session_id=?", (sid,))
        self._exec("DELETE FROM sessions WHERE id=?", (sid,))

    def purge_older_than(self, days: int) -> int:
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        old = self._all("SELECT id FROM sessions WHERE started_at < ?", (cutoff,))
        for r in old:
            self.delete_session(r["id"])
        return len(old)

    # -- transcript -----------------------------------------------------------
    def add_segment(self, sid: str, seg_id: str, source: str, text: str, t0: float, t1: float) -> None:
        self._exec("INSERT OR REPLACE INTO segments VALUES(?,?,?,?,?,?)", (seg_id, sid, source, text, t0, t1))

    def segments(self, sid: str) -> list[dict]:
        return self._all("SELECT * FROM segments WHERE session_id=? ORDER BY t0", (sid,))

    # -- questions ------------------------------------------------------------
    def add_question(self, sid: str, qid: str, t: float, question: str, kind: str, directed: float, answer: dict) -> None:
        self._exec("INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?)",
                   (qid, sid, t, question, kind, directed, json.dumps(answer), None))

    def set_teacher_answer(self, qid: str, text: str) -> None:
        self._exec("UPDATE questions SET teacher_answer=? WHERE id=?", (text, qid))

    def questions(self, sid: str) -> list[dict]:
        rows = self._all("SELECT * FROM questions WHERE session_id=? ORDER BY t", (sid,))
        for r in rows:
            r["answer"] = json.loads(r["answer"] or "{}")
        return rows

    # -- notes ----------------------------------------------------------------
    def save_notes(self, sid: str, data: dict, markdown: str, subject_id: str | None, title: str, edited: bool = False) -> None:
        self._exec("INSERT OR REPLACE INTO notes VALUES(?,?,?,?,?)", (sid, json.dumps(data), markdown, time.time(), int(edited)))
        self._exec("DELETE FROM notes_fts WHERE session_id=?", (sid,))
        self._exec("INSERT INTO notes_fts VALUES(?,?,?,?)", (sid, subject_id or "", title, markdown))

    def get_notes(self, sid: str) -> dict | None:
        rows = self._all("SELECT * FROM notes WHERE session_id=?", (sid,))
        if not rows:
            return None
        r = rows[0]
        r["data"] = json.loads(r["data"] or "{}")
        return r

    def search(self, query: str, subject_id: str | None = None, limit: int = 20) -> list[dict]:
        q = " OR ".join(f'"{w}"' for w in query.replace('"', " ").split() if len(w) > 1)
        if not q:
            return []
        sql = ("SELECT session_id, subject_id, title, snippet(notes_fts, 3, '[', ']', '…', 18) AS snippet, bm25(notes_fts) AS score "
               "FROM notes_fts WHERE notes_fts MATCH ?")
        args: tuple = (q,)
        if subject_id:
            sql += " AND subject_id=?"
            args += (subject_id,)
        sql += " ORDER BY score LIMIT ?"
        try:
            return self._all(sql, args + (limit,))
        except sqlite3.OperationalError:
            return []

    # -- tasks ----------------------------------------------------------------
    def save_task(self, task: dict) -> None:
        self._exec("INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?,?)",
                   (task["id"], task.get("session_id"), task.get("subject_id"), json.dumps(task), task.get("status", "active"),
                    task.get("created_at", time.time()), task.get("completed_at")))

    def tasks(self, sid: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT data FROM tasks WHERE 1=1", ()
        if sid:
            sql += " AND session_id=?"
            args += (sid,)
        if status:
            sql += " AND status=?"
            args += (status,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        return [json.loads(r["data"]) for r in self._all(sql, args + (limit,))]

    # -- formulas / flashcards -----------------------------------------------------
    def add_formula(self, subject_id: str | None, sid: str | None, text: str, latex: str, meaning: str) -> str | None:
        exists = self._all("SELECT id FROM formulas WHERE subject_id IS ? AND replace(text,' ','')=?",
                           (subject_id, text.replace(" ", "")))
        if exists:
            return None
        fid = new_id("f_")
        self._exec("INSERT INTO formulas VALUES(?,?,?,?,?,?,?,?)", (fid, subject_id, sid, text, latex, meaning, 0, time.time()))
        return fid

    def formulas(self, subject_id: str | None = None, sid: str | None = None) -> list[dict]:
        if sid:
            return self._all("SELECT * FROM formulas WHERE session_id=? ORDER BY pinned DESC, created_at", (sid,))
        if subject_id:
            return self._all("SELECT * FROM formulas WHERE subject_id=? ORDER BY pinned DESC, created_at", (subject_id,))
        return self._all("SELECT * FROM formulas ORDER BY pinned DESC, created_at DESC LIMIT 200")

    def pin_formula(self, fid: str, pinned: bool) -> None:
        self._exec("UPDATE formulas SET pinned=? WHERE id=?", (int(pinned), fid))

    def add_flashcards(self, sid: str, subject_id: str | None, cards: list[dict]) -> list[dict]:
        out = []
        for c in cards:
            fid = new_id("c_")
            self._exec("INSERT INTO flashcards VALUES(?,?,?,?,?,?)", (fid, sid, subject_id, c["front"], c["back"], time.time()))
            out.append({"id": fid, **c})
        return out

    def flashcards(self, sid: str | None = None, subject_id: str | None = None) -> list[dict]:
        if sid:
            return self._all("SELECT * FROM flashcards WHERE session_id=? ORDER BY created_at", (sid,))
        if subject_id:
            return self._all("SELECT * FROM flashcards WHERE subject_id=? ORDER BY created_at DESC", (subject_id,))
        return self._all("SELECT * FROM flashcards ORDER BY created_at DESC LIMIT 300")

    def subject_stats(self) -> list[dict]:
        return self._all(
            "SELECT subject_id, COUNT(*) AS lessons, MAX(started_at) AS last, GROUP_CONCAT(topic, '||') AS topics "
            "FROM sessions GROUP BY subject_id")
