import builtins
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from ada_core import ExtensionState, HealthReport

ROOT = Path(__file__).resolve().parents[1]
AUDIO_MODULES = ("faster_whisper", "pyaudio", "numpy", "pygame", "edge_tts", "pyttsx3")


def load_module(name, filename, voice=None):
    missing = object()
    previous = sys.modules.get("voice", missing)
    try:
        if voice is not None:
            sys.modules["voice"] = voice
        spec = importlib.util.spec_from_file_location(name, ROOT / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is missing:
            sys.modules.pop("voice", None)
        else:
            sys.modules["voice"] = previous


class OptionalAudioTests(unittest.TestCase):
    def setUp(self):
        self.modules = {name: MagicMock() for name in AUDIO_MODULES}
        for name, module in self.modules.items():
            module.__spec__ = importlib.util.spec_from_loader(name, loader=None)
            previous = sys.modules.get(name)
            existed = name in sys.modules
            sys.modules[name] = module
            self.addCleanup(self.restore_module, name, previous, existed)
        import memory
        self.directory = tempfile.TemporaryDirectory(prefix="ada-audio-test-")
        self.addCleanup(self.directory.cleanup)
        database = patch.object(memory, "DB_PATH", Path(self.directory.name) / "memory.db")
        database.start()
        self.addCleanup(database.stop)
        self.voice = load_module("optional_audio_voice", "voice.py")
        self.voice.console = MagicMock()
        self.engine = self.voice.VoiceEngine()
        from stt_worker import MicrophoneCapture
        self.capture = MicrophoneCapture()

    def capture_once(self):
        try:
            self.capture.start()
        finally:
            self.capture.stop()
        return self.capture.transcribe()

    @staticmethod
    def restore_module(name, previous, existed):
        if existed:
            sys.modules[name] = previous
        else:
            sys.modules.pop(name, None)

    def prepare_recording(self):
        audio = self.modules["pyaudio"]
        device = audio.PyAudio.return_value
        stream = device.open.return_value
        model = self.modules["faster_whisper"].WhisperModel.return_value
        model.transcribe.return_value = ([types.SimpleNamespace(text=" hola ")], None)

        def open_stream(**kwargs):
            callback = kwargs["stream_callback"]
            self.assertEqual(callback(b"\x00\x00", 1, {}, 0), (None, audio.paContinue))
            return stream

        device.open.side_effect = open_stream
        return device, stream, model

    def test_ada_loader_preserves_adapter_module_identity_and_valid_audio_specs(self):
        import ada_adapters
        import ada_adapters.voice_adapters
        modules = (ada_adapters, ada_adapters.voice_adapters)
        ada = load_module("identity_audio_ada", "ada.py", self.voice)
        self.assertIs(ada.register_default_adapters, ada_adapters.register_default_adapters)
        self.assertIs(sys.modules["ada_adapters"], modules[0])
        self.assertIs(sys.modules["ada_adapters.voice_adapters"], modules[1])
        for name in AUDIO_MODULES:
            self.assertIsNotNone(importlib.util.find_spec(name))

    def test_import_and_constructor_do_not_import_audio(self):
        real_import = builtins.__import__

        def without_audio(name, *args, **kwargs):
            if name.split(".")[0] in AUDIO_MODULES:
                raise ImportError("audio intentionally unavailable")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=without_audio):
            voice = load_module("no_audio_voice", "voice.py")
            voice.VoiceEngine()
            ada = load_module("no_audio_ada", "ada.py", voice)
            self.run_chat(ada, ["hola", "salir"])

    def test_first_microphone_use_loads_whisper_once_and_reuses_it(self):
        factory = self.modules["faster_whisper"].WhisperModel
        factory.assert_not_called()
        device, stream, model = self.prepare_recording()
        with patch("builtins.input", side_effect=AssertionError("Worker must not read terminal")):
            self.assertEqual(self.capture_once(), "hola")
            self.assertEqual(self.capture_once(), "hola")
        factory.assert_called_once_with("base", device="cpu", compute_type="int8")
        self.assertEqual(model.transcribe.call_count, 2)
        self.assertEqual(stream.close.call_count, 2)
        self.assertEqual(device.terminate.call_count, 2)
        self.modules["pygame"].mixer.init.assert_not_called()
        self.modules["pyttsx3"].init.assert_not_called()

    def test_failed_first_load_can_be_retried_and_then_cached(self):
        device, stream, model = self.prepare_recording()
        factory = self.modules["faster_whisper"].WhisperModel
        factory.side_effect = [RuntimeError("first load failed"), model]
        with self.assertRaisesRegex(RuntimeError, "first load failed"):
            self.capture_once()
        self.assertIsNone(self.capture.model)
        self.assertEqual(self.capture_once(), "hola")
        self.assertEqual(self.capture_once(), "hola")
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(stream.close.call_count, 2)
        self.assertEqual(device.terminate.call_count, 2)

    def test_transcription_iterator_failure_preserves_audio_cleanup(self):
        device, stream, model = self.prepare_recording()

        def failed_segments():
            raise RuntimeError("decode failed")
            yield

        model.transcribe.return_value = (failed_segments(), None)
        with self.assertRaisesRegex(RuntimeError, "decode failed"):
            self.capture_once()
        stream.close.assert_called_once()
        device.terminate.assert_called_once()

    def test_microphone_failures_notify_and_release_resources(self):
        for failure in ("import", "model", "device", "open", "transcribe", "stop", "close", "terminate"):
            with self.subTest(failure=failure):
                from stt_worker import MicrophoneCapture
                self.capture = MicrophoneCapture()
                device, stream, model = self.prepare_recording()
                self.voice.console.reset_mock()
                for mock in (device.open, model.transcribe, stream.stop_stream,
                             stream.close, device.terminate, self.modules["pyaudio"].PyAudio,
                             self.modules["faster_whisper"].WhisperModel):
                    mock.reset_mock()
                    mock.side_effect = None
                self.prepare_recording()
                targets = {
                    "model": self.modules["faster_whisper"].WhisperModel,
                    "device": self.modules["pyaudio"].PyAudio,
                    "open": device.open, "transcribe": model.transcribe,
                    "stop": stream.stop_stream, "close": stream.close,
                    "terminate": device.terminate,
                }
                if failure in targets:
                    targets[failure].side_effect = RuntimeError("audio unavailable")
                with patch.dict("sys.modules", {"pyaudio": None} if failure == "import" else {}), \
                     self.assertRaises((RuntimeError, ImportError)):
                    self.capture_once()
                if failure in {"open", "transcribe", "stop", "close", "terminate"}:
                    device.terminate.assert_called_once()
                if failure in {"transcribe", "stop", "close", "terminate"}:
                    stream.close.assert_called_once()

    def test_speech_is_lazy_independent_and_falls_back_locally(self):
        self.modules["pyttsx3"].init.assert_not_called()
        self.modules["pygame"].mixer.init.assert_not_called()
        with patch.dict("sys.modules", {"edge_tts": None}):
            self.engine.speak("**Hola**")
        self.modules["pyttsx3"].init.return_value.say.assert_called_once_with("Hola")
        self.modules["faster_whisper"].WhisperModel.assert_not_called()
        self.modules["pyaudio"].PyAudio.assert_not_called()

    def test_total_speech_failure_notifies_without_raising(self):
        with patch.dict("sys.modules", {"edge_tts": None, "pyttsx3": None}):
            self.engine.speak("Hola")
        self.voice.console.print.assert_called()

    def test_edge_speech_initializes_only_playback_and_cleans_prefetch_on_failure(self):
        pygame = self.modules["pygame"]
        pygame.mixer.get_init.return_value = None
        communicate = self.modules["edge_tts"].Communicate.return_value
        communicate.save = AsyncMock()
        pygame.mixer.music.load.side_effect = RuntimeError("playback unavailable")
        with patch.object(self.voice.tempfile, "NamedTemporaryFile") as temporary, \
             patch.object(self.voice.os.path, "getsize", return_value=1), \
             patch.object(self.voice.os, "remove") as remove:
            temporary.return_value.name = "mock-audio.mp3"
            self.engine.speak("Hola. Otra frase.")
        pygame.mixer.init.assert_called_once()
        self.assertEqual(communicate.save.await_count, 2)
        self.assertEqual(remove.call_count, 2)
        self.modules["pyttsx3"].init.return_value.say.assert_called_once()
        self.modules["faster_whisper"].WhisperModel.assert_not_called()

    def test_edge_generation_failure_cleans_file_and_uses_local_speech(self):
        self.modules["edge_tts"].Communicate.return_value.save = AsyncMock(
            side_effect=RuntimeError("service unavailable")
        )
        with patch.object(self.voice.tempfile, "NamedTemporaryFile") as temporary, \
             patch.object(self.voice.os, "remove") as remove:
            temporary.return_value.name = "mock-audio.mp3"
            self.engine.speak("Hola")
        remove.assert_called_once_with("mock-audio.mp3")
        self.modules["pyttsx3"].init.return_value.say.assert_called_once_with("Hola")

    def test_capture_failure_returns_after_stop_without_an_extra_keyboard_reader(self):
        device, stream, model = self.prepare_recording()
        audio = self.modules["pyaudio"]

        def failed_stream(**kwargs):
            self.assertEqual(kwargs["stream_callback"](b"", 0, {}, 1), (None, audio.paAbort))
            return stream

        device.open.side_effect = failed_stream
        with self.assertRaisesRegex(RuntimeError, "Audio capture failed"):
            self.capture_once()
        stream.close.assert_called_once()
        device.terminate.assert_called_once()
        model.transcribe.assert_not_called()

    def run_chat(self, ada, inputs, speech_enabled=False):
        client = MagicMock()
        client.chat.completions.create.return_value.choices = [
            types.SimpleNamespace(message=types.SimpleNamespace(content="Respuesta escrita"), finish_reason="stop")
        ]
        with patch.dict("sys.modules", {"ada": ada}), \
             patch("ada_adapters.memory_sqlite.SqliteMemoryAdapter.recall", return_value=()), \
             patch("ada_adapters.voice_adapters._available", return_value=True), \
             patch.object(ada, "get_client", return_value=(client, "test")), \
             patch.object(ada, "get_system_prompt", return_value="system"), \
             patch.object(ada, "print_header"), \
             patch.object(ada, "print_ada_response") as output, \
             patch.object(ada, "execute_memory_command", return_value=None), \
             patch.object(ada, "console", MagicMock()), \
             patch("builtins.input", side_effect=inputs), \
             patch.object(ada.sys.stdout, "write"):
            ada.chat_loop(speech_enabled=speech_enabled)
        return output

    def test_text_chat_does_not_speak_and_empty_enter_requests_microphone(self):
        ada = load_module("optional_audio_ada", "ada.py", self.voice)
        with patch("ada_adapters.voice_adapters.WhisperPushToTalkAdapter.start_capture"), \
             patch("ada_adapters.voice_adapters.WhisperPushToTalkAdapter.stop_capture"), \
             patch("ada_adapters.voice_adapters.WhisperPushToTalkAdapter.poll_capture",
                   side_effect=[("recording", ""), ("recording", ""), ("recording", ""),
                                ("complete", "hola")]), \
             patch.object(ada, "VoiceEngine") as factory:
            output = self.run_chat(ada, ["hola", "", "", "salir"])
        factory.assert_not_called()
        factory.return_value.speak.assert_not_called()
        self.assertEqual(output.call_count, 3)
        output.assert_any_call("Respuesta escrita", "casual")
        output.assert_any_call("Hasta luego.", "casual")

    def test_audio_failure_does_not_prevent_next_written_turn(self):
        ada = load_module("optional_audio_failure_ada", "ada.py", self.voice)
        from ada_core import CapabilityError, CapabilityKind, ErrorCode
        with patch.dict("sys.modules", {"edge_tts": None, "pyttsx3": None}), \
             patch("ada_adapters.voice_adapters.WhisperPushToTalkAdapter.start_capture",
                   side_effect=CapabilityError(ErrorCode.EXECUTION_FAILED, CapabilityKind.STT,
                                               "No microphone.")), \
             patch.object(ada, "VoiceEngine", self.voice.VoiceEngine):
            output = self.run_chat(ada, ["", "hola", "salir"], speech_enabled=True)
        output.assert_any_call("Respuesta escrita", "casual")
        output.assert_any_call("Hasta luego.", "casual")

    def test_enabled_speech_keeps_memory_command_and_farewell_behavior(self):
        ada = load_module("optional_audio_memory_ada", "ada.py", self.voice)
        client = MagicMock()
        with patch("ada_adapters.voice_adapters._available", return_value=True), \
             patch.object(ada, "get_client", return_value=(client, "test")), \
             patch.object(ada, "get_system_prompt", return_value="system"), \
             patch.object(ada, "print_header"), \
             patch.object(ada, "print_ada_response") as output, \
             patch.object(ada, "console", MagicMock()), \
             patch.object(ada, "execute_memory_command", return_value="Información guardada."), \
             patch.object(ada, "VoiceEngine") as factory, \
             patch("builtins.input", side_effect=["anota algo", "salir"]):
            ada.chat_loop(speech_enabled=True)
        output.assert_any_call("Información guardada.", "task")
        factory.return_value.speak.assert_any_call("Información guardada.")
        factory.return_value.speak.assert_any_call("Hasta luego.")
        client.chat.completions.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
