"""Public session integration checks with fake providers and temporary SQLite."""

import copy
from contextlib import closing
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from rich.console import Console

import memory
from ada_core import (
    CapabilityError, CapabilityKind, CompletionResponse, ErrorCode, ExtensionManifest,
    ExtensionRegistry, ExtensionState, HealthReport, SideEffectProfile, TurnMode,
    WebNavigationResponse,
)
from ada_adapters import GroqChainReasoningAdapter, SqliteMemoryAdapter, register_default_adapters
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

    def test_optional_extensions_recover_only_on_the_next_explicit_turn(self):
        cases = (
            (CapabilityKind.NAVIGATION, "navigate", ErrorCode.EXECUTION_FAILED,
             WebNavigationResponse("página", "https://example.com"), "navigation",
             lambda session: session.handle_text("/navegar https://example.com resume")),
            (CapabilityKind.TTS, "speak", ErrorCode.TIMEOUT, None, "speech",
             lambda session: session.speak("hola")),
            (CapabilityKind.STT, "transcribe", ErrorCode.EXECUTION_FAILED, "hola", "input",
             lambda session: session.listen()),
            (CapabilityKind.STT, "transcribe", ErrorCode.DEPENDENCY_MISSING, "hola", "input",
             lambda session: session.listen()),
        )
        for capability, method, code, output, kind, turn in cases:
            with self.subTest(capability=capability, code=code):
                registry = self.minimal_registry()
                manifest = ExtensionManifest(f"flaky.{capability.value}", capability,
                                             SideEffectProfile.PURE_READ)
                operation = Mock(side_effect=[CapabilityError(code, capability, "first call"), output])
                adapter = type("FakeAdapter", (), {})()
                setattr(adapter, method, operation)
                registry.register(manifest, lambda: adapter)
                session = self.session(registry)
                before, database = list(session.messages), self.snapshot()

                failed = turn(session)
                state = "UNAVAILABLE_DEPENDENCY" if code == ErrorCode.DEPENDENCY_MISSING else "FAILED"
                self.assertEqual(failed.kind, "error")
                self.assertIn(f"{capability.value}/{code.value}", failed.text)
                self.assertIn(f"{manifest.extension_id}={state}", failed.text)
                self.assertIn(f"{manifest.extension_id} | yes | yes | yes | no | {state}",
                              session.status().text)
                operation.assert_called_once()
                self.assertEqual((session.messages, self.snapshot()), (before, database))

                self.assertEqual(turn(session).kind, kind)
                self.assertEqual(operation.call_count, 2)
                self.assertEqual((session.messages, self.snapshot()), (before, database))

    def test_reasoning_provider_outage_enters_honest_limited_mode_without_corrupting_db_or_history(self):
        request = httpx.Request("POST", ada.GROQ_BASE_URL)
        for error, attempts, expected in (
            (ada.openai.APIConnectionError(request=request), 1, "conexión"),
            (status_error(ada.openai.AuthenticationError, 401), 1, "autenticación"),
            (status_error(ada.openai.RateLimitError, 429), 2, "cuota"),
            (ada.openai.APITimeoutError(request=request), 1, "tiempo"),
        ):
            with self.subTest(error=type(error).__name__):
                session = self.session(self.minimal_registry())
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
                    session = self.session(self.minimal_registry())
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

    def test_groq_retry_keeps_rotated_model_after_unusable_response(self):
        primary, backup = ada.DEFAULT_GROQ_MODELS
        create = self.client.chat.completions.create
        for content, reason, confirmed_primary in (("", "stop", False), ("partial", "length", True)):
            with self.subTest(finish_reason=reason, confirmed_primary=confirmed_primary):
                session = self.session()
                if confirmed_primary:
                    create.side_effect = None
                    self.assertEqual(session.handle_text("hola").text, "Respuesta confirmada")
                    self.assertEqual(session.model, primary)
                create.reset_mock()
                create.side_effect = [
                    status_error(ada.openai.RateLimitError, 429),
                    completion(content, finish_reason=reason),
                    completion("respaldo confirmado"),
                ]
                result = session.handle_text("otra pregunta")
                self.assertEqual((result.kind, result.text), ("chat", "respaldo confirmado"))
                self.assertEqual([call.kwargs["model"] for call in create.call_args_list],
                                 [primary, backup, backup])
                self.assertEqual(session.model, backup)
                self.assertEqual([message.content for message in session.messages[-2:]],
                                 ["otra pregunta", "respaldo confirmado"])

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

    def test_local_retry_keeps_the_selected_model(self):
        self.client.base_url = ada.DEFAULT_LOCAL_URL
        self.factory.return_value = (self.client, ada.DEFAULT_LOCAL_MODEL)
        session = self.session(provider="local")
        self.assertEqual(session.handle_text("hola").kind, "chat")
        create = self.client.chat.completions.create
        create.reset_mock()
        create.side_effect = [completion("partial", finish_reason="length"), completion("completo")]
        result = session.handle_text("otra pregunta")
        self.assertEqual((result.kind, result.text), ("chat", "completo"))
        self.assertEqual([call.kwargs["model"] for call in create.call_args_list],
                         [ada.DEFAULT_LOCAL_MODEL, ada.DEFAULT_LOCAL_MODEL])

    def test_retry_preserves_model_for_stateless_reasoning_port(self):
        models = []

        class Port:
            def complete(self, request):
                models.append(request.model)
                if len(models) == 2:
                    raise CapabilityError(
                        ErrorCode.INCOMPLETE_RESPONSE, CapabilityKind.REASONING, "partial"
                    )
                model = request.model or (
                    "named-model" if request.mode == TurnMode.REASONING else "default-model"
                )
                return SimpleNamespace(text=f"answer from {model}", model=model)

        registry = self.minimal_registry()
        registry.register(
            ExtensionManifest(
                "reasoning.fixture", CapabilityKind.REASONING, SideEffectProfile.PURE_READ
            ),
            Port,
        )
        registry.set_preferred_provider(CapabilityKind.REASONING, "reasoning.fixture")
        session = self.session(registry)
        text = "Compara estas dos opciones"
        self.assertEqual(session.handle_text(text).text, "answer from named-model")
        result = session.handle_text(text)
        self.assertEqual((result.kind, result.text), ("chat", "answer from named-model"))
        self.assertEqual(models, ["", "named-model", "named-model"])

    def install_reasoning_doubles(self):
        local = Mock(base_url=ada.DEFAULT_LOCAL_URL)
        groq = self.client
        clients = {"local": (local, ada.DEFAULT_LOCAL_MODEL), "groq": (groq, ada.DEFAULT_MODEL)}
        self.factory.side_effect = clients.__getitem__
        return local.chat.completions.create, groq.chat.completions.create

    def register_third_reasoning_provider(self, session, complete):
        manifest = ExtensionManifest("reasoning.fixture", CapabilityKind.REASONING,
                                     SideEffectProfile.PURE_READ)
        session.registry.register(manifest, lambda: Mock(complete=complete))

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_fixture"})
    def test_reasoning_queue_reaches_a_third_provider_after_local_and_groq(self):
        local, groq = self.install_reasoning_doubles()
        attempts = []

        def fail_local(**kwargs):
            attempts.append(("local", kwargs["model"]))
            raise ada.openai.APIConnectionError(request=httpx.Request("POST", ada.DEFAULT_LOCAL_URL))

        def fail_groq(**kwargs):
            attempts.append(("groq", kwargs["model"]))
            raise status_error(ada.openai.RateLimitError, 429)

        def succeed(request):
            attempts.append(("fixture", request.model))
            return CompletionResponse("Third provider answered", "fixture-model")

        local.side_effect, groq.side_effect = fail_local, fail_groq
        session = self.session(provider="local")
        self.register_third_reasoning_provider(session, succeed)

        result = session.handle_text("hola")

        self.assertEqual(attempts, [("local", ada.DEFAULT_LOCAL_MODEL),
                                    *[("groq", model) for model in ada.DEFAULT_GROQ_MODELS],
                                    ("fixture", "")])
        self.assertEqual((result.kind, result.text, session.model),
                         ("chat", "Third provider answered", "fixture-model"))
        self.assertEqual(len(result.notices), 1)
        self.assertIn("reasoning.groq", result.notices[0])
        self.assertEqual(session.handle_text("otra pregunta").kind, "chat")
        self.assertEqual(attempts[-1], ("fixture", "fixture-model"))
        self.assertEqual(len(attempts), 5)

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_fixture"})
    def test_provider_promotion_is_sticky_and_local_can_recover_after_groq_exhausts_quota(self):
        local, groq = self.install_reasoning_doubles()
        local.side_effect = [RuntimeError("Ollama stopped"), completion("Local recovered")]
        groq.side_effect = [
            completion("Groq backup"), completion("Groq still active"),
            *[status_error(ada.openai.RateLimitError, 429) for _ in ada.DEFAULT_GROQ_MODELS],
        ]
        session = self.session(provider="local")

        first = session.handle_text("hola")
        self.assertEqual(first.text, "Groq backup")
        self.assertEqual(len(first.notices), 1)
        self.assertIn("reasoning.groq", first.notices[0])
        self.assertIn(f"yes | {ada.DEFAULT_MODEL}", session.handle_text("/estado").text)
        second = session.handle_text("otra pregunta")
        self.assertEqual((second.text, second.notices), ("Groq still active", ()))
        local.assert_called_once()
        third = session.handle_text("una última pregunta")
        self.assertEqual((third.text, third.notices), ("Local recovered", ()))
        self.assertIn(f"yes | {ada.DEFAULT_LOCAL_MODEL}", session.handle_text("/estado").text)
        self.assertEqual([call.kwargs["model"] for call in local.call_args_list],
                         [ada.DEFAULT_LOCAL_MODEL] * 2)
        self.assertEqual([call.kwargs["model"] for call in groq.call_args_list],
                         [ada.DEFAULT_MODEL, ada.DEFAULT_MODEL, *ada.DEFAULT_GROQ_MODELS])

    def test_local_failure_skips_unconfigured_groq_and_still_reaches_the_next_provider(self):
        local, groq = self.install_reasoning_doubles()
        local.side_effect = RuntimeError("Ollama stopped")
        session = self.session(provider="local")
        fallback = Mock(return_value=CompletionResponse("Offline fallback", "fixture-model"))
        self.register_third_reasoning_provider(session, fallback)

        result = session.handle_text("hola")

        self.assertEqual((result.kind, result.text, result.notices),
                         ("chat", "Offline fallback", ()))
        self.factory.assert_called_once_with("local")
        groq.assert_not_called()
        fallback.assert_called_once()

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_fixture"})
    def test_terminal_shows_cloud_notice_once_before_the_fallback_request(self):
        local, groq = self.install_reasoning_doubles()
        local.side_effect = RuntimeError("Ollama stopped")
        shown = []
        marker = "proveedor en la nube: reasoning.groq"

        def observe_notice(**kwargs):
            shown.append(marker in self.output.getvalue())
            return completion("Groq backup")

        groq.side_effect = observe_notice
        with patch.object(ada, "print_header"), patch("builtins.input", side_effect=["hola", "salir"]):
            ada.chat_loop(provider="local")

        self.assertEqual(shown, [True])
        self.assertEqual(self.output.getvalue().count(marker), 1)

    def test_injected_default_catalog_honors_the_requested_initial_provider(self):
        local, groq = self.install_reasoning_doubles()
        local.return_value = completion("Local answer")
        registry = ExtensionRegistry()
        register_default_adapters(registry, provider="groq")
        session = self.session(registry, provider="local")

        self.assertEqual(session.handle_text("hola").text, "Local answer")
        groq.assert_not_called()

    def test_disabling_preferred_local_does_not_bypass_cloud_safeguards(self):
        for key in ("", "gsk_fixture"):
            with self.subTest(configured=bool(key)), patch.dict(os.environ, {"GROQ_API_KEY": key}):
                local, groq = self.install_reasoning_doubles()
                local.return_value = completion("Local answer")
                session = self.session(provider="local")
                self.assertEqual(session.handle_text("hola").text, "Local answer")
                session.registry.disable("reasoning.ollama_local")
                seen = []
                groq.side_effect = lambda **kwargs: (
                    completion("Cloud answer") if seen else completion("Notice was missing")
                )
                self.factory.reset_mock()
                groq.reset_mock()

                result = session.handle_text("otra pregunta", on_reasoning_notice=seen.append)

                if key:
                    self.assertEqual(result.text, "Cloud answer")
                    self.assertEqual(result.notices, tuple(seen))
                    self.assertEqual(len(seen), 1)
                    self.factory.assert_called_once_with("groq")
                else:
                    self.assertEqual((result.kind, result.notices, seen), ("error", (), []))
                    self.factory.assert_not_called()
                    groq.assert_not_called()

    def test_status_does_not_mark_unattempted_reasoning_backup_active(self):
        local, groq = self.install_reasoning_doubles()
        local.side_effect = [
            completion("Local answer"),
            ada.openai.APITimeoutError(request=httpx.Request("POST", ada.DEFAULT_LOCAL_URL)),
        ]
        session = self.session(provider="local")
        self.assertEqual(session.handle_text("hola").text, "Local answer")
        self.assertEqual(session.handle_text("otra pregunta").kind, "error")

        line = next(row for row in session.handle_text("/estado").text.splitlines()
                    if row.startswith("reasoning.groq "))

        self.assertEqual(line.split(" | ")[6:8], ["no", ada.DEFAULT_MODEL])
        self.assertEqual(session.model, ada.DEFAULT_LOCAL_MODEL)
        groq.assert_not_called()

    def test_transient_reasoning_dependency_recovers_on_a_new_operation(self):
        local, _ = self.install_reasoning_doubles()
        local.return_value = completion("Local recovered")
        client = self.factory.side_effect("local")
        self.factory.side_effect = [ImportError("Temporary dependency failure"), client]
        session = self.session(provider="local")
        session.registry.disable("reasoning.groq")

        self.assertEqual(session.handle_text("hola").kind, "error")
        self.factory.assert_called_once_with("local")
        self.assertEqual(session.handle_text("otra pregunta").text, "Local recovered")
        self.assertEqual(self.factory.call_count, 2)

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_fixture"})
    def test_unusable_third_party_response_retries_before_advancing(self):
        local, groq = self.install_reasoning_doubles()
        local.side_effect = RuntimeError("Ollama stopped")
        groq.side_effect = RuntimeError("Groq stopped")
        session = self.session(provider="local")
        blank = Mock(return_value=CompletionResponse("   ", "fixture-model"))
        self.register_third_reasoning_provider(session, blank)
        manifest = ExtensionManifest("reasoning.last", CapabilityKind.REASONING,
                                     SideEffectProfile.PURE_READ)
        last = Mock(return_value=CompletionResponse("Usable answer", "last-model"))
        session.registry.register(manifest, lambda: Mock(complete=last))

        self.assertEqual(session.handle_text("hola").text, "Usable answer")
        self.assertEqual(blank.call_count, 2)
        last.assert_called_once()
        self.assertEqual(session.model, "last-model")

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_fixture"})
    def test_failed_first_reasoning_turn_does_not_initialize_sqlite(self):
        local, groq = self.install_reasoning_doubles()
        local.side_effect = RuntimeError("Ollama stopped")
        groq.side_effect = RuntimeError("Groq stopped")
        for index, text in enumerate(("hola", "Anota mi edad: 30")):
            with self.subTest(text=text):
                fresh = Path(memory.DB_PATH).with_name(f"not-initialized-{index}.sqlite")
                with patch.object(memory, "DB_PATH", str(fresh)):
                    session = self.session(provider="local")
                    before = list(session.messages)
                    self.assertEqual(session.handle_text(text).kind, "error")
                    self.assertEqual(session.messages, before)
                    self.assertFalse(fresh.exists())

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_fixture"})
    def test_exhausting_providers_reports_each_failure_and_keeps_history_and_sqlite_intact(self):
        local, groq = self.install_reasoning_doubles()
        local.side_effect = ada.openai.APIConnectionError(
            request=httpx.Request("POST", ada.DEFAULT_LOCAL_URL))
        groq.side_effect = [status_error(ada.openai.RateLimitError, 429)
                            for _ in ada.DEFAULT_GROQ_MODELS]
        session = self.session(provider="local")
        third = Mock(side_effect=RuntimeError("fixture backend stopped"))
        self.register_third_reasoning_provider(session, third)
        before, db_hash = list(session.messages), hashlib.sha256(self.snapshot()).digest()

        result = session.handle_text("hola")

        self.assertEqual(result.kind, "error")
        self.assertEqual(len(result.notices), 1)
        for detail in ("reasoning.ollama_local: network_unavailable", "reasoning.groq: quota_exceeded",
                       "reasoning.fixture: execution_failed", "fixture backend stopped"):
            self.assertIn(detail, result.text)
        self.assertEqual(session.messages, before)
        self.assertEqual(hashlib.sha256(self.snapshot()).digest(), db_hash)
        local.assert_called_once()
        self.assertEqual(groq.call_count, len(ada.DEFAULT_GROQ_MODELS))
        third.assert_called_once()

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_fixture"})
    def test_memory_extraction_retries_the_same_provider_then_uses_and_promotes_the_backup(self):
        local, groq = self.install_reasoning_doubles()
        local.side_effect = [completion(""), completion("partial", finish_reason="length")]
        groq.side_effect = [
            completion('[{"category": "general", "key": "edad", "value": "30"}]'),
            completion("Groq still active"),
        ]
        session = self.session(provider="local")

        result = session.handle_text("Anota mi edad: 30")

        self.assertEqual(result.kind, "memory")
        self.assertEqual(len(result.notices), 1)
        self.assertIn("reasoning.groq", result.notices[0])
        with closing(sqlite3.connect(memory.DB_PATH)) as connection:
            self.assertIn(("edad", "30"), connection.execute("SELECT key, value FROM memories").fetchall())
        self.assertEqual([call.kwargs["model"] for call in local.call_args_list],
                         [ada.DEFAULT_LOCAL_MODEL] * 2)
        self.assertEqual(session.handle_text("hola").text, "Groq still active")

    def test_malformed_memory_batches_leave_storage_ready_for_recall_and_save(self):
        session = self.session()
        self.assertEqual(session.handle_text("¿Cuál es mi bebida?").kind, "chat")
        for command, content, expected in (
            ("Anota mi edad", '[{"key":"edad","value":30}]',
             "Hubo un error al intentar extraer y guardar la información."),
            ("Anota mi edad", '[{"key":"edad","value":"30"},{}]',
             "Hubo un error al intentar extraer y guardar la información."),
            ("Anota mi ciudad", r'[{"key":"ciudad","value":"Bogot\ud800"}]',
             "Hubo un error al intentar extraer y guardar la información."),
            ("Borra mi bebida", '["bebida", 30]',
             "Hubo un error al procesar el borrado de memoria."),
        ):
            self.client.chat.completions.create.side_effect = [
                completion(content), completion("té"), completion('[{"key":"edad","value":"30"}]'),
            ]
            before, database = copy.deepcopy(session.messages), self.snapshot()
            result = session.handle_text(command)
            self.assertEqual((result.kind, result.text), ("error", expected))
            self.assertEqual(self.snapshot(), database)
            self.assertEqual(session.messages, before)
            self.assertIn("memory.sqlite | yes | yes | yes | yes | READY", session.status().text)
            recall = session.handle_text("¿Cuál es mi bebida?")
            self.assertEqual((recall.kind, recall.text, recall.notices), ("chat", "té", ()))
            self.assertIn("té", self.client.chat.completions.create.call_args.kwargs["messages"][0]["content"])
            result = session.handle_text("Anota mi edad")
            self.assertEqual((result.kind, result.text), ("memory", "Información guardada."))
            self.assertIn(("general", "edad", "30"), memory.get_all_memories())

    def test_memory_batches_preserve_atomicity_and_actual_deletion_acknowledgements(self):
        session = self.session()
        with memory.get_db_connection() as conn:
            conn.execute("""CREATE TRIGGER fail_insert BEFORE INSERT ON memories
                WHEN NEW.key = 'blocked' BEGIN SELECT RAISE(ABORT, 'blocked'); END""")
        self.client.chat.completions.create.return_value = completion(
            '[{"key":"bebida","value":"agua"},{"key":"blocked","value":"no"}]')
        before, database = copy.deepcopy(session.messages), self.snapshot()
        failure = session.handle_text("Anota mi edad")
        self.assertEqual(failure.kind, "error")
        self.assertIn("memory/execution_failed", failure.text)
        self.assertEqual((session.messages, self.snapshot()), (before, database))

        self.client.chat.completions.create.return_value = completion('["bebida"]')
        with memory.get_db_connection() as conn:
            conn.execute("CREATE TRIGGER keep BEFORE DELETE ON memories BEGIN SELECT RAISE(IGNORE); END")
        result = session.handle_text("Borra mi bebida")
        self.assertEqual((result.kind, result.text),
                         ("memory", "No encontré ese dato en mi memoria para borrarlo."))
        self.assertEqual(memory.get_all_memories(), [("preferences", "bebida", "té")])

    def test_recall_recovers_on_a_new_turn_without_replaying_the_failed_read(self):
        session = self.session()
        self.assertEqual(session.handle_text("hola").kind, "chat")
        before = self.snapshot()
        with patch.object(memory, "select_memories_for_prompt",
                          side_effect=sqlite3.OperationalError("transient read failure")) as recall:
            failed = session.handle_text("¿Cuál es mi bebida?")
            self.assertEqual(failed.kind, "chat")
            self.assertIn("memory/execution_failed", failed.notices[0])
            recall.assert_called_once()
        recovered = session.handle_text("¿Cuál es mi bebida?")
        self.assertEqual((recovered.kind, recovered.notices), ("chat", ()))
        self.assertIn("té", self.client.chat.completions.create.call_args.kwargs["messages"][0]["content"])
        self.assertEqual(self.snapshot(), before)

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
        prompts = []
        from ada_adapters.voice_adapters import WhisperPushToTalkAdapter
        original = ExtensionRegistry.execute

        def bounded(registry, capability, operation, **kwargs):
            if capability == CapabilityKind.STT:
                kwargs["timeout_s"] = 0.1
            return original(registry, capability, operation, **kwargs)

        count = 0

        def typed(prompt):
            nonlocal count
            count += 1
            if count == 1:
                return ""
            if count == 2:
                # Longer than a short registry operation deadline: user waiting
                # must not be inside registry.execute's daemon call.
                time.sleep(0.15)
                return ""
            prompts.append(count)
            return "salir"

        with patch.object(WhisperPushToTalkAdapter, "start_capture"), \
             patch.object(WhisperPushToTalkAdapter, "stop_capture"), \
             patch.object(WhisperPushToTalkAdapter, "poll_capture",
                          side_effect=[("recording", ""), ("recording", ""),
                                       ("recording", ""), ("complete", "hola")]), \
             patch("ada_adapters.voice_adapters._available", return_value=True), \
             patch.object(ExtensionRegistry, "execute", bounded), \
             patch("builtins.input", side_effect=typed), patch.object(ada, "print_header"):
            ada.chat_loop()
        self.assertEqual(prompts, [3])
        self.client.chat.completions.create.assert_called_once()

    def stt_session(self, mode):
        from ada_adapters.voice_adapters import WhisperPushToTalkAdapter
        import test_stt_worker
        adapter = WhisperPushToTalkAdapter(
            worker_command=[sys.executable, "-B", str(Path(test_stt_worker.__file__)), "--fake", mode],
            init_timeout=0.4 if mode == "init_hang" else 2,
            stop_timeout=0.3, transcription_timeout=0.3)
        registry = self.minimal_registry()
        registry.register(adapter.manifest, lambda: adapter,
                          probe=lambda: HealthReport(ExtensionState.READY))
        session = self.session(registry)
        self.addCleanup(session.shutdown)
        return session, adapter

    def test_failed_rejected_and_silent_capture_preserve_exact_history_and_sqlite(self):
        from ada_core import CapabilityError
        from ada_adapters.voice_adapters import TerminalInput
        for mode in ("silence", "device", "init_hang", "recording_error"):
            with self.subTest(mode=mode):
                session, adapter = self.stt_session(mode)
                before, database = copy.deepcopy(session.messages), self.snapshot()

                def stop(tick):
                    if mode == "recording_error":
                        while True:
                            tick()
                            time.sleep(0.01)
                    else:
                        with patch("builtins.input", return_value=""):
                            TerminalInput().wait_for_stop(tick)

                self.client.chat.completions.create.reset_mock()
                result = session.listen(stop)
                self.assertEqual(result.kind, "input" if mode == "silence" else "error")
                if mode == "silence":
                    self.assertEqual(result.text, "")
                self.assertEqual(session.messages, before)
                self.assertEqual(self.snapshot(), database)
                self.client.chat.completions.create.assert_not_called()
                states = session.status().text
                self.assertIn("READY" if mode == "silence" else "FAILED", states)
                self.assertNotIn("DEGRADED", states)
                self.assertNotIn("tras el reintento", result.text)
                with self.assertRaises(CapabilityError):
                    adapter.transcribe(b"rejected audio")
                self.assertEqual(session.messages, before)
                self.assertEqual(self.snapshot(), database)
                self.assertEqual(session.handle_text("mensaje escrito").kind, "chat")
                self.client.chat.completions.create.assert_called_once()
                for exit_word in ("salir", "exit", "quit"):
                    self.assertEqual(session.handle_text(exit_word).kind, "exit")
                session.shutdown()

    def test_chat_loop_failure_then_written_turn_status_and_exit_are_consumed_once(self):
        for exit_word in ("salir", "exit", "quit"):
            with self.subTest(exit_word=exit_word):
                session, adapter = self.stt_session("init_hang")
                self.client.chat.completions.create.reset_mock()
                with patch.object(ada, "create_session", return_value=session), \
                     patch.object(ada, "print_header"), \
                     patch("builtins.input", side_effect=["", "written once", "/estado", exit_word]) as keyboard:
                    ada.chat_loop()
                self.assertEqual(keyboard.call_count, 4)
                self.client.chat.completions.create.assert_called_once()
                self.assertEqual([m.content for m in session.messages if m.role == "user"],
                                 ["written once"])
                self.assertIsNone(adapter.process)

    def test_explicit_microphone_retry_after_timeout_recovers_without_registry_race(self):
        from ada_adapters.voice_adapters import TerminalInput
        session, adapter = self.stt_session("init_hang")
        self.assertEqual(session.listen(lambda tick: None).kind, "error")
        self.assertIsNone(adapter.process)
        adapter.worker_command[-1] = "normal"
        adapter.init_timeout = 2
        for expected in ("hola 1", "hola 2"):
            with patch("builtins.input", return_value=""):
                result = session.listen(TerminalInput().wait_for_stop)
            self.assertEqual((result.kind, result.text), ("input", expected))
            self.assertIn("READY", session.status().text)

    def test_unconfirmed_exit_is_failed_in_status_but_text_and_exit_remain_usable(self):
        import subprocess
        session, adapter = self.stt_session("normal")

        def quarantine(tick):
            process = adapter.process
            adapter._deadline = 0
            with patch.object(process, "poll", return_value=None), \
                 patch.object(process, "wait", side_effect=subprocess.TimeoutExpired("fake", 0)):
                tick()

        result = session.listen(quarantine)
        self.assertEqual(result.kind, "error")
        status = session.status().text
        self.assertIn("FAILED", status)
        self.assertIn("quarantined", status)
        with patch("subprocess.Popen") as launch:
            self.assertEqual(session.listen(lambda tick: None).kind, "error")
            launch.assert_not_called()
        self.assertEqual(session.handle_text("written").kind, "chat")
        self.assertEqual(session.handle_text("exit").kind, "exit")

    def test_ctrl_c_during_recording_exits_and_reaps_worker(self):
        session, adapter = self.stt_session("normal")
        with patch.object(ada, "create_session", return_value=session), \
             patch.object(ada, "print_header"), \
             patch("builtins.input", side_effect=["", KeyboardInterrupt]):
            ada.chat_loop()
        self.assertIsNone(adapter.process)
        self.client.chat.completions.create.assert_not_called()

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
