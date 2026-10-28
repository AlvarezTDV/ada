import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import test_optional_audio as audio_fixture


class VoiceFallbackTests(unittest.TestCase):
    def setUp(self):
        self.modules = {name: MagicMock() for name in audio_fixture.AUDIO_MODULES}
        modules = patch.dict("sys.modules", self.modules)
        modules.start()
        self.addCleanup(modules.stop)
        self.voice = audio_fixture.load_module("fallback_voice", "voice.py")
        self.voice.console = MagicMock()
        self.engine = self.voice.VoiceEngine()
        self.directory = tempfile.TemporaryDirectory(prefix="ada-voice-test-")
        self.addCleanup(self.directory.cleanup)
        create_file = tempfile.NamedTemporaryFile
        temporary = patch.object(
            self.voice.tempfile, "NamedTemporaryFile",
            side_effect=lambda **kwargs: create_file(dir=self.directory.name, **kwargs),
        )
        temporary.start()
        self.addCleanup(temporary.stop)
        self.music = self.modules["pygame"].mixer.music
        self.music.get_busy.return_value = False
        self.generated = []

        async def save(filename):
            self.generated.append(filename)
            Path(filename).write_bytes(b"synthetic audio")

        self.save = self.modules["edge_tts"].Communicate.return_value.save = AsyncMock(
            side_effect=save
        )
        self.local = self.modules["pyttsx3"].init.return_value

    def assert_no_audio_files(self):
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_success_reports_completed_chunks_without_using_local_engine(self):
        result = self.engine.speak("**Primera.** Segunda. Tercera.")
        self.assertTrue(result.success)
        self.assertEqual((result.engine, result.completed_chunks), ("edge", 3))
        self.assertEqual(self.music.play.call_count, 3)
        self.modules["pyttsx3"].init.assert_not_called()
        self.assert_no_audio_files()

    def test_generation_failure_resumes_from_first_unplayed_chunk(self):
        # The successful generation must create audio, not just return normally.
        async def save(filename):
            if self.generated:
                raise RuntimeError("generation failed")
            self.generated.append(filename)
            Path(filename).write_bytes(b"synthetic audio")
        self.save.side_effect = save
        result = self.engine.speak("Primera. Segunda. Tercera.")
        self.local.say.assert_called_once_with("Segunda. Tercera.")
        self.assertTrue(result.success)
        self.assertEqual((result.engine, result.completed_chunks), ("local", 3))
        self.assertEqual(self.music.play.call_count, 1)
        self.assert_no_audio_files()

    def test_playback_failure_retries_only_interrupted_and_pending_chunks(self):
        self.music.get_busy.side_effect = [False, RuntimeError("device lost")]
        result = self.engine.speak("Primera. Segunda. Tercera.")
        self.local.say.assert_called_once_with("Segunda. Tercera.")
        self.assertTrue(result.success)
        self.assertEqual(result.completed_chunks, 3)
        self.assertEqual(self.music.play.call_count, 2)
        self.assertEqual(self.music.stop.call_count, 2)
        self.assert_no_audio_files()

    def test_generation_without_audio_is_failure_even_without_exception(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.local.say.reset_mock()
                async def save(filename):
                    if missing:
                        Path(filename).unlink()
                self.save.side_effect = save
                result = self.engine.speak("Hola.")
                self.local.say.assert_called_once_with("Hola.")
                self.assertTrue(result.success)
                self.assert_no_audio_files()

    def test_load_and_start_errors_activate_local_fallback(self):
        for operation in (self.music.load, self.music.play):
            with self.subTest(operation=operation):
                self.local.say.reset_mock()
                operation.side_effect = RuntimeError("playback failed")
                result = self.engine.speak("Primera. Segunda.")
                operation.side_effect = None
                self.local.say.assert_called_once_with("Primera. Segunda.")
                self.assertTrue(result.success)
                self.assert_no_audio_files()

    def test_mixer_initialization_failure_uses_local_engine(self):
        mixer = self.modules["pygame"].mixer
        mixer.get_init.return_value = False
        mixer.init.side_effect = RuntimeError("no playback device")
        result = self.engine.speak("Hola.")
        self.assertTrue(result.success)
        self.local.say.assert_called_once_with("Hola.")
        self.save.assert_not_awaited()

    def test_edge_failure_result_identifies_stage_and_confirmed_progress(self):
        self.music.play.side_effect = [None, RuntimeError("playback failed")]
        result = asyncio.run(self.engine._speak_edge(["Primera.", "Segunda."]))
        self.assertFalse(result.success)
        self.assertEqual((result.stage, result.completed_chunks), ("playback", 1))
        self.assertIsInstance(result.error, RuntimeError)
        self.assert_no_audio_files()

    def test_failed_prefetch_does_not_interrupt_current_playback(self):
        events = []
        async def save(filename):
            if events:
                events.append("prefetch failed")
                raise RuntimeError("generation failed")
            events.append("first generated")
            Path(filename).write_bytes(b"synthetic audio")
        self.save.side_effect = save
        self.music.play.side_effect = lambda: events.append("played")
        self.local.say.side_effect = lambda text: events.append("local")
        result = self.engine.speak("Primera. Segunda.")
        self.assertTrue(result.success)
        self.assertEqual(self.music.play.call_count, 1)
        self.local.say.assert_called_once_with("Segunda.")
        self.assertLess(events.index("played"), events.index("local"))
        self.assertIn("prefetch failed", events)
        self.assert_no_audio_files()

    def test_cleanup_failure_does_not_repeat_completed_audio(self):
        self.music.unload.side_effect = RuntimeError("unload failed")
        result = self.engine.speak("Primera. Segunda.")
        self.assertTrue(result.success)
        self.assertEqual(result.completed_chunks, 2)
        self.modules["pyttsx3"].init.assert_not_called()
        self.assert_no_audio_files()

    def test_total_failure_reports_failure_and_retains_confirmed_progress(self):
        self.music.play.side_effect = [None, RuntimeError("playback failed")]
        self.local.runAndWait.side_effect = RuntimeError("local failed")
        result = self.engine.speak("Primera. Segunda. Tercera.")
        self.assertFalse(result.success)
        self.assertEqual((result.engine, result.completed_chunks), ("local", 1))
        self.assertEqual(result.stage, "playback")
        self.local.say.assert_called_once_with("Segunda. Tercera.")
        self.local.stop.assert_called_once()
        self.voice.console.print.assert_any_call(
            "[red]Salida de voz no disponible; puedes continuar por texto: local failed[/red]"
        )
        self.assert_no_audio_files()

    def test_local_mode_and_empty_input_do_not_start_edge(self):
        self.engine.mode = "local"
        result = self.engine.speak("**Hola.**")
        self.assertTrue(result.success)
        self.local.say.assert_called_once_with("Hola.")
        self.modules["edge_tts"].Communicate.assert_not_called()
        self.local.say.reset_mock()
        for text in ("", "**"):
            result = self.engine.speak(text)
            self.assertTrue(result.success)
            self.assertEqual(result.completed_chunks, 0)
        self.local.say.assert_not_called()

    def test_cancelled_generation_is_not_converted_into_local_fallback(self):
        self.save.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.engine.speak("Primera. Segunda.")
        self.modules["pyttsx3"].init.assert_not_called()
        self.assert_no_audio_files()

    def test_pending_prefetch_settles_before_local_fallback(self):
        settled = []
        async def save(filename):
            self.generated.append(filename)
            if len(self.generated) == 2:
                await asyncio.sleep(0.03)
                settled.append(True)
            Path(filename).write_bytes(b"audio")
        self.save.side_effect = save
        self.music.play.side_effect = RuntimeError("device lost")

        def local(text):
            self.assertEqual(text, "First. Second.")
            self.assertEqual(settled, [True])
            self.assert_no_audio_files()
        self.local.say.side_effect = local
        self.assertTrue(self.engine.speak("First. Second.").success)
        self.local.say.assert_called_once()

    def test_cancel_during_playback_waits_for_cleanup(self):
        import threading
        entered = threading.Event()
        release = threading.Event()

        def play():
            entered.set()
            if not release.wait(5):
                raise AssertionError("release timed out")
        self.music.play.side_effect = play

        async def scenario():
            task = asyncio.create_task(self.engine._speak_edge(["First.", "Second."]))
            try:
                for _ in range(200):
                    if entered.is_set() and len(self.generated) == 2:
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(entered.is_set())
                self.assertEqual(len(self.generated), 2)
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(scenario())
        self.local.say.assert_not_called()
        self.music.stop.assert_called_once()
        self.music.unload.assert_called_once()
        self.assert_no_audio_files()

    def test_cancel_waiting_for_prefetch_removes_partial_file(self):
        async def save(filename):
            self.generated.append(filename)
            Path(filename).write_bytes(b"audio")
            if len(self.generated) == 2:
                await asyncio.Event().wait()
        self.save.side_effect = save

        async def scenario():
            task = asyncio.create_task(self.engine._speak_edge(["First.", "Second."]))
            for _ in range(200):
                if self.music.unload.called and len(self.generated) == 2:
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(self.music.unload.called)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(scenario())
        self.local.say.assert_not_called()
        self.assert_no_audio_files()


if __name__ == "__main__":
    unittest.main()
