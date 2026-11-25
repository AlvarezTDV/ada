import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from ada_adapters.conversation_sqlite import SqliteConversationStore
from ada_core import ChatMessage, ConversationStorePort, SessionManager, SessionMode, SessionResult


class FakeSession:
    def __init__(self):
        self.messages = [ChatMessage("system", "seed")]
        self.contexts = []
        self.shutdowns = 0

    def handle_text(self, text, *, on_reasoning_notice=None):
        if text == "salir":
            return SessionResult("Bye", kind="exit")
        self.contexts.append(tuple(self.messages))
        if on_reasoning_notice:
            on_reasoning_notice(text)
        self.messages.extend([ChatMessage("user", text), ChatMessage("assistant", f"Echo: {text}")])
        return SessionResult(text, kind="error" if text == "fail" else "chat")

    def shutdown(self):
        self.shutdowns += 1


class SessionManagerTests(unittest.TestCase):
    def setUp(self):
        scratch = self.enterContext(TemporaryDirectory())
        self.db = Path(scratch) / "ada_state.db"
        self.store = SqliteConversationStore(self.db)
        self.instances = []
        self.manager = SessionManager(self.factory, self.store)

    def factory(self):
        session = FakeSession()
        self.instances.append(session)
        return session

    def persistent(self):
        return self.manager.create_session(SessionMode.PERSISTENT, "Saved")

    def test_ephemeral_history_stays_in_one_manager(self):
        manager = SessionManager(self.factory)
        session = manager.create_session()
        self.assertEqual(session.mode, SessionMode.EPHEMERAL)
        manager.handle_text(session.session_id, "one")
        expected = tuple(self.instances[0].messages)
        manager.handle_text(session.session_id, "two")
        self.assertEqual(self.instances[0].contexts[-1], expected)
        self.assertEqual(manager.list_sessions()[0].message_count, 5)
        self.assertEqual(SessionManager(self.factory).list_sessions(), ())

    def test_persistent_restart_restores_exact_history_and_summary(self):
        self.assertIsInstance(self.store, ConversationStorePort)
        session = self.persistent()
        self.manager.handle_text(session.session_id, "first\nturn")
        previous = self.instances[-1]
        expected = tuple(previous.messages)
        self.manager.shutdown_all()
        self.assertEqual(previous.shutdowns, 1)
        reopened = SessionManager(self.factory, SqliteConversationStore(self.db))
        summary, = reopened.list_sessions()
        self.assertEqual((summary.session_id, summary.mode, summary.title, summary.created_at,
                          summary.message_count),
                         (session.session_id, SessionMode.PERSISTENT, "Saved", session.created_at, 3))
        reopened.handle_text(session.session_id, "next")
        self.assertEqual(self.instances[-1].contexts, [expected])

    def test_same_session_turns_are_serialized(self):
        session_id = self.persistent().session_id
        first_inside, second_started, second_inside, release = (Event() for _ in range(4))

        def hold_first(_):
            first_inside.set()
            if not release.wait(5):
                raise TimeoutError("The test did not release the first turn")

        def second():
            second_started.set()
            return self.manager.handle_text(session_id, "two", on_reasoning_notice=lambda _: second_inside.set())

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.manager.handle_text, session_id, "one", on_reasoning_notice=hold_first)
            try:
                self.assertTrue(first_inside.wait(5))
                following = pool.submit(second)
                self.assertTrue(second_started.wait(5))
                self.assertFalse(second_inside.wait(0.15))
            finally:
                release.set()
            self.assertEqual((first.result(5).text, following.result(5).text), ("one", "two"))
        self.assertEqual(self.instances[0].contexts[-1][-1], ChatMessage("assistant", "Echo: one"))
        self.assertEqual(tuple(self.store.load_messages(session_id)), tuple(self.instances[0].messages))

    def test_exit_closes_only_the_selected_session(self):
        ephemeral = self.manager.create_session()
        closing_session = self.instances[-1]
        other = self.manager.create_session()
        active_session = self.instances[-1]
        saved = self.persistent()
        self.manager.handle_text(saved.session_id, "keep")
        persistent_session = self.instances[-1]
        history = self.store.load_messages(saved.session_id)
        self.assertEqual(self.manager.handle_text(ephemeral.session_id, "salir").kind, "exit")
        self.assertEqual(closing_session.shutdowns, 1)
        self.assertEqual(self.manager.handle_text(other.session_id, "still here").kind, "chat")
        self.assertEqual(active_session.shutdowns, 0)
        self.assertEqual(self.manager.handle_text(saved.session_id, "salir").kind, "exit")
        self.assertEqual(persistent_session.shutdowns, 0)
        self.assertEqual({s.session_id for s in self.manager.list_sessions()}, {other.session_id, saved.session_id})
        self.assertEqual(self.store.load_messages(saved.session_id), history)
        self.manager.handle_text(saved.session_id, "back")
        self.assertIsNot(self.instances[-1], persistent_session)
        self.assertEqual(self.instances[-1].contexts, [history])

    def test_delete_removes_cache_and_cascades_to_messages(self):
        ephemeral = self.manager.create_session()
        saved = self.persistent()
        self.manager.handle_text(saved.session_id, "remove this")
        for session in (ephemeral, saved):
            self.assertTrue(self.manager.delete_session(session.session_id))
            self.assertFalse(self.manager.delete_session(session.session_id))
            with self.assertRaises(KeyError):
                self.manager.handle_text(session.session_id, "gone")
        self.assertEqual(self.manager.list_sessions(), ())
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM session_messages").fetchone(), (0,))

    def test_failed_turn_does_not_change_history_or_summary(self):
        session = self.persistent()
        history = self.store.load_messages(session.session_id)
        self.assertEqual(self.manager.handle_text(session.session_id, "fail").kind, "error")
        self.assertEqual(self.manager.list_sessions(), (session,))
        self.assertEqual(self.store.load_messages(session.session_id), history)
        self.assertEqual(tuple(self.instances[-1].messages), history)

    def test_database_failure_rolls_back_messages_and_summary(self):
        session = self.persistent()
        history = self.store.load_messages(session.session_id)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("""CREATE TRIGGER reject_turn BEFORE INSERT ON session_messages
                WHEN NEW.content = 'Echo: reject' BEGIN SELECT RAISE(ABORT, 'rejected'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "rejected"):
            self.manager.handle_text(session.session_id, "reject")
        self.assertEqual(self.manager.list_sessions(), (session,))
        self.assertEqual(self.store.load_messages(session.session_id), history)
        self.assertEqual(tuple(self.instances[-1].messages), history)

    def test_reentrant_callback_never_acknowledges_a_lost_turn(self):
        class SnapshotSession(FakeSession):
            def handle_text(self, text, *, on_reasoning_notice=None):
                turn = [*self.messages, ChatMessage("user", text)]
                if on_reasoning_notice:
                    on_reasoning_notice("fallback")
                self.messages = [*turn, ChatMessage("assistant", f"ACK {text}")]
                return SessionResult(text)

        manager = SessionManager(SnapshotSession, self.store)
        session_id = manager.create_session(SessionMode.PERSISTENT).session_id

        def reenter(_):
            with self.assertRaisesRegex(RuntimeError, "cannot be nested"):
                manager.handle_text(session_id, "inner")

        self.assertEqual(manager.handle_text(session_id, "outer", on_reasoning_notice=reenter).text, "outer")
        self.assertEqual([m.content for m in self.store.load_messages(session_id) if m.role == "user"], ["outer"])

    def test_shutdown_drains_an_already_started_persistent_hydration(self):
        session_id = self.persistent().session_id
        factory_entered, release_factory, shutdown_started = (Event() for _ in range(3))
        cold = FakeSession()

        def factory():
            factory_entered.set()
            if not release_factory.wait(5):
                raise TimeoutError("Test did not release hydration")
            return cold

        manager = SessionManager(factory, self.store)

        def shutdown():
            shutdown_started.set()
            manager.shutdown_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            turn = pool.submit(manager.handle_text, session_id, "in flight")
            try:
                self.assertTrue(factory_entered.wait(5))
                draining = pool.submit(shutdown)
                self.assertTrue(shutdown_started.wait(5))
                with self.assertRaises(TimeoutError):
                    draining.result(0.2)
            finally:
                release_factory.set()
            self.assertEqual(turn.result(5).text, "in flight")
            draining.result(5)
        self.assertEqual(cold.shutdowns, 1)
        self.assertEqual(self.store.load_messages(session_id)[-1].content, "Echo: in flight")

    def test_reader_sees_only_committed_snapshots(self):
        session = self.persistent()
        session_id = session.session_id
        old = self.store.load_messages(session_id)
        updated = (*old, ChatMessage("user", "new"))
        inserted, release = Event(), Event()

        class GatedStore(SqliteConversationStore):
            def _insert_messages(self, *args):
                super()._insert_messages(*args)
                inserted.set()
                if not release.wait(5):
                    raise TimeoutError("Test did not release commit")

        writer = GatedStore(self.db)
        with ThreadPoolExecutor(max_workers=1) as pool:
            save = pool.submit(writer.save_messages, session_id, updated, "new timestamp")
            try:
                self.assertTrue(inserted.wait(5))
                self.assertEqual(self.store.load_messages(session_id), old)
                self.assertEqual(self.store.list_sessions(), (session,))
            finally:
                release.set()
            save.result(5)
        self.assertEqual(self.store.load_messages(session_id), updated)
        self.assertEqual(self.store.list_sessions()[0].updated_at, "new timestamp")
