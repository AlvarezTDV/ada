import builtins
import importlib.util
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
AUDIO_MODULES = ("faster_whisper", "pyaudio", "numpy", "pygame", "edge_tts", "pyttsx3")


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OptionalAudioTests(unittest.TestCase):
    def setUp(self):
        self.modules = {name: MagicMock() for name in AUDIO_MODULES}
        self.module_patch = patch.dict("sys.modules", self.modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.voice = load_module("optional_audio_voice", "voice.py")
        self.voice.console = MagicMock()
        self.engine = self.voice.VoiceEngine()

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

    def test_import_and_constructor_do_not_import_audio(self):
        real_import = builtins.__import__

        def without_audio(name, *args, **kwargs):
            if name.split(".")[0] in AUDIO_MODULES:
                raise ImportError("audio intentionally unavailable")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=without_audio):
            voice = load_module("no_audio_voice", "voice.py")
            voice.VoiceEngine()
            with patch.dict("sys.modules", {"voice": voice}):
                ada = load_module("no_audio_ada", "ada.py")
            self.run_chat(ada, ["hola", "salir"])

    def test_first_microphone_use_loads_whisper_once_and_reuses_it(self):
        factory = self.modules["faster_whisper"].WhisperModel
        factory.assert_not_called()
        device, stream, model = self.prepare_recording()
        with patch("builtins.input", return_value="") as keyboard:
            self.assertEqual(self.engine.listen_manual_push(), "hola")
            self.assertEqual(self.engine.listen_manual_push(), "hola")
        factory.assert_called_once_with("base", device="cpu", compute_type="int8")
        self.assertEqual(model.transcribe.call_count, 2)
        self.assertEqual(keyboard.call_count, 2)
        self.assertEqual(stream.close.call_count, 2)
        self.assertEqual(device.terminate.call_count, 2)
        self.modules["pygame"].mixer.init.assert_not_called()
        self.modules["pyttsx3"].init.assert_not_called()

    def test_failed_first_load_can_be_retried_and_then_cached(self):
        device, stream, model = self.prepare_recording()
        factory = self.modules["faster_whisper"].WhisperModel
        factory.side_effect = [RuntimeError("first load failed"), model]
        with patch("builtins.input", return_value="") as keyboard:
            self.assertEqual(self.engine.listen_manual_push(), "")
            self.assertIsNone(self.engine.whisper_model)
            self.assertEqual(self.engine.listen_manual_push(), "hola")
            self.assertEqual(self.engine.listen_manual_push(), "hola")
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(keyboard.call_count, 2)
        self.assertEqual(stream.close.call_count, 2)
        self.assertEqual(device.terminate.call_count, 2)

    def test_transcription_iterator_failure_preserves_audio_cleanup(self):
        device, stream, model = self.prepare_recording()

        def failed_segments():
            raise RuntimeError("decode failed")
            yield

        model.transcribe.return_value = (failed_segments(), None)
        with patch("builtins.input", return_value=""):
            self.assertEqual(self.engine.listen_manual_push(), "")
        stream.close.assert_called_once()
        device.terminate.assert_called_once()
        self.voice.console.print.assert_called()

    def test_microphone_failures_notify_and_release_resources(self):
        for failure in ("import", "model", "device", "open", "transcribe", "stop", "close", "terminate"):
            with self.subTest(failure=failure):
                self.engine = self.voice.VoiceEngine()
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
                     patch("builtins.input", return_value="") as keyboard:
                    result = self.engine.listen_manual_push()
                if failure not in {"stop", "close", "terminate"}:
                    self.assertEqual(result, "")
                self.voice.console.print.assert_called()
                if failure in {"import", "model", "device", "open"}:
                    keyboard.assert_not_called()
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
        with patch("builtins.input", return_value="") as keyboard:
            self.assertEqual(self.engine.listen_manual_push(), "")
        keyboard.assert_called_once()
        stream.close.assert_called_once()
        device.terminate.assert_called_once()
        model.transcribe.assert_not_called()
        self.voice.console.print.assert_called()

    def run_chat(self, ada, inputs, speech_enabled=False):
        client = MagicMock()
        client.chat.completions.create.return_value.choices = [
            types.SimpleNamespace(message=types.SimpleNamespace(content="Respuesta escrita"))
        ]
        with patch.object(ada.memory, "init_db"), \
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
        with patch.dict("sys.modules", {"voice": self.voice}):
            ada = load_module("optional_audio_ada", "ada.py")
        with patch.object(ada, "VoiceEngine") as factory:
            factory.return_value.listen_manual_push.return_value = "hola"
            output = self.run_chat(ada, ["hola", "", "salir"])
        factory.return_value.listen_manual_push.assert_called_once()
        factory.return_value.speak.assert_not_called()
        self.assertEqual(output.call_count, 3)
        output.assert_any_call("Respuesta escrita", "casual")
        output.assert_any_call("Hasta luego.", "casual")

    def test_audio_failure_does_not_prevent_next_written_turn(self):
        with patch.dict("sys.modules", {"voice": self.voice}):
            ada = load_module("optional_audio_failure_ada", "ada.py")
        self.modules["pyaudio"].PyAudio.side_effect = RuntimeError("no microphone")
        with patch.dict("sys.modules", {"edge_tts": None, "pyttsx3": None}):
            output = self.run_chat(ada, ["", "hola", "salir"], speech_enabled=True)
        output.assert_any_call("Respuesta escrita", "casual")
        output.assert_any_call("Hasta luego.", "casual")

    def test_enabled_speech_keeps_memory_command_and_farewell_behavior(self):
        with patch.dict("sys.modules", {"voice": self.voice}):
            ada = load_module("optional_audio_memory_ada", "ada.py")
        client = MagicMock()
        with patch.object(ada.memory, "init_db"), \
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
