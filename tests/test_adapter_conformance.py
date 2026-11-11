"""Offline port conformance with HTTP doubles and real temporary SQLite files."""

import gc
import builtins
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
    ExecutionModel, ExtensionRegistry, ExtensionState, ReasoningPort,
    SpeechInputPort, SpeechOutputPort, SpeechOutputReport, TurnMode,
    WebNavigationPort, WebNavigationRequest, WebNavigationResponse,
)
from ada_adapters import (
    GroqChainReasoningAdapter, OllamaReasoningAdapter, SqliteMemoryAdapter,
)
import memory
import browser_navigation


CHAIN = "llama-3.3-70b-versatile,openai/gpt-oss-20b,openai/gpt-oss-120b"


class AdapterConformanceTests(unittest.TestCase):
    def setUp(self):
        # The shared voice fixture restores sys.modules, including its ada entry.
        modules = patch.dict("sys.modules", {"ada": ada})
        modules.start()
        self.addCleanup(modules.stop)
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
                "reasoning.groq", "memory.sqlite", "stt.faster_whisper",
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
            self.assertEqual(registry.execute(CapabilityKind.STT, lambda a: a.transcribe(b"")), "Recognized text")
            engine.listen_manual_push.assert_called_once_with()
            engine.speak.return_value = SimpleNamespace(success=False, engine="local", completed_chunks=0,
                                                        stage="playback", error=RuntimeError("Audio failed"))
            with self.assertRaises(CapabilityError):
                registry.execute(CapabilityKind.TTS, lambda a: a.speak("Hello"))
            self.assertEqual(registry.execute(CapabilityKind.STT, lambda a: a.transcribe(b"")), "Recognized text")
            engine.listen_manual_push.return_value = ""
            with self.assertRaises(CapabilityError) as caught:
                stt.transcribe(b"")
            self.assertEqual(caught.exception.code, ErrorCode.EMPTY_RESPONSE)


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
