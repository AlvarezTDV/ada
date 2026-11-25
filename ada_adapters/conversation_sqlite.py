"""SQLite conversation snapshots, independent of the assistant memory database."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from ada_core.domain import ChatMessage, SessionMode, SessionSummary


class SqliteConversationStore:
    def __init__(self, db_path: str | Path = "ada_state.db"):
        self._path = Path(db_path).absolute()
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT NOT NULL PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (session_id, seq)
                );
            """)

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA foreign_keys=ON")
            yield db

    @staticmethod
    def _insert_messages(db, session_id, messages, timestamp):
        db.executemany(
            "INSERT INTO session_messages (session_id, seq, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            ((session_id, seq, message.role, message.content, timestamp) for seq, message in enumerate(messages)),
        )

    def create_session(self, summary: SessionSummary, messages: tuple[ChatMessage, ...]) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO sessions (session_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                       (summary.session_id, summary.title, summary.created_at, summary.updated_at))
            self._insert_messages(db, summary.session_id, messages, summary.created_at)

    def list_sessions(self) -> tuple[SessionSummary, ...]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT s.session_id, s.title, s.created_at, s.updated_at, COUNT(m.id)
                FROM sessions s LEFT JOIN session_messages m ON m.session_id = s.session_id
                GROUP BY s.session_id ORDER BY s.created_at, s.session_id
            """)
            return tuple(SessionSummary(session_id, SessionMode.PERSISTENT, *details)
                         for session_id, *details in rows)

    def load_messages(self, session_id: str) -> tuple[ChatMessage, ...] | None:
        with self._connect() as db:
            rows = db.execute("""
                SELECT m.role, m.content FROM sessions s
                LEFT JOIN session_messages m ON m.session_id = s.session_id
                WHERE s.session_id = ? ORDER BY m.seq
            """, (session_id,)).fetchall()
            if not rows:
                return None
            return tuple(ChatMessage(role, content) for role, content in rows if role is not None)

    def save_messages(self, session_id: str, messages: tuple[ChatMessage, ...], updated_at: str) -> None:
        with self._connect() as db:
            if not db.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                              (updated_at, session_id)).rowcount:
                raise KeyError(session_id)
            db.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
            self._insert_messages(db, session_id, messages, updated_at)

    def delete_session(self, session_id: str) -> bool:
        with self._connect() as db:
            return bool(db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,)).rowcount)
