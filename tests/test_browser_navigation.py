import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from rich.console import Console

import browser_navigation as navigation
from test_memory_prompt import IsolatedPromptFixture, ada


class NavigationTests(unittest.TestCase):
    def test_only_public_https_destinations_are_accepted(self):
        for url in ("http://example.com", "https://localhost", "https://127.0.0.1",
                    "https://192.168.1.1", "file:///secret", "https://user:pass@example.com",
                    "https://example.com:444", "https://example.com\n", "https://host.local",
                    "https://@example.com/", "https://:@example.com/"):
            with self.subTest(url=url), self.assertRaises(navigation.BrowserError):
                navigation.validate_url(url)
        self.assertEqual(navigation.validate_url("https://example.com/path"), "example.com")
        with self.assertRaises(navigation.BrowserError):
            navigation.validate_url("https://other.example", host="example.com")
        with patch.object(navigation.socket, "getaddrinfo", return_value=[
                (2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(navigation.BrowserError):
                navigation.validate_url("https://example.com", resolve=True)

    def test_worker_environment_excludes_credentials_and_cloud_configuration(self):
        with patch.dict(navigation.os.environ, {"OPENAI_API_KEY": "private",
                                               "HTTP_PROXY": "private",
                                               "BROWSER_USE_API_KEY": "private"}):
            env = navigation.worker_environment(Path("job"))
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("BROWSER_USE_API_KEY", env)
        self.assertEqual(env["ANONYMIZED_TELEMETRY"], "false")
        self.assertEqual(env["BROWSER_USE_CLOUD_SYNC"], "false")
        self.assertEqual(env["PYTHON_DOTENV_DISABLED"], "1")

    def test_bounded_worker_returns_only_validated_plain_data(self):
        process = Mock()
        process.communicate.return_value = (
            json.dumps({"ok": True, "text": "[bold]literal[/bold]", "url": "https://example.com"}).encode(), None)
        process.returncode = 0
        with patch.object(navigation, "worker_python", return_value=Path(sys.executable)), \
             patch.object(navigation, "worker_job", return_value=nullcontext()), \
             patch.object(navigation.subprocess, "Popen", return_value=process) as launch:
            result = navigation.run_task("https://example.com", "Read the title")
        self.assertEqual(result.text, "[bold]literal[/bold]")
        self.assertFalse(launch.call_args.kwargs.get("shell", False))
        self.assertEqual(process.communicate.call_args.kwargs["timeout"], navigation.TIMEOUT_SECONDS)
        payload = json.loads(process.communicate.call_args.kwargs["input"])
        self.assertEqual(set(payload), {"url", "task"})

    def test_invalid_tasks_and_missing_environment_never_launch_worker(self):
        with patch.object(navigation.subprocess, "Popen") as launch:
            for task in (" ", "x" * 4001):
                with self.assertRaises(navigation.BrowserError):
                    navigation.run_task("https://example.com", task)
            with patch.object(navigation, "worker_python", side_effect=navigation.BrowserError("Setup required")):
                with self.assertRaises(navigation.BrowserError):
                    navigation.run_task("https://example.com", "Read")
        launch.assert_not_called()

    def test_failed_or_cancelled_worker_is_not_reported_as_success(self):
        for failure in (subprocess.TimeoutExpired("worker", 1), KeyboardInterrupt()):
            process = Mock()
            process.communicate.side_effect = failure
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(navigation, "worker_python", return_value=Path(sys.executable)), \
                 patch.object(navigation, "worker_job", return_value=nullcontext()), \
                 patch.object(navigation.subprocess, "Popen", return_value=process), \
                 patch.object(navigation, "stop_worker") as stop:
                expected = KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else navigation.BrowserError
                with self.assertRaises(expected):
                    navigation.run_task("https://example.com", "Read")
                stop.assert_called_once_with(process)
        for text in ("", "unsafe\rspoof", "unsafe\x1b[31m", "x" * 40001):
            with self.subTest(text=text[:20]), self.assertRaises(navigation.BrowserError):
                navigation.validate_text(text)
        process = Mock(returncode=1)
        process.communicate.return_value = (b'{"ok":false,"error":"model_task_failed"}', None)
        with patch.object(navigation, "worker_python", return_value=Path(sys.executable)), \
             patch.object(navigation, "worker_job", return_value=nullcontext()), \
             patch.object(navigation.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(navigation.BrowserError, "modelo local"):
                navigation.run_task("https://example.com", "Read")

    @unittest.skipUnless(navigation.os.name == "nt", "Windows process-tree ownership")
    def test_worker_job_closes_owned_descendants(self):
        import ctypes
        from ctypes import wintypes

        code = ("import sys,subprocess; sys.stdin.read(1); "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "print(child.pid,flush=True); sys.stdin.read()")
        process = subprocess.Popen([sys.executable, "-B", "-c", code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        handle = None
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            with navigation.worker_job(process):
                process.stdin.write(b"x")
                process.stdin.flush()
                pid = int(process.stdout.readline())
                handle = kernel.OpenProcess(0x100000, False, pid)
                self.assertTrue(handle)
            process.wait(timeout=5)
            self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
            if handle:
                kernel.CloseHandle(handle)


class NavigationRoutingTests(IsolatedPromptFixture):
    def test_empty_userinfo_is_rejected_before_worker_preparation(self):
        self.remember("private memory")
        before = hashlib.sha256(Path(ada.memory.DB_PATH).read_bytes()).digest()
        output = io.StringIO()
        client = Mock()
        with patch.object(ada, "get_client", return_value=(client, "test")), \
             patch.object(ada, "get_system_prompt", return_value="system"), \
             patch.object(ada, "print_header"), \
             patch.object(ada, "console", Console(file=output, force_terminal=False, width=160)), \
             patch.object(ada, "VoiceEngine") as voice, \
             patch.object(navigation, "worker_python", side_effect=navigation.BrowserError("Guard bypassed")) as prepare, \
             patch("builtins.input", side_effect=["/navegar https://@example.com/ Read",
                                                "/navegar https://:@example.com/ Read", "salir"]):
            ada.chat_loop()
        prepare.assert_not_called()
        self.assertEqual(output.getvalue().count("sin credenciales"), 2)
        client.chat.completions.create.assert_not_called()
        voice.return_value.speak.assert_not_called()
        self.assertEqual(hashlib.sha256(Path(ada.memory.DB_PATH).read_bytes()).digest(), before)

    def test_explicit_browser_task_is_isolated_and_normal_chat_is_unchanged(self):
        self.remember("private memory")
        before = hashlib.sha256(Path(ada.memory.DB_PATH).read_bytes()).digest()
        output = io.StringIO()
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="normal answer"))])
        literal = "[bold]untrusted page result[/bold]"
        result = navigation.BrowserResult(literal, "https://example.com")
        with patch.object(ada, "get_client", return_value=(client, "test")), \
             patch.object(ada, "get_system_prompt", return_value="system"), \
             patch.object(ada, "print_header"), \
             patch.object(ada, "console", Console(file=output, force_terminal=False, width=160)), \
             patch.object(ada, "VoiceEngine") as voice, \
             patch.object(ada, "run_browser_task", return_value=result) as run, \
             patch("builtins.input", side_effect=["hola", "/navegar https://example.com Read title", "continue", "salir"]):
            ada.chat_loop()
        run.assert_called_once_with("https://example.com", "Read title")
        self.assertIn(literal, output.getvalue())
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertNotIn(literal, str(client.chat.completions.create.call_args))
        self.assertNotIn("Read title", str(client.chat.completions.create.call_args))
        voice.return_value.speak.assert_not_called()
        self.assertEqual(hashlib.sha256(Path(ada.memory.DB_PATH).read_bytes()).digest(), before)

    def test_missing_arguments_and_browser_failure_do_not_fall_through(self):
        client = Mock()
        with patch.object(ada, "get_client", return_value=(client, "test")), \
             patch.object(ada, "get_system_prompt", return_value="system"), \
             patch.object(ada, "print_header"), patch.object(ada, "console"), \
             patch.object(ada, "VoiceEngine"), \
             patch.object(ada, "run_browser_task", side_effect=navigation.BrowserError("Safe failure")) as run, \
             patch("builtins.input", side_effect=["/navegar", "/navegar https://example.com",
                                                "/navegar https://example.com Read", "salir"]):
            ada.chat_loop()
        run.assert_called_once()
        client.chat.completions.create.assert_not_called()
