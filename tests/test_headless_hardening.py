import json
import logging
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

import ada
from ada_adapters import GroqChainReasoningAdapter, OllamaReasoningAdapter
from ada_core import CapabilityKind, ExtensionManifest, ExtensionRegistry, SideEffectProfile
from ada_core.errors import redact_secrets
from ada_core.registry import ExtensionState
import memory


def completion(text):
    return SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop"),
    ])


def groq_client(*results):
    create = Mock(side_effect=results)
    return SimpleNamespace(
        base_url=ada.GROQ_BASE_URL, chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )


def quota_error():
    response = httpx.Response(429, request=httpx.Request("POST", ada.GROQ_BASE_URL))
    return ada.openai.RateLimitError("Synthetic quota exhaustion", response=response, body=None)


class HeadlessHardeningTests(unittest.TestCase):
    def test_system_prompt_is_relative_to_module_not_working_directory(self):
        expected = Path(ada.__file__).resolve().with_name("system_prompt.txt").read_text(
            encoding="utf-8"
        ).strip()
        with TemporaryDirectory() as directory, patch.object(ada, "_CACHED_SYSTEM_PROMPT", None):
            old_cwd = os.getcwd()
            try:
                os.chdir(directory)
                self.assertEqual(ada.get_system_prompt(reload=True, memory_context=""), expected + "\n\n")
            finally:
                os.chdir(old_cwd)

    def test_headless_session_runs_chat_and_memory_without_terminal_or_audio(self):
        first, second = ada.DEFAULT_GROQ_MODELS
        client = groq_client(
            quota_error(), completion("Hola."),
            completion('[{"category":"perfil","key":"color_favorito","value":"azul"}]'),
            completion('["color_favorito"]'),
        )
        with (
            TemporaryDirectory() as directory,
            patch.object(memory, "DB_PATH", Path(directory) / "memory.sqlite"),
            patch.dict(os.environ, {"ADA_GROQ_MODELS": f"{first},{second}"}),
            patch.object(ada, "get_client", return_value=(client, first)),
            patch.object(ada, "VoiceEngine") as voice,
            patch.object(ada.console, "print") as print_,
            patch.object(ada.console, "status") as status,
        ):
            session = ada.create_session(headless=True)
            self.addCleanup(session.shutdown)
            diagnostics = {row.extension_id: row.state for row in session.registry.diagnostics()}
            self.assertEqual(diagnostics["tts.edge_with_local_fallback"], ExtensionState.DISABLED)
            self.assertEqual(session.speak("No debe sonar.").kind, "error")
            chat = session.handle_text("Hola")
            self.assertEqual((chat.kind, chat.text, session.model), ("chat", "Hola.", second))
            saved = session.handle_text("Anota que mi color favorito es azul")
            self.assertEqual((saved.kind, saved.text), ("memory", "Información guardada."))
            self.assertEqual(memory.get_all_memories(), [("perfil", "color_favorito", "azul")])
            deleted = session.handle_text("Borra mi color favorito")
            self.assertEqual(
                (deleted.kind, deleted.text), ("memory", "Información eliminada de mi base de datos."),
            )
            self.assertEqual(memory.get_all_memories(), [])
            self.assertEqual(
                [call.kwargs["model"] for call in client.chat.completions.create.call_args_list],
                [first, second, second, second],
            )
            for effect in (voice, print_, status):
                effect.assert_not_called()

    def test_memory_callbacks_receive_plain_text_and_replace_terminal_output(self):
        store = Mock()
        store.save_memories.return_value = [{"key": "edad", "value": "30"}]
        statuses, events = [], []
        with patch.object(ada.console, "print") as print_, patch.object(ada.console, "status") as status:
            result = ada.execute_memory_command(
                "Anota mi edad", store=store,
                complete=lambda *_: '[{"key":"edad","value":"30"}]',
                on_status=statuses.append, on_event=events.append,
            )
        self.assertEqual(result, "Información guardada.")
        self.assertEqual(statuses, ["Procesando orden de guardado..."])
        self.assertEqual(events, ["🧠 Anotado: edad -> 30"])
        print_.assert_not_called()
        status.assert_not_called()

    def test_headless_navigation_requires_disclosure_before_cloud_fallback(self):
        url = "https://example.com/"
        payload = {"url": url, "title": "Example", "language": "en", "text": "Example page."}
        navigation = ExtensionManifest("navigation.fixture", CapabilityKind.NAVIGATION,
                                       SideEffectProfile.PURE_READ)

        class Page:
            def prepare_navigation(self, _request):
                return payload

        first, second = ada.DEFAULT_GROQ_MODELS
        with (
            patch.dict(os.environ, {"GROQ_API_KEY": "test-groq-key", "ADA_GROQ_MODELS": f"{first},{second}"}),
            patch.object(ada.console, "print") as print_,
            patch.object(ada.console, "status") as status,
        ):
            for disclose in (False, True):
                with self.subTest(disclose=disclose):
                    events = []
                    local = groq_client(quota_error())
                    local.base_url = ada.DEFAULT_LOCAL_URL
                    cloud = groq_client(quota_error(), completion('{"answer":"Resumen."}'))
                    registry = ExtensionRegistry()
                    registry.register(navigation, Page)
                    registry.register(OllamaReasoningAdapter.manifest, lambda: OllamaReasoningAdapter(
                        client=local, model=ada.DEFAULT_LOCAL_MODEL,
                    ))
                    registry.register(GroqChainReasoningAdapter.manifest, lambda: GroqChainReasoningAdapter(
                        client=cloud, model=first,
                    ))

                    def notice(message):
                        cloud.chat.completions.create.assert_not_called()
                        events.append(message)

                    session = ada.create_session("local", registry, headless=True,
                                                 navigation_notice=notice if disclose else None)
                    self.addCleanup(session.shutdown)
                    result = session.handle_text(f"/navegar {url} resume la pagina")
                    if disclose:
                        self.assertEqual((result.kind, result.text, result.url), ("navigation", "Resumen.", url))
                        self.assertEqual((len(events), result.notices), (1, tuple(events)))
                        self.assertIn("reasoning.groq", events[0])
                        self.assertEqual(cloud.chat.completions.create.call_count, 2)
                    else:
                        self.assertEqual((result.kind, result.notices), ("error", ()))
                        cloud.chat.completions.create.assert_not_called()
        print_.assert_not_called()
        status.assert_not_called()

    def test_quota_notice_callback_replaces_cli_rendering(self):
        first, second = ada.DEFAULT_GROQ_MODELS
        notice = f"Cuota agotada en {first}; cambiando a modelo de respaldo: {second}"
        with (
            patch.dict(os.environ, {"ADA_GROQ_MODELS": f"{first},{second}"}),
            patch.object(ada.console, "print") as print_,
        ):
            notices = []
            for kwargs in ({}, {"on_notice": notices.append}):
                reply = completion("Hola.")
                client = groq_client(quota_error(), reply)
                self.assertEqual(ada.create_chat_completion_with_fallback(
                    client, first, [], temperature=0.0, max_tokens=20, **kwargs,
                ), (reply, second))
        self.assertEqual(notices, [notice])
        print_.assert_called_once()
        self.assertEqual(print_.call_args.args[0].plain, notice)

    def test_server_credentials_are_redacted(self):
        values = {"ADA_TOKEN": "test-ada-secret", "OPENCODE_SERVER_PASSWORD": "test-server-secret"}
        with patch.dict(os.environ, values):
            self.assertEqual(
                redact_secrets("Token=test-ada-secret Password=test-server-secret"),
                "Token=[REDACTED] Password=[REDACTED]",
            )

    def test_sqlite_connection_enables_wal_and_busy_timeout(self):
        with (
            TemporaryDirectory() as directory,
            patch.object(memory, "DB_PATH", Path(directory) / "memory.sqlite"),
            patch.object(memory.sqlite3, "connect", wraps=memory.sqlite3.connect) as connect,
        ):
            with memory.get_db_connection() as connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone(), ("wal",))
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone(), (5000,))
            self.assertEqual(connect.call_args.kwargs["timeout"], 5.0)

    def test_sqlite_connection_keeps_readonly_rollback_database_readable(self):
        connect = sqlite3.connect
        with TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite"
            with connect(path, factory=memory._ClosingConnection) as seed:
                seed.execute("PRAGMA journal_mode=DELETE").close()
                seed.execute("CREATE TABLE fixture (value TEXT)")
                seed.execute("INSERT INTO fixture VALUES ('synthetic')")

            with (
                patch.object(memory, "DB_PATH", f"{path.as_uri()}?mode=ro"),
                patch.object(sqlite3, "connect", side_effect=lambda *args, **kwargs: connect(
                    *args, uri=True, **kwargs,
                )),
                memory.get_db_connection() as connection,
            ):
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone(), ("delete",))
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone(), (5000,))
                self.assertEqual(connection.execute("SELECT value FROM fixture").fetchone(),
                                 ("synthetic",))

    def test_sqlite_connection_closes_and_propagates_other_wal_errors(self):
        connection = Mock()
        error = sqlite3.OperationalError("database is locked")
        connection.execute.side_effect = [Mock(), error]
        with patch.object(sqlite3, "connect", return_value=connection):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                memory.get_db_connection()
        self.assertIs(caught.exception, error)
        connection.close.assert_called_once_with()

    def test_rotating_file_logger_redacts_messages_and_arguments(self):
        from ada_adapters.file_logging import configure_redacted_file_logger

        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"GROQ_API_KEY": "test-groq-secret", "ADA_TOKEN": "test-ada-secret"}),
        ):
            path = Path(directory) / "ada.log"
            logger = configure_redacted_file_logger(path, max_bytes=150, backup_count=2)
            try:
                self.assertIsInstance(logger, logging.Logger)
                for _ in range(8):
                    logger.info("test-groq-secret %(token)s", {"token": "test-ada-secret"})
            finally:
                for handler in logger.handlers[:]:
                    logger.removeHandler(handler)
                    handler.close()
            self.assertEqual({file.name for file in path.parent.glob("ada.log*")},
                             {"ada.log", "ada.log.1", "ada.log.2"})
            for file in path.parent.glob("ada.log*"):
                body = file.read_text(encoding="utf-8")
                self.assertIn("[REDACTED] [REDACTED]", body)
                self.assertNotIn("test-groq-secret", body)
                self.assertNotIn("test-ada-secret", body)

    def test_file_logger_redacts_escaped_password_in_arguments_and_traceback(self):
        from ada_adapters.file_logging import configure_redacted_file_logger

        password = 'fixture\\password"'
        with TemporaryDirectory() as directory, patch.dict(
            os.environ, {"OPENCODE_SERVER_PASSWORD": password}
        ):
            path = Path(directory) / "ada.log"
            logger = configure_redacted_file_logger(path)
            try:
                logger.info("password=%r", password)
                logger.info("payload=%s", json.dumps({"password": password}))
                try:
                    raise KeyError(password)
                except KeyError:
                    logger.exception("lookup failed")
            finally:
                logger.handlers[0].close()
            text = path.read_text(encoding="utf-8")
            self.assertIn("password='[REDACTED]'", text)
            self.assertIn('payload={"password": "[REDACTED]"}', text)
            self.assertIn("KeyError: '[REDACTED]'", text)
            self.assertNotIn(password, text)
