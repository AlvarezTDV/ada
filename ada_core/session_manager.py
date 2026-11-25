"""Manage conversation lifetimes without coupling the core to storage."""

import logging
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Condition, RLock, local
from uuid import uuid4
from weakref import WeakValueDictionary

from .domain import SessionMode, SessionSummary
from .orchestrator import SessionOrchestrator, SessionResult
from .ports import ConversationStorePort


@dataclass
class _ActiveSession:
    orchestrator: SessionOrchestrator
    ephemeral: SessionSummary | None = None


class SessionManager:
    """Own active orchestrators and serialize operations on each session."""

    def __init__(self, session_factory: Callable[[], SessionOrchestrator],
                 store: ConversationStorePort | None = None):
        self._factory = session_factory
        self._store = store
        self._guard = Condition()
        self._active: dict[str, _ActiveSession] = {}
        self._locks = WeakValueDictionary()
        self._in_flight = 0
        self._draining = False
        self._thread = local()

    @contextmanager
    def _no_reentry(self):
        if getattr(self._thread, "busy", False):
            raise RuntimeError("Session operations cannot be nested inside a session callback")
        self._thread.busy = True
        try:
            yield
        finally:
            self._thread.busy = False

    @contextmanager
    def _session_operation(self, session_id: str):
        with self._no_reentry():
            with self._guard:
                self._guard.wait_for(lambda: not self._draining)
                # Waiters keep the same per-session lock alive until they finish.
                lock = self._locks.get(session_id)
                if lock is None:
                    lock = self._locks[session_id] = RLock()
                self._in_flight += 1
            try:
                with lock:
                    yield
            finally:
                with self._guard:
                    self._in_flight -= 1
                    if not self._in_flight:
                        self._guard.notify_all()

    def create_session(self, mode: SessionMode = SessionMode.EPHEMERAL, title: str = "") -> SessionSummary:
        mode = SessionMode(mode)
        store = self._store if mode is SessionMode.PERSISTENT else None
        if mode is SessionMode.PERSISTENT and store is None:
            raise ValueError("Persistent sessions require a conversation store")
        session_id = str(uuid4())
        with self._session_operation(session_id):
            orchestrator = self._factory()
            now = datetime.now(timezone.utc).isoformat()
            summary = SessionSummary(session_id, mode, title, now, now, len(orchestrator.messages))
            if store is not None:
                store.create_session(summary, tuple(orchestrator.messages))
            with self._guard:
                self._active[session_id] = _ActiveSession(orchestrator, summary if store is None else None)
        return summary

    def list_sessions(self) -> tuple[SessionSummary, ...]:
        persistent = self._store.list_sessions() if self._store is not None else ()
        with self._guard:
            ephemeral = tuple(s.ephemeral for s in self._active.values() if s.ephemeral is not None)
        return tuple(sorted(ephemeral + persistent, key=lambda s: (s.created_at, s.session_id)))

    def _get_or_load(self, session_id: str) -> _ActiveSession:
        with self._guard:
            active = self._active.get(session_id)
        if active is not None:
            return active
        messages = self._store.load_messages(session_id) if self._store is not None else None
        if messages is None:
            raise KeyError(session_id)
        active = _ActiveSession(self._factory())
        active.orchestrator.messages = list(messages)
        with self._guard:
            self._active[session_id] = active
        return active

    def handle_text(self, session_id: str, text: str, *, on_reasoning_notice=None) -> SessionResult:
        with self._session_operation(session_id):
            active = self._get_or_load(session_id)
            orchestrator = active.orchestrator
            before = tuple(orchestrator.messages)
            try:
                result = orchestrator.handle_text(text, on_reasoning_notice=on_reasoning_notice)
                if result.kind == "exit":
                    self._evict(session_id, shutdown=active.ephemeral is not None)
                elif result.kind == "error":
                    orchestrator.messages = list(before)
                else:
                    messages = tuple(orchestrator.messages)
                    if messages != before:
                        self._save_history(session_id, active, messages)
            except Exception:
                orchestrator.messages = list(before)
                raise
            return result

    def _save_history(self, session_id, active, messages):
        now = datetime.now(timezone.utc).isoformat()
        if active.ephemeral is None:
            assert self._store is not None
            self._store.save_messages(session_id, messages, now)
        else:
            with self._guard:
                active.ephemeral = replace(active.ephemeral, message_count=len(messages), updated_at=now)

    def _evict(self, session_id: str, *, shutdown: bool = True) -> None:
        with self._guard:
            active = self._active.pop(session_id, None)
        if active is not None and shutdown:
            self._shutdown(active.orchestrator)

    @staticmethod
    def _shutdown(orchestrator: SessionOrchestrator) -> None:
        try:
            orchestrator.shutdown()
        except Exception:
            logging.getLogger(__name__).warning("Session shutdown failed after the session was removed")

    def delete_session(self, session_id: str) -> bool:
        with self._session_operation(session_id):
            with self._guard:
                active = self._active.get(session_id)
            if active is not None and active.ephemeral is not None:
                deleted = True
            else:
                deleted = self._store is not None and self._store.delete_session(session_id)
            self._evict(session_id)
            return deleted

    def shutdown_all(self) -> None:
        with self._no_reentry():
            with self._guard:
                self._guard.wait_for(lambda: not self._draining)
                self._draining = True
            try:
                with self._guard:
                    self._guard.wait_for(lambda: not self._in_flight)
                    active = tuple(self._active.values())
                    self._active.clear()
                for session in active:
                    self._shutdown(session.orchestrator)
            finally:
                with self._guard:
                    self._draining = False
                    self._guard.notify_all()
