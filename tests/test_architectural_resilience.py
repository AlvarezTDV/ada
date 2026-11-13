"""Public session integration checks with fake providers and temporary SQLite."""

import copy
import io
import os
from pathlib import Path
import sys
import threading
from unittest.mock import Mock, patch

import httpx
from rich.console import Console

import memory
from ada_core import (
    CapabilityKind, ExtensionManifest, ExtensionRegistry, ExtensionState,
    HealthReport, SideEffectProfile,
)
from ada_adapters import GroqChainReasoningAdapter, SqliteMemoryAdapter
from test_groq_chat_provider import completion, status_error
from test_memory_prompt import IsolatedPromptFixture, ada


class ArchitecturalResilienceTests(IsolatedPromptFixture):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.client = Mock(base_url=ada.GROQ_BASE_URL)
        self.client.chat.completions.create.return_value = completion("Respuesta confirmada")
        factory = patch.object(ada, "get_client", return_value=(self.client, ada.DEFAULT_MODEL))
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.output = io.StringIO()
        console = patch.object(ada, "console", Console(file=self.output, width=240))
        console.start()
        self.addCleanup(console.stop)
        self.remember("té", "preferences", "bebida")

    def session(self, registry=None, provider="groq"):
        return ada.create_session(provider=provider, registry=registry)

    def minimal_registry(self):
        registry = ExtensionRegistry()
        registry.register(GroqChainReasoningAdapter.manifest, GroqChainReasoningAdapter)
        registry.register(SqliteMemoryAdapter.manifest, SqliteMemoryAdapter)
        return registry

    def snapshot(self):
        return Path(memory.DB_PATH).read_bytes()

    def test_ada_starts_and_chats_when_optional_browser_and_voice_extensions_are_uninstalled(self):
        with patch.dict(sys.modules, {"voice": None, "browser_navigation": None,
                                      "faster_whisper": None, "browser_use": None}):
            session = self.session(self.minimal_registry())
            result = session.handle_text("hola")
            self.assertEqual((result.kind, result.text), ("chat", "Respuesta confirmada"))
            self.assertEqual(session.handle_text("/estado").kind, "status")
            self.assertEqual(session.handle_text("salir").kind, "exit")
            with patch.object(ada, "print_header"), patch.object(ada, "print_ada_response") as display, \
                 patch("builtins.input", side_effect=["hola", "salir"]):
                ada.chat_loop(registry=self.minimal_registry())
            display.assert_any_call("Respuesta confirmada", "casual")
        self.factory.assert_called()

    def test_navigation_extension_failure_or_removal_is_isolated_and_chat_continues(self):
        for failure in ("removed", "disabled", "missing", "crashed"):
            with self.subTest(failure=failure):
                registry = self.minimal_registry()
                if failure != "removed":
                    manifest = ExtensionManifest(
                        "navigation.browser_use_edge", CapabilityKind.NAVIGATION,
                        SideEffectProfile.PURE_READ,
                    )
                    adapter = Mock()
                    adapter.navigate.side_effect = (
                        ImportError("browser extension not installed") if failure == "missing"
                        else RuntimeError("browser process failed")
                    )
                    registry.register(manifest, lambda: adapter)
                    if failure == "disabled":
                        registry.disable(manifest.extension_id)
                session = self.session(registry)
                before, database = copy.deepcopy(session.messages), self.snapshot()
                result = session.handle_text("/navegar https://example.com resume")
                self.assertEqual(result.kind, "error")
                self.assertEqual(session.messages, before)
                self.assertEqual(self.snapshot(), database)
                self.assertIn("navigation", result.text)
                self.assertNotIn("Respuesta confirmada", result.text)
                self.assertEqual(session.handle_text("hola").kind, "chat")
        for capability in (CapabilityKind.STT, CapabilityKind.TTS):
            registry = self.minimal_registry()
            manifest = ExtensionManifest(f"broken.{capability.value}", capability, SideEffectProfile.PURE_READ)
            registry.register(manifest, lambda: (_ for _ in ()).throw(RuntimeError("device failed")))
            session = self.session(registry)
            notice = session.listen() if capability == CapabilityKind.STT else session.speak("hola")
            self.assertEqual(notice.kind, "error")
            self.assertEqual(session.handle_text("hola").kind, "chat")

    def test_reasoning_provider_outage_enters_honest_limited_mode_without_corrupting_db_or_history(self):
        request = httpx.Request("POST", ada.GROQ_BASE_URL)
        for error, attempts, expected in (
            (ada.openai.APIConnectionError(request=request), 1, "conexión"),
            (status_error(ada.openai.AuthenticationError, 401), 1, "autenticación"),
            (status_error(ada.openai.RateLimitError, 429), 2, "cuota"),
            (ada.openai.APITimeoutError(request=request), 1, "tiempo"),
        ):
            with self.subTest(error=type(error).__name__):
                session = self.session()
                self.client.chat.completions.create.side_effect = [
                    completion("primera"), *[error] * attempts, completion("recuperada")]
                self.assertEqual(session.handle_text("hola").kind, "chat")
                before, database = copy.deepcopy(session.messages), self.snapshot()
                result = session.handle_text("fallará")
                self.assertEqual(result.kind, "error")
                self.assertIn(expected, result.text.lower())
                self.assertIn("limitado", result.text.lower())
                self.assertNotIn("gsk_secret12345", result.text)
                self.assertEqual(session.messages, before)
                self.assertEqual(self.snapshot(), database)
                self.assertEqual(session.handle_text("/estado").kind, "status")
                self.assertEqual(session.handle_text("salir").kind, "exit")
                self.assertEqual(session.handle_text("reintento").text, "recuperada")
                self.assertNotIn("fallará", str(self.client.chat.completions.create.call_args))
        self.client.chat.completions.create.side_effect = None

    def test_memory_command_aborts_safely_when_reasoning_provider_is_unavailable(self):
        request = httpx.Request("POST", ada.GROQ_BASE_URL)
        for text in ("Anota mi edad: 30", "Borra mi bebida"):
            for error, attempts in (
                (ada.openai.APIConnectionError(request=request), 1),
                (status_error(ada.openai.AuthenticationError, 401), 1),
                (status_error(ada.openai.RateLimitError, 429), 2),
            ):
                with self.subTest(text=text, error=type(error).__name__):
                    session = self.session()
                    before, database = copy.deepcopy(session.messages), self.snapshot()
                    self.client.chat.completions.create.side_effect = [
                        *[error] * attempts, completion("recuperada")]
                    result = session.handle_text(text)
                    self.assertEqual(result.kind, "error")
                    self.assertIn("extraer", result.text)
                    self.assertIn("razonamiento", result.text)
                    self.assertEqual(self.snapshot(), database)
                    self.assertEqual(session.messages, before)
                    self.assertNotIn("Información guardada.", result.text)
                    self.assertNotIn("Información eliminada", result.text)
                    self.assertEqual(session.handle_text("hola").text, "recuperada")

    def test_status_command_lists_installed_enabled_compatible_and_available_extensions(self):
        session = self.session()
        before, database = copy.deepcopy(session.messages), self.snapshot()
        with patch("ada_adapters.voice_adapters._available", return_value=False):
            status = session.handle_text("/estado")
        self.assertEqual(status.kind, "status")
        for extension in ("reasoning.groq", "reasoning.ollama_local", "memory.sqlite",
                          "stt.faster_whisper", "tts.edge_with_local_fallback",
                          "navigation.browser_use_edge"):
            self.assertIn(extension, status.text)
        for field in ("installed", "enabled", "compatible", "available", "model"):
            self.assertIn(field, status.text.lower())
        self.assertIn(ada.DEFAULT_MODEL, status.text)
        self.assertIn("DISABLED", status.text)
        self.assertIn("UNAVAILABLE_DEPENDENCY", status.text)
        self.factory.assert_not_called()
        self.assertEqual(session.messages, before)
        self.assertEqual(self.snapshot(), database)
        session.registry.disable("memory.sqlite")
        self.assertIn("DISABLED", session.handle_text("/estado").text)
        manifest = ExtensionManifest(
            "incompatible", CapabilityKind.AGENT, SideEffectProfile.PURE_READ)
        object.__setattr__(manifest, "contract_version", 999)
        session.registry.register(manifest, Mock())
        self.assertIn("REJECTED_INCOMPATIBLE", session.handle_text("/estado").text)
        # Local diagnostics never invoke a model or revive a failed extension.
        self.client.chat.completions.create.assert_not_called()

    def test_groq_primary_exhaustion_transparently_uses_backup_model_inside_orchestrator(self):
        self.client.chat.completions.create.side_effect = [
            status_error(ada.openai.RateLimitError, 429), completion("respaldo"), completion("siguiente")]
        session = self.session()
        self.assertEqual(session.handle_text("hola").text, "respaldo")
        self.assertEqual(session.handle_text("otra pregunta").text, "siguiente")
        models = [call.kwargs["model"] for call in self.client.chat.completions.create.call_args_list]
        self.assertEqual(models, [*ada.DEFAULT_GROQ_MODELS, ada.DEFAULT_GROQ_MODELS[1]])
        self.assertIn(ada.DEFAULT_GROQ_MODELS[1], session.handle_text("/estado").text)
        self.assertEqual(len(session.messages), 5)

    def test_provider_local_flag_switches_active_reasoning_extension_to_ollama(self):
        args = ada.build_argument_parser().parse_args(["--provider", "local", "--speak"])
        self.client.base_url = ada.DEFAULT_LOCAL_URL
        self.factory.return_value = (self.client, ada.DEFAULT_LOCAL_MODEL)
        session = self.session(provider=args.provider)
        self.assertEqual(session.handle_text("hola").kind, "chat")
        self.factory.assert_called_once_with("local")
        call = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(call["model"], ada.DEFAULT_LOCAL_MODEL)
        self.assertEqual(call["extra_body"], {"options": {"think": False, "num_ctx": 2048}})
        active = [row.extension_id for row in session.registry.diagnostics() if row.active
                  and row.capability == CapabilityKind.REASONING]
        self.assertEqual(active, ["reasoning.ollama_local"])
        self.assertTrue(args.speak)

    def test_memory_batches_preserve_atomicity_and_actual_deletion_acknowledgements(self):
        session = self.session()
        for content in ('[{"key":"edad","value":"30"},{}]',
                        '[{"key":"bebida","value":"agua"},{"key":"blocked","value":"no"}]'):
            with memory.get_db_connection() as conn:
                conn.execute("""CREATE TRIGGER IF NOT EXISTS fail_insert BEFORE INSERT ON memories
                    WHEN NEW.key = 'blocked' BEGIN SELECT RAISE(ABORT, 'blocked'); END""")
            self.client.chat.completions.create.return_value = completion(content)
            before, database = copy.deepcopy(session.messages), self.snapshot()
            self.assertEqual(session.handle_text("Anota mi edad").kind, "error")
            self.assertEqual(self.snapshot(), database)
            self.assertEqual(session.messages, before)
            session = self.session()
        self.client.chat.completions.create.return_value = completion('["bebida"]')
        with memory.get_db_connection() as conn:
            conn.execute("CREATE TRIGGER keep BEFORE DELETE ON memories BEGIN SELECT RAISE(IGNORE); END")
        result = session.handle_text("Borra mi bebida")
        self.assertNotIn("Información eliminada", result.text)
        self.assertEqual(memory.get_all_memories(), [("preferences", "bebida", "té")])

    def test_memory_commands_use_complete_catalogue_and_confirm_only_committed_batches(self):
        for index in range(7):
            self.remember(f"private-value-{index}", "general", f"dato_{index}")
        session = self.session()
        self.client.chat.completions.create.return_value = completion('["dato_6"]')
        result = session.handle_text("Borra el último dato")
        self.assertEqual((result.kind, result.text),
                         ("memory", "Información eliminada de mi base de datos."))
        prompt = self.client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        for index in range(7):
            self.assertIn(f"dato_{index}", prompt)
            self.assertNotIn(f"private-value-{index}", prompt)
        self.assertNotIn("dato_6", [row[1] for row in memory.get_all_memories()])
        self.client.chat.completions.create.return_value = completion(
            '[{"key":"nueva","value":"sí"},{"key":"otra","value":"también"}]')
        result = session.handle_text("Anota dos datos")
        self.assertEqual((result.kind, result.text), ("memory", "Información guardada."))
        self.assertEqual(memory.get_all_memories()[-2:],
                         [("general", "nueva", "sí"), ("general", "otra", "también")])
        self.assertEqual(len(session.messages), 5)

    def test_recording_finishes_before_next_prompt_even_after_registry_deadline(self):
        release, finished = threading.Event(), threading.Event()
        prompts = []
        voice = Mock()

        def record():
            release.wait(2)
            finished.set()
            return "hola"

        voice.listen_manual_push.side_effect = record
        original = ExtensionRegistry.execute

        def bounded(registry, capability, operation, **kwargs):
            if capability == CapabilityKind.STT:
                kwargs["timeout_s"] = 0.01
            return original(registry, capability, operation, **kwargs)

        count = 0

        def typed(prompt):
            nonlocal count
            count += 1
            if count == 1:
                return ""
            prompts.append(finished.is_set())
            return "salir"

        timer = threading.Timer(0.1, release.set)
        timer.start()
        try:
            with patch.object(ada, "VoiceEngine", return_value=voice), \
                 patch("ada_adapters.voice_adapters._available", return_value=True), \
                 patch.object(ExtensionRegistry, "execute", bounded), \
                 patch("builtins.input", side_effect=typed), patch.object(ada, "print_header"):
                ada.chat_loop()
            self.assertEqual(prompts, [True])
            self.client.chat.completions.create.assert_called_once()
            voice.listen_manual_push.assert_called_once()
        finally:
            release.set()
            timer.join()
            finished.wait(2)

    def test_injected_browser_runner_and_readiness_are_independent_doubles(self):
        result = Mock(text="page text", url="https://example.com")
        with patch.object(ada, "run_browser_task", return_value=result) as runner, \
             patch("ada_adapters.browser_navigation_adapter.BrowserUseNavigationAdapter.probe",
                   return_value=HealthReport(ExtensionState.READY)), \
             patch("browser_navigation.worker_python", side_effect=AssertionError("No real browser")), \
             patch("builtins.input", side_effect=["/navegar https://example.com Read", "salir"]), \
             patch.object(ada, "print_header"):
            ada.chat_loop()
        runner.assert_called_once_with("https://example.com", "Read")
        self.assertIn("page text", self.output.getvalue())
        self.client.chat.completions.create.assert_not_called()

    def test_local_url_rejection_preserves_exact_output_without_claiming_http_failure(self):
        with patch("builtins.input", side_effect=["/navegar https://@example.com Read", "salir"]), \
             patch.object(ada, "print_header"), \
             patch("browser_navigation.worker_python") as prepare:
            ada.chat_loop()
        self.assertEqual(self.output.getvalue().splitlines()[0],
                         "🔴 Usa una URL HTTPS pública, sin credenciales y del dominio autorizado.")
        self.assertNotIn("403", self.output.getvalue())
        self.client.chat.completions.create.assert_not_called()
        prepare.assert_not_called()
