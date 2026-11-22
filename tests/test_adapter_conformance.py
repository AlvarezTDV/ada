"""Offline port conformance with HTTP doubles and real temporary SQLite files."""

import gc
import builtins
from dataclasses import replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI
from openai.types.chat import ChatCompletion  # Load SDK types before the voice import fixture.

from test_memory_prompt import ada
from ada_core import (
    CapabilityError, CapabilityKind, ChatMessage, CompletionRequest,
    CompletionResponse, ErrorCode, ExtensionManifest, MemoryItem, MemoryPort,
    ExecutionModel, ExtensionRegistry, ExtensionState, HealthReport, ReasoningPort,
    SpeechInputPort, SpeechOutputPort, SpeechOutputReport, TurnMode,
    WebNavigationPort, WebNavigationRequest, WebNavigationResponse,
)
from ada_adapters import (
    BrowserUseNavigationAdapter, GroqChainReasoningAdapter, OllamaReasoningAdapter, SqliteMemoryAdapter,
)
import memory
import browser_navigation


CHAIN = "llama-3.3-70b-versatile,openai/gpt-oss-20b,openai/gpt-oss-120b"


class AdapterConformanceTests(unittest.TestCase):
    def setUp(self):
        # Bind provider imports to the shared Ada fixture during HTTP-double tests.
        existed, previous = "ada" in sys.modules, sys.modules.get("ada")
        sys.modules["ada"] = ada

        def restore_ada():
            if existed:
                sys.modules["ada"] = previous
            else:
                sys.modules.pop("ada", None)

        self.addCleanup(restore_ada)
        sockets = patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden"))
        sockets.start()
        self.addCleanup(sockets.stop)
        environment = patch.dict(os.environ, {
            "GROQ_API_KEY": "gsk_conformance", "ADA_GROQ_MODELS": CHAIN,
            "ADA_LOCAL_MODEL": "qwen2.5:3b",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def make_adapter(self, adapter_type, outcomes):
        calls = []
        outcomes = iter(outcomes)

        def respond(request):
            calls.append((str(request.url), json.loads(request.content)))
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            status, content, finish = outcome
            if status != 200:
                return httpx.Response(status, json={"error": {"message": "rejected"}})
            return httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 0,
                "model": calls[-1][1]["model"],
                "choices": [] if finish == "no_choices" else [{
                    "index": 0, "message": {"role": "assistant", "content": content},
                    "finish_reason": finish,
                }],
            })

        client = OpenAI(
            api_key="test", max_retries=0,
            base_url=(ada.GROQ_BASE_URL if adapter_type is GroqChainReasoningAdapter
                      else ada.DEFAULT_LOCAL_URL),
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        )
        self.addCleanup(client.close)
        return adapter_type(client=client), calls

    def test_fixture_restores_only_ada_without_evicting_lazy_modules(self):
        from types import ModuleType
        name = "_ada_conformance_identity_probe"
        marker = ModuleType(name)
        sys.modules[name] = marker
        try:
            self.doCleanups()
            self.assertIs(sys.modules.get(name), marker)
        finally:
            sys.modules.pop(name, None)

    def test_groq_and_ollama_adapters_satisfy_reasoning_port_contract_interchangeably(self):
        for adapter_type, extension_id in (
            (GroqChainReasoningAdapter, "reasoning.groq"),
            (OllamaReasoningAdapter, "reasoning.ollama_local"),
        ):
            for mode in TurnMode:
                with self.subTest(adapter=extension_id, mode=mode):
                    adapter, calls = self.make_adapter(adapter_type, [(200, " OK ", "stop")])
                    request = CompletionRequest((ChatMessage("user", "Hello"),),
                                                mode=mode, temperature=0.2, max_tokens=64)
                    self.assertIsInstance(adapter, ReasoningPort)
                    self.assertIsInstance(adapter.manifest, ExtensionManifest)
                    self.assertEqual(adapter.manifest.extension_id, extension_id)
                    self.assertEqual(adapter.manifest.capability, CapabilityKind.REASONING)
                    result = adapter.complete(request)
                    self.assertIsInstance(result, CompletionResponse)
                    self.assertEqual(result.text, "OK")
                    self.assertEqual(result.finish_reason, "stop")
                    url, body = calls[0]
                    self.assertEqual(result.model, body["model"])
                    self.assertEqual(body["messages"], [{"role": "user", "content": "Hello"}])
                    self.assertEqual(body["temperature"], 0.2)
                    self.assertEqual(body["max_tokens"], 64)
                    if adapter_type is OllamaReasoningAdapter:
                        self.assertEqual(url, "http://localhost:11434/v1/chat/completions")
                        self.assertEqual(body["options"], {
                            "think": mode == TurnMode.REASONING, "num_ctx": 2048,
                        })
                    else:
                        self.assertNotIn("options", body)
                        self.assertNotIn("reasoning_effort", body)
                        self.assertEqual(adapter.manifest.required_env, ("GROQ_API_KEY",))

    def test_groq_adapter_rotates_across_model_chain_on_429_and_raises_quota_exceeded_when_all_exhausted(self):
        for failures in (1, 2, 3):
            with self.subTest(failures=failures):
                outcomes = [(429, None, None)] * failures
                if failures < 3:
                    outcomes.append((200, "fallback", "stop"))
                adapter, calls = self.make_adapter(GroqChainReasoningAdapter, outcomes)
                request = CompletionRequest((ChatMessage("user", "Hello"),), max_tokens=64,
                                            mode=TurnMode.REASONING)
                with patch.object(ada.console, "print"):
                    if failures == 3:
                        with self.assertRaises(CapabilityError) as caught:
                            adapter.complete(request)
                        self.assertEqual(caught.exception.code, ErrorCode.QUOTA_EXCEEDED)
                        self.assertEqual(caught.exception.extension_id, "reasoning.groq")
                        self.assertFalse(caught.exception.side_effect_uncertain)
                    else:
                        result = adapter.complete(request)
                        self.assertEqual(result.text, "fallback")
                        self.assertEqual(result.model, CHAIN.split(",")[failures])
                self.assertEqual([body["model"] for _, body in calls],
                                 CHAIN.split(",")[:min(failures + 1, 3)])
                for _, body in calls[1:]:
                    self.assertEqual(body["max_tokens"], 300)
                    self.assertEqual(body["reasoning_effort"], "medium")
                    self.assertFalse(body["include_reasoning"])

    def test_reasoning_adapters_translate_final_failures_without_retrying(self):
        request = httpx.Request("POST", "http://localhost/test")
        cases = [
            ((401, None, None), ErrorCode.AUTH_INVALID),
            ((403, None, None), ErrorCode.PERMISSION_DENIED),
            ((500, None, None), ErrorCode.EXECUTION_FAILED),
            (httpx.ReadTimeout("timeout", request=request), ErrorCode.TIMEOUT),
            (httpx.ConnectError("offline", request=request), ErrorCode.NETWORK_UNAVAILABLE),
            ((200, "", "stop"), ErrorCode.EMPTY_RESPONSE),
            ((200, None, "stop"), ErrorCode.EMPTY_RESPONSE),
            ((200, "partial", "length"), ErrorCode.INCOMPLETE_RESPONSE),
            ((200, "", "no_choices"), ErrorCode.EMPTY_RESPONSE),
        ]
        for adapter_type in (GroqChainReasoningAdapter, OllamaReasoningAdapter):
            for outcome, code in cases:
                with self.subTest(adapter=adapter_type.__name__, code=code):
                    adapter, calls = self.make_adapter(adapter_type, [outcome])
                    with self.assertRaises(CapabilityError) as caught:
                        adapter.complete(CompletionRequest((ChatMessage("user", "Hello"),)))
                    error = caught.exception
                    self.assertEqual(error.code, code)
                    self.assertEqual(error.capability, CapabilityKind.REASONING)
                    self.assertEqual(error.extension_id, adapter.manifest.extension_id)
                    self.assertNotIn("gsk_conformance", str(error))
                    self.assertEqual(len(calls), 1)
        adapter, calls = self.make_adapter(OllamaReasoningAdapter, [(429, None, None)])
        with self.assertRaises(CapabilityError) as caught:
            adapter.complete(CompletionRequest(()))
        self.assertEqual(caught.exception.code, ErrorCode.QUOTA_EXCEEDED)
        self.assertEqual(len(calls), 1)

    def test_reasoning_adapters_reuse_client_configuration_and_model_override(self):
        for adapter_type, provider in ((GroqChainReasoningAdapter, "groq"),
                                       (OllamaReasoningAdapter, "local")):
            adapter, calls = self.make_adapter(adapter_type, [(200, "OK", "stop")])
            with patch.object(ada, "get_client", return_value=(adapter._client, "custom")) as factory:
                result = adapter_type().complete(CompletionRequest((), model="explicit"))
                factory.assert_called_once_with(provider)
            self.assertEqual(result.model, "explicit")
            self.assertEqual(calls[0][1]["model"], "explicit")
        with patch.dict(os.environ, {"GROQ_API_KEY": ""}):
            with self.assertRaises(CapabilityError) as caught:
                GroqChainReasoningAdapter().complete(CompletionRequest(()))
        self.assertEqual(caught.exception.code, ErrorCode.AUTH_INVALID)

    def test_default_reasoning_catalog_keeps_both_providers_enabled_and_prefers_the_requested_one(self):
        from ada_adapters import register_default_adapters

        for provider, adapter_type in (("groq", GroqChainReasoningAdapter),
                                       ("local", OllamaReasoningAdapter)):
            with self.subTest(provider=provider):
                registry = ExtensionRegistry()
                register_default_adapters(registry, provider=provider)
                reasoning = [row for row in registry.diagnostics()
                             if row.capability == CapabilityKind.REASONING]
                self.assertEqual([(row.extension_id, row.state) for row in reasoning], [
                    ("reasoning.groq", ExtensionState.ADMITTED),
                    ("reasoning.ollama_local", ExtensionState.ADMITTED),
                ])
                with patch.object(ada, "get_client") as get_client:
                    self.assertIsInstance(registry.resolve(CapabilityKind.REASONING), adapter_type)
                get_client.assert_not_called()


    def make_session(self, local, groq, third):
        registry = ExtensionRegistry()
        for priority, provider in zip((30, 20, 10), (local, groq, third)):
            registry.register(provider.manifest, lambda provider=provider: provider, priority=priority)
        registry.register(BrowserUseNavigationAdapter.manifest, BrowserUseNavigationAdapter,
                          probe=lambda: HealthReport(ExtensionState.READY))
        return ada.create_session("local", registry, per_provider_timeout=10, clock=lambda: self.now)

    def navigation_providers(self):
        self.now = 100
        self.page = {"url": "https://example.com/current", "title": "Title",
                     "language": "en", "text": "x" * 20_005, "private": "not a page field"}
        local = Mock(spec=ReasoningPort, manifest=OllamaReasoningAdapter.manifest)
        third = Mock(spec=ReasoningPort, manifest=replace(
            OllamaReasoningAdapter.manifest, extension_id="reasoning.third"))
        third.complete.return_value = CompletionResponse('{"answer":"third answer"}', "third-model")
        return local, third

    def test_backup_gets_fresh_deadline_and_notice_before_bounded_payload(self):
        local, third = self.navigation_providers()
        groq, _ = self.make_adapter(GroqChainReasoningAdapter, [])
        events, notices = [], []

        def slow_local(_request):
            events.append("local")
            self.now += 10
            return CompletionResponse('{"answer":"late"}', "local-model")

        def cloud(**kwargs):
            events.append("cloud")
            self.assertEqual(kwargs["timeout"], 10)
            expected = {key: self.page[key] for key in ("url", "title", "language")}
            expected["text"] = "x" * 20_000
            self.assertEqual(json.loads(kwargs["messages"][-1]["content"]),
                             {"task": "Summarize", "observation": expected})
            self.assertNotIn("private chat", str(kwargs["messages"]))
            self.assertNotIn("gsk_conformance", str(kwargs))
            self.now += 9
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"answer":"from cloud"}'), finish_reason="stop")])

        def notice(message):
            notices.append(message)
            events.append("notice")

        local.complete.side_effect = slow_local
        session = self.make_session(local, groq, third)
        session.messages.append(ChatMessage("user", "private chat"))
        with patch.object(browser_navigation, "observe_page", return_value=self.page) as observe, \
             patch.object(groq._client.chat.completions, "create", side_effect=cloud), \
             patch.object(session.registry, "execute", wraps=session.registry.execute) as execute:
            result = session.handle_text("/navegar https://example.com Summarize", on_reasoning_notice=notice)
        self.assertEqual((result.kind, result.text, result.url), ("navigation", "from cloud", self.page["url"]))
        self.assertEqual(events, ["local", "notice", "cloud"])
        self.assertEqual(result.notices, tuple(notices))
        self.assertIn("nube", notices[0])
        self.assertIn("página", notices[0])
        observe.assert_called_once_with("https://example.com")
        third.complete.assert_not_called()
        navigation_call = next(call for call in execute.call_args_list if call.args[0] == CapabilityKind.NAVIGATION)
        self.assertGreaterEqual(navigation_call.kwargs["timeout_s"], browser_navigation.TIMEOUT_SECONDS + 30)
        self.assertEqual(session.registry.provider_catalog(CapabilityKind.REASONING)[0].extension_id,
                         "reasoning.groq")

    def test_model_rotation_is_inside_one_attempt_then_third_provider_becomes_active(self):
        for exhaust_clock in (False, True):
            with self.subTest(exhaust_clock=exhaust_clock):
                local, third = self.navigation_providers()
                local.complete.return_value = CompletionResponse('{"error":"model_task_failed"}', "local-model")
                groq, calls = self.make_adapter(GroqChainReasoningAdapter, [(429, None, None)] * 3)
                session = self.make_session(local, groq, third)
                create = groq._client.chat.completions.create
                timeouts = []

                def spend_time(**kwargs):
                    timeouts.append(kwargs.get("timeout"))
                    self.now += 6 if exhaust_clock else 2
                    return create(**kwargs)

                with patch.object(browser_navigation, "observe_page", return_value=self.page) as observe, \
                     patch.object(groq._client.chat.completions, "create", side_effect=spend_time), \
                     patch.object(ada.console, "print"):
                    result = session.handle_text("/navegar https://example.com Read")
                    again = session.handle_text("/navegar https://example.com Read again")
                self.assertEqual((result.kind, result.text, again.text), ("navigation", "third answer", "third answer"))
                expected_timeouts = [10, 4] if exhaust_clock else [10, 8, 6]
                self.assertEqual(timeouts, expected_timeouts)
                self.assertEqual([body["model"] for _, body in calls], CHAIN.split(",")[:len(expected_timeouts)])
                self.assertEqual(session.registry.provider_catalog(CapabilityKind.REASONING)[0].extension_id,
                                 "reasoning.third")
                self.assertEqual(session.model, "third-model")
                self.assertEqual(observe.call_count, 2)
                local.complete.assert_called_once()
                self.assertEqual(third.complete.call_count, 2)

    def test_missing_cloud_credentials_skip_disclosure_and_attempt(self):
        local, third = self.navigation_providers()
        local.complete.return_value = CompletionResponse("not JSON", "local-model")
        groq, calls = self.make_adapter(GroqChainReasoningAdapter, [])
        notice = Mock()
        with patch.dict(os.environ, {"GROQ_API_KEY": " "}), \
             patch.object(browser_navigation, "observe_page", return_value=self.page):
            session = self.make_session(local, groq, third)
            result = session.handle_text("/navegar https://example.com Read", on_reasoning_notice=notice)
        self.assertEqual((result.kind, result.text, result.notices), ("navigation", "third answer", ()))
        self.assertEqual(calls, [])
        notice.assert_not_called()

    def test_navigation_without_callback_emits_notice_before_cloud_send(self):
        local, third = self.navigation_providers()
        local.complete.return_value = CompletionResponse("not JSON", "local-model")
        groq, _ = self.make_adapter(GroqChainReasoningAdapter, [])
        session = self.make_session(local, groq, third)
        events = []

        def cloud(**_kwargs):
            events.append("send")
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"answer":"from cloud"}'), finish_reason="stop")])

        with patch.object(browser_navigation, "observe_page", return_value=self.page), \
             patch.object(groq._client.chat.completions, "create", side_effect=cloud), \
             patch.object(ada.console, "print", side_effect=lambda message: events.append(str(message))):
            result = session.handle_text("/navegar https://example.com Read")
        self.assertEqual((result.kind, result.text), ("navigation", "from cloud"))
        self.assertEqual(events, [*result.notices, "send"])
        self.assertEqual(len(result.notices), 1)

    def test_navigation_without_notice_sink_skips_cloud(self):
        local, third = self.navigation_providers()
        local.complete.return_value = CompletionResponse("not JSON", "local-model")
        groq, calls = self.make_adapter(GroqChainReasoningAdapter, [])
        session = self.make_session(local, groq, third)
        with patch.object(session, "_navigation_notice", None), \
             patch.object(browser_navigation, "observe_page", return_value=self.page):
            result = session.handle_text("/navegar https://example.com Read")
        self.assertEqual((result.kind, result.text, result.notices), ("navigation", "third answer", ()))
        self.assertEqual(calls, [])
        third.complete.assert_called_once()

    def test_terminal_browser_or_provider_interrupt_does_not_start_a_backup(self):
        local, third = self.navigation_providers()
        groq, calls = self.make_adapter(GroqChainReasoningAdapter, [])
        session = self.make_session(local, groq, third)
        with patch.object(browser_navigation, "observe_page") as observe:
            denied = session.handle_text("/navegar https://127.0.0.1 Read")
            self.assertEqual(denied.kind, "error")
            self.assertIn("dominio autorizado", denied.text)
            observe.assert_not_called()
            for failure in (
                KeyboardInterrupt(),
                browser_navigation.BrowserError("DNS rechazado", code="permission_denied"),
                browser_navigation.BrowserError("Microsoft Edge no está instalado", code="dependency_missing"),
                browser_navigation.BrowserError("No se pudo cargar la página"),
            ):
                observe.side_effect = failure
                session = self.make_session(local, groq, third)
                if isinstance(failure, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        session.handle_text("/navegar https://example.com Read")
                else:
                    result = session.handle_text("/navegar https://example.com Read")
                    self.assertEqual(result.kind, "error")
                    self.assertIn(str(failure), result.text)
            local.complete.assert_not_called()
            observe.side_effect = None
            observe.return_value = self.page
            local.complete.side_effect = KeyboardInterrupt
            session = self.make_session(local, groq, third)
            with self.assertRaises(KeyboardInterrupt):
                session.handle_text("/navegar https://example.com Read")
        self.assertEqual(calls, [])
        third.complete.assert_not_called()

    def test_all_failures_keep_concrete_provider_causes(self):
        local, third = self.navigation_providers()
        local.complete.side_effect = CapabilityError(
            ErrorCode.NETWORK_UNAVAILABLE, CapabilityKind.REASONING, "Ollama is offline")
        third.complete.return_value = CompletionResponse('{"answer":false}', "third-model")
        groq, calls = self.make_adapter(GroqChainReasoningAdapter, [(429, None, None)] * 3)
        session = self.make_session(local, groq, third)
        with patch.object(browser_navigation, "observe_page", return_value=self.page) as observe, \
             patch.object(ada.console, "print"):
            result = session.handle_text("/navegar https://example.com Read")
        self.assertEqual(result.kind, "error")
        for cause in ("Ollama is offline", "quota_exceeded", "modelo local", "reasoning.third"):
            self.assertIn(cause, result.text)
        self.assertEqual(len(calls), 3)
        observe.assert_called_once()


class VoiceAndBrowserAdapterConformanceTests(unittest.TestCase):
    def test_registering_and_probing_voice_and_browser_adapters_never_imports_audio_or_browser_packages(self):
        forbidden = {"faster_whisper", "pyaudio", "numpy", "pygame", "edge_tts",
                     "pyttsx3", "browser_use", "playwright", "voice"}
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in forbidden:
                raise AssertionError(f"Unexpected optional import: {name}")
            return original_import(name, *args, **kwargs)

        with patch.dict(sys.modules), patch("builtins.__import__", guarded_import), patch(
            "subprocess.Popen", side_effect=AssertionError("Unexpected worker launch"),
        ), patch("importlib.util.find_spec", return_value=None), patch.object(
            browser_navigation, "worker_python",
            side_effect=browser_navigation.BrowserError("Missing .venv-browser"),
        ) as worker:
            for name in list(sys.modules):
                if name == "ada_adapters" or name.startswith("ada_adapters."):
                    del sys.modules[name]
            adapters = importlib.import_module("ada_adapters")
            registry = ExtensionRegistry()
            adapters.register_default_adapters(registry)
            self.assertEqual({d.extension_id for d in registry.diagnostics()}, {
                "reasoning.groq", "reasoning.ollama_local", "memory.sqlite", "stt.faster_whisper",
                "tts.edge_with_local_fallback", "navigation.browser_use_edge",
            })
            worker.assert_not_called()
            for adapter_type in (adapters.WhisperPushToTalkAdapter,
                                 adapters.EdgeLocalTTSAdapter,
                                 adapters.BrowserUseNavigationAdapter):
                self.assertEqual(adapter_type().probe().state,
                                 ExtensionState.UNAVAILABLE_DEPENDENCY)
            worker.assert_called_once_with()
            local = ExtensionRegistry()
            adapters.register_default_adapters(local, provider="local")
            self.assertIn("reasoning.ollama_local", {d.extension_id for d in local.diagnostics()})

    def test_browser_adapter_maps_url_guard_missing_venv_and_timeout_to_exact_error_codes(self):
        from ada_adapters import BrowserUseNavigationAdapter, register_default_adapters

        adapter = BrowserUseNavigationAdapter()
        self.assertIsInstance(adapter, WebNavigationPort)
        self.assertEqual(adapter.manifest.execution_model, ExecutionModel.SUBPROCESS_JOB)
        self.assertTrue(adapter.manifest.permissions.allow_subprocess)
        with patch("subprocess.Popen", side_effect=AssertionError("Unexpected launch")), patch.object(
            browser_navigation, "worker_python",
            side_effect=browser_navigation.BrowserError(
                "Prepara .venv-browser e instala requirements-browser.txt; consulta README."),
        ):
            for url, code in (("http://localhost", ErrorCode.PERMISSION_DENIED),
                              ("https://example.com", ErrorCode.DEPENDENCY_MISSING)):
                with self.subTest(url=url), self.assertRaises(CapabilityError) as caught:
                    adapter.read_public_url(url, "Read the page")
                self.assertEqual(caught.exception.code, code)
            self.assertEqual(adapter.probe().state, ExtensionState.UNAVAILABLE_DEPENDENCY)

        cases = (
            ("Usa una URL HTTPS pública, sin credenciales y del dominio autorizado.",
             ErrorCode.PERMISSION_DENIED),
            ("La navegación agotó su tiempo y se detuvo; no se reintentó.", ErrorCode.TIMEOUT),
            ("El modelo local no completó la tarea. Comprueba ADA_BROWSER_MODEL; no se mostró una respuesta fallida.",
             ErrorCode.EXECUTION_FAILED),
        )
        for message, code in cases:
            runner = Mock(side_effect=browser_navigation.BrowserError(message))
            registry = ExtensionRegistry()
            register_default_adapters(registry, browser_runner=runner)
            with self.subTest(code=code), patch.object(browser_navigation, "worker_python"), self.assertRaises(
                CapabilityError,
            ) as caught:
                registry.execute(CapabilityKind.NAVIGATION, lambda a: a.navigate(
                    WebNavigationRequest("https://example.com", "Read the page")))
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.capability, CapabilityKind.NAVIGATION)
            self.assertEqual(caught.exception.extension_id, "navigation.browser_use_edge")
            runner.assert_called_once_with("https://example.com", "Read the page")
        with patch.object(browser_navigation, "run_task", return_value=browser_navigation.BrowserResult(
            "Public text", "https://example.com"),
        ) as runner:
            self.assertEqual(adapter.navigate(WebNavigationRequest("https://example.com", "Read")),
                             WebNavigationResponse("Public text", "https://example.com"))
            runner.assert_called_once_with("https://example.com", "Read")

    def test_stt_adapter_rejects_nonempty_audio_without_initializing_voice_or_recording(self):
        from ada_adapters import WhisperPushToTalkAdapter

        engine = Mock()
        factory = Mock(return_value=engine)
        adapter = WhisperPushToTalkAdapter(voice_factory=factory)

        with self.assertRaises(CapabilityError) as caught:
            adapter.transcribe(b"recorded audio")

        self.assertEqual(caught.exception.code, ErrorCode.EXECUTION_FAILED)
        self.assertEqual(caught.exception.capability, CapabilityKind.STT)
        self.assertEqual(caught.exception.extension_id, "stt.faster_whisper")
        self.assertEqual(str(caught.exception), "Push-to-talk requires an empty audio buffer.")
        factory.assert_not_called()
        engine.listen_manual_push.assert_not_called()

    def test_stt_and_tts_adapters_report_health_and_degrade_independently(self):
        from ada_adapters import EdgeLocalTTSAdapter, WhisperPushToTalkAdapter, register_default_adapters

        available = set()
        engine = Mock()
        engine.listen_manual_push.return_value = "Recognized text"
        engine.speak.return_value = SimpleNamespace(success=True, engine="local", completed_chunks=1,
                                                    stage=None, error=None)
        factory = Mock(return_value=engine)
        stt, tts = WhisperPushToTalkAdapter(voice_factory=factory), EdgeLocalTTSAdapter(voice_factory=factory)
        self.assertIsInstance(stt, SpeechInputPort)
        self.assertIsInstance(tts, SpeechOutputPort)
        self.assertEqual(stt.manifest.optional_packages, ("faster_whisper", "pyaudio", "numpy"))
        self.assertEqual(tts.manifest.optional_packages, ("edge_tts", "pygame", "pyttsx3"))
        with patch("importlib.util.find_spec", side_effect=lambda name: object() if name in available else None):
            for packages, stt_state, tts_state in (
                (set(), ExtensionState.UNAVAILABLE_DEPENDENCY, ExtensionState.UNAVAILABLE_DEPENDENCY),
                ({"pyttsx3"}, ExtensionState.UNAVAILABLE_DEPENDENCY, ExtensionState.DEGRADED),
                ({"edge_tts"}, ExtensionState.UNAVAILABLE_DEPENDENCY, ExtensionState.UNAVAILABLE_DEPENDENCY),
                ({"edge_tts", "pygame"}, ExtensionState.UNAVAILABLE_DEPENDENCY, ExtensionState.READY),
                ({"faster_whisper", "pyaudio", "numpy"}, ExtensionState.READY, ExtensionState.UNAVAILABLE_DEPENDENCY),
            ):
                available.clear()
                available.update(packages)
                self.assertEqual(stt.probe().state, stt_state)
                self.assertEqual(tts.probe().state, tts_state)
            factory.assert_not_called()
            available.clear()
            available.add("pyttsx3")
            registry = ExtensionRegistry()
            register_default_adapters(registry, voice_factory=factory)
            with self.assertRaises(CapabilityError) as caught:
                registry.resolve(CapabilityKind.STT)
            self.assertEqual(caught.exception.code, ErrorCode.DEPENDENCY_MISSING)
            self.assertEqual(registry.execute(CapabilityKind.TTS, lambda a: a.speak("Hello")),
                             SpeechOutputReport(True, "local", 1))
            engine.speak.assert_called_once_with("Hello")
            states = {d.capability: d.state for d in registry.diagnostics()}
            self.assertEqual(states[CapabilityKind.STT], ExtensionState.UNAVAILABLE_DEPENDENCY)
            self.assertNotEqual(states[CapabilityKind.TTS], ExtensionState.UNAVAILABLE_DEPENDENCY)
            available.update(stt.manifest.optional_packages)
            registry.enable(stt.manifest.extension_id)
            with patch.object(WhisperPushToTalkAdapter, "start_capture"), \
                 patch.object(WhisperPushToTalkAdapter, "stop_capture"), \
                 patch.object(WhisperPushToTalkAdapter, "poll_capture",
                              side_effect=[("recording", ""), ("complete", "Recognized text")]), \
                 patch("ada_adapters.voice_adapters.TerminalInput.wait_for_stop"):
                self.assertEqual(registry.execute(CapabilityKind.STT, lambda a: a.transcribe(b"")),
                                 "Recognized text")
            engine.listen_manual_push.assert_not_called()
            engine.speak.return_value = SimpleNamespace(success=False, engine="local", completed_chunks=0,
                                                        stage="playback", error=RuntimeError("Audio failed"))
            with self.assertRaises(CapabilityError):
                registry.execute(CapabilityKind.TTS, lambda a: a.speak("Hello"))
            with patch.object(WhisperPushToTalkAdapter, "start_capture"), \
                 patch.object(WhisperPushToTalkAdapter, "stop_capture"), \
                 patch.object(WhisperPushToTalkAdapter, "poll_capture",
                              side_effect=[("recording", ""), ("complete", "")]), \
                 patch("ada_adapters.voice_adapters.TerminalInput.wait_for_stop"):
                self.assertEqual(registry.execute(CapabilityKind.STT, lambda a: a.transcribe(b"")), "")
            states = {d.capability: d.state for d in registry.diagnostics()}
            self.assertEqual(states[CapabilityKind.STT], ExtensionState.READY)


class SqliteAdapterConformanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="ada-wu06-")
        self.addCleanup(directory.cleanup)
        # Legacy sqlite connection contexts commit/rollback but close only on GC.
        self.addCleanup(gc.collect)
        self.db = Path(directory.name) / "ada_memory.db"
        database = patch.object(memory, "DB_PATH", str(self.db))
        database.start()
        self.addCleanup(database.stop)
        self.adapter = SqliteMemoryAdapter()
        self.adapter.initialize()

    def test_sqlite_memory_adapter_satisfies_memory_port_and_reuses_selection(self):
        self.assertIsInstance(self.adapter, MemoryPort)
        self.assertEqual(self.adapter.manifest.extension_id, "memory.sqlite")
        self.assertEqual(self.adapter.manifest.capability, CapabilityKind.MEMORY)
        item = MemoryItem("profile", "favorite: color", "blue: [Tema: sky]")
        self.assertIsNone(self.adapter.save(item))
        self.assertEqual(self.adapter.recall("color"), (item,))
        self.assertEqual(self.adapter.recall("unrelated"), ())
        self.assertEqual(self.adapter.recall(""), ())
        self.adapter.delete("different", item.key)
        self.assertEqual(self.adapter.recall("color"), (item,))
        self.assertIsNone(self.adapter.delete(item.category, item.key))
        self.assertEqual(self.adapter.recall("color"), ())

    def test_sqlite_memory_adapter_preserves_db_hash_on_invalid_input_or_sqlite_trigger_abort(self):
        self.adapter.save(MemoryItem("profile", "color", "blue"))
        calls = [
            lambda: self.adapter.save(MemoryItem("profile", "", "red")),
            lambda: self.adapter.save(MemoryItem("", "color", "red")),
            lambda: self.adapter.save(MemoryItem("profile", "color", "")),
            lambda: self.adapter.delete("profile", ""),
            lambda: self.adapter.delete("", "color"),
        ]
        # Exercise triggers separately: an INSERT trigger can mask the UPDATE
        # trigger of an upsert, so a combined trigger set would not prove both.
        for event, call in (
            ("INSERT", lambda: self.adapter.save(MemoryItem("profile", "new", "red"))),
            ("UPDATE", lambda: self.adapter.save(MemoryItem("profile", "color", "red"))),
            ("DELETE", lambda: self.adapter.delete("profile", "color")),
        ):
            with sqlite3.connect(self.db) as connection:
                connection.execute(f"""CREATE TRIGGER reject_operation BEFORE {event} ON memories
                    BEGIN SELECT RAISE(ABORT, 'rejected'); END""")
            before = hashlib.sha256(self.db.read_bytes()).digest()
            with self.subTest(trigger=event), self.assertRaises(CapabilityError):
                call()
            self.assertEqual(hashlib.sha256(self.db.read_bytes()).digest(), before)
            self.assertEqual(memory.get_all_memories(), [("profile", "color", "blue")])
            with sqlite3.connect(self.db) as connection:
                connection.execute("DROP TRIGGER reject_operation")
        for index, call in enumerate(calls):
            with self.subTest(operation=index):
                before = hashlib.sha256(self.db.read_bytes()).digest()
                with self.assertRaises(CapabilityError) as caught:
                    call()
                self.assertEqual(caught.exception.capability, CapabilityKind.MEMORY)
                self.assertEqual(caught.exception.code, ErrorCode.EXECUTION_FAILED)
                self.assertEqual(caught.exception.extension_id, "memory.sqlite")
                self.assertFalse(caught.exception.side_effect_uncertain)
                self.assertEqual(hashlib.sha256(self.db.read_bytes()).digest(), before)
                self.assertEqual(memory.get_all_memories(), [("profile", "color", "blue")])

    def test_sqlite_recall_preserves_unicode_separators_order_and_budget(self):
        for index, separator in enumerate(("\u2028", "\u2029", "\x85", "\v", "\f")):
            item = MemoryItem("profile", f"favorite{index}", f"blue{separator}green")
            self.adapter.save(item)
            self.assertEqual(self.adapter.recall(item.key), (item,))
        for index in range(12):
            self.adapter.save(MemoryItem("topic", f"item{index}", "match " + "x" * 100))
        selected = self.adapter.recall("match")
        self.assertTrue(selected)
        self.assertLessEqual(len(selected), memory.MAX_PROMPT_MEMORIES)
        self.assertEqual([item.key for item in selected],
                         [f"item{index}" for index in range(11, 11 - len(selected), -1)])
        self.assertLessEqual(len(memory.select_memories_for_prompt("match")),
                             memory.MAX_MEMORY_CONTEXT_CHARS)

    def test_sqlite_adapter_translates_read_and_initialization_errors(self):
        for helper, call in (
            ("init_db", self.adapter.initialize),
            ("get_all_memories", lambda: self.adapter.recall("color")),
            ("select_memories_for_prompt", lambda: self.adapter.recall("color")),
        ):
            with self.subTest(helper=helper), patch.object(
                memory, helper, side_effect=sqlite3.OperationalError("unavailable"),
            ):
                with self.assertRaises(CapabilityError) as caught:
                    call()
                self.assertEqual(caught.exception.code, ErrorCode.EXECUTION_FAILED)
