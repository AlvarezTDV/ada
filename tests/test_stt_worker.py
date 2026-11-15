"""Offline subprocess regressions for interactive speech isolation."""

import subprocess
import sys
import json
import os
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def fake_worker(mode):
    """Real child process, with deterministic failures and no audio dependencies."""
    count = 0
    child = None
    for line in sys.stdin:
        command = json.loads(line)
        job = command["job_id"]

        def send(kind, **fields):
            print(json.dumps(dict(type=kind, job_id=job, **fields)), flush=True)

        if command["type"] == "start":
            count += 1
            if mode == "init_hang":
                time.sleep(120)
            if mode == "crash":
                os._exit(17)
            if mode == "eof":
                return
            if mode in {"malformed", "oversize", "empty_object", "wrong_type"}:
                print({"malformed": "not-json", "oversize": "x" * 20_000,
                       "empty_object": "{}", "wrong_type": "[]"}[mode], flush=True)
                time.sleep(120)
            if mode == "stale":
                print(json.dumps(dict(type="recording", job_id="old-job")), flush=True)
                time.sleep(120)
            if mode in {"dependency_exit", "execution_exit"}:
                send("initializing")
                send("error", code="dependency_missing" if mode == "dependency_exit" else "execution_failed")
                return
            if mode == "dependency":
                send("error", code="dependency_missing")
                time.sleep(120)
            if mode == "device":
                send("error", code="execution_failed")
                time.sleep(120)
            if mode == "tree":
                child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(120)"])
                Path(sys.argv[3]).write_text(str(child.pid))
            send("initializing")
            send("recording")
            if mode == "recording_crash":
                time.sleep(0.08)
                os._exit(18)
            if mode == "recording_error":
                time.sleep(0.08)
                send("error", code="execution_failed")
        elif command["type"] == "stop":
            if mode == "stop_hang":
                time.sleep(120)
            send("transcribing")
            if mode == "transcription_hang":
                time.sleep(120)
            send("result", text=" \t " if mode == "silence" else f"hola {count}")
        elif command["type"] == "shutdown":
            return


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--fake":
    fake_worker(sys.argv[2])
    sys.exit(0)


import ctypes
import tempfile
import unittest
from unittest.mock import patch


def wait_phase(adapter, target):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        phase, text = adapter.poll_capture()
        if phase == target:
            return text
        time.sleep(0.005)
    raise AssertionError(f"Worker did not reach {target}")


def process_alive(pid):
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        # Orphaned POSIX zombies are dead, awaiting the system reaper.
        status = Path(f"/proc/{pid}/stat")
        return not status.exists() or status.read_text().split()[2] != "Z"
    except ProcessLookupError:
        return False


class SupervisedWorkerTests(unittest.TestCase):
    def adapter(self, mode="normal", **kwargs):
        from ada_adapters.voice_adapters import WhisperPushToTalkAdapter
        adapter = WhisperPushToTalkAdapter(
            worker_command=[sys.executable, "-B", str(Path(__file__).resolve()), "--fake", mode],
            init_timeout=kwargs.pop("init_timeout", 2),
            stop_timeout=0.15, transcription_timeout=0.15, **kwargs)
        self.addCleanup(adapter.shutdown)
        return adapter

    def turn(self, adapter):
        adapter.start_capture()
        wait_phase(adapter, "recording")
        adapter.stop_capture()
        return wait_phase(adapter, "complete")

    def test_success_reuses_process_and_job_ids_change(self):
        adapter = self.adapter()
        self.assertEqual(self.turn(adapter), "hola 1")
        process, identity = adapter.process, adapter._job_id
        self.assertEqual(self.turn(adapter), "hola 2")
        self.assertIs(adapter.process, process)
        self.assertNotEqual(adapter._job_id, identity)
        adapter.shutdown()
        self.assertIsNotNone(process.poll())
        self.assertFalse(adapter._reader)

    def test_user_recording_has_no_thirty_second_deadline(self):
        adapter = self.adapter()
        adapter.start_capture()
        wait_phase(adapter, "recording")
        now = time.monotonic()
        with patch("ada_adapters.voice_adapters.time.monotonic", return_value=now + 31):
            self.assertEqual(adapter.poll_capture(), ("recording", ""))
        adapter.stop_capture()
        self.assertEqual(wait_phase(adapter, "complete"), "hola 1")

    def test_each_native_phase_hang_is_killed_reaped_and_recoverable(self):
        from ada_core import CapabilityError, ErrorCode
        for mode in ("init_hang", "stop_hang", "transcription_hang"):
            with self.subTest(mode=mode):
                adapter = self.adapter(mode, init_timeout=0.4)
                adapter.start_capture()
                process = adapter.process
                with self.assertRaises(CapabilityError) as caught:
                    if mode != "init_hang":
                        wait_phase(adapter, "recording")
                        adapter.stop_capture()
                    wait_phase(adapter, "complete")
                self.assertEqual(caught.exception.code, ErrorCode.TIMEOUT)
                self.assertIsNotNone(process.poll())
                self.assertIsNone(adapter.process)
                adapter.worker_command[-1] = "normal"
                adapter.init_timeout = 2
                self.assertEqual(self.turn(adapter), "hola 1")

    def test_crash_eof_bad_ipc_and_stale_identity_never_deliver_a_result(self):
        from ada_core import CapabilityError, ErrorCode
        for mode in ("crash", "eof", "malformed", "oversize", "empty_object",
                     "wrong_type", "stale", "recording_crash", "recording_error",
                     "dependency", "device"):
            with self.subTest(mode=mode):
                adapter = self.adapter(mode)
                adapter.start_capture()
                process = adapter.process
                with self.assertRaises(CapabilityError) as caught:
                    wait_phase(adapter, "complete")
                expected = ErrorCode.DEPENDENCY_MISSING if mode == "dependency" else ErrorCode.EXECUTION_FAILED
                self.assertEqual(caught.exception.code, expected)
                self.assertIsNotNone(process.poll())
                self.assertIsNone(adapter.process)

    def test_replaced_worker_queue_cannot_deliver_to_new_job(self):
        adapter = self.adapter()
        adapter.start_capture()
        wait_phase(adapter, "recording")
        old_events, old_identity = adapter._events, adapter._job_id
        adapter.cancel_capture()
        adapter.start_capture()
        wait_phase(adapter, "recording")
        old_events.put({"job_id": old_identity, "type": "result", "text": "stale"})
        adapter.stop_capture()
        self.assertEqual(wait_phase(adapter, "complete"), "hola 1")

    def test_structured_error_survives_immediate_worker_exit(self):
        from ada_core import CapabilityError, ErrorCode
        for mode, expected in (("dependency_exit", ErrorCode.DEPENDENCY_MISSING),
                               ("execution_exit", ErrorCode.EXECUTION_FAILED)):
            with self.subTest(mode=mode):
                adapter = self.adapter(mode)
                adapter.start_capture()
                process = adapter.process
                process.wait(timeout=4)
                with self.assertRaises(CapabilityError) as caught:
                    wait_phase(adapter, "complete")
                self.assertEqual(caught.exception.code, expected)
                self.assertIsNone(adapter.process)

    def test_production_error_exit_and_recovery(self):
        from contextlib import closing
        import hashlib
        import sqlite3
        from ada_core import CapabilityError, ErrorCode
        from ada_adapters.voice_adapters import WhisperPushToTalkAdapter

        script = """
import os
import sys
import stt_worker

class Capture:
    def start(self):
        if sys.argv[1] == "dependency":
            raise ImportError("missing probe dependency")
        if sys.argv[1] == "execution":
            raise RuntimeError("probe device failure")
        if sys.argv[1] == "eof":
            os._exit(23)
    def check(self):
        pass
    def stop(self):
        pass
    def transcribe(self):
        return "recovered"

stt_worker.run(Capture())
"""
        cases = (
            ("dependency", ErrorCode.DEPENDENCY_MISSING,
             "Speech dependencies are unavailable."),
            ("execution", ErrorCode.EXECUTION_FAILED,
             "Microphone, cleanup or transcription failed; continue using text."),
            ("eof", ErrorCode.EXECUTION_FAILED,
             "Speech worker exited unexpectedly; continue using text."),
        )
        with tempfile.TemporaryDirectory(prefix="ada-stt-error-") as directory:
            database = Path(directory) / "isolated.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE sentinel(value TEXT)")
                connection.execute("INSERT INTO sentinel VALUES ('unchanged')")
                connection.commit()

            def digest():
                return hashlib.sha256(database.read_bytes()).digest()

            before = digest()
            for mode, expected, message in cases:
                with self.subTest(mode=mode):
                    adapter = WhisperPushToTalkAdapter(
                        worker_command=[sys.executable, "-B", "-c", script, mode],
                        init_timeout=5, stop_timeout=1, transcription_timeout=1)
                    self.addCleanup(adapter.shutdown)
                    adapter.start_capture()
                    process = adapter.process
                    process.stdin.close()
                    self.assertEqual(process.wait(timeout=4), 23 if mode == "eof" else 0)
                    with self.assertRaises(CapabilityError) as caught:
                        wait_phase(adapter, "complete")
                    self.assertEqual(caught.exception.code, expected)
                    self.assertEqual(caught.exception.user_message, message)
                    self.assertIsNone(adapter.process)
                    self.assertFalse(adapter._reader)
                    self.assertEqual(digest(), before)
                    adapter.worker_command = [
                        sys.executable, "-B", str(Path(__file__).resolve()), "--fake", "normal"]
                    adapter.start_capture()
                    recovered = adapter.process
                    wait_phase(adapter, "recording")
                    adapter.stop_capture()
                    self.assertEqual(wait_phase(adapter, "complete"), "hola 1")
                    adapter.shutdown()
                    self.assertIsNotNone(recovered.poll())
                    self.assertEqual(digest(), before)

    def test_unconfirmed_termination_quarantines_without_replacement(self):
        from ada_core import CapabilityError
        adapter = self.adapter()
        adapter.start_capture()
        wait_phase(adapter, "recording")
        process = adapter.process
        try:
            with patch.object(process, "poll", return_value=None), \
                 patch.object(process, "wait", side_effect=subprocess.TimeoutExpired("fake", 0)):
                with self.assertRaisesRegex(CapabilityError, "quarantined"):
                    adapter.cancel_capture()
                with patch("subprocess.Popen") as launch:
                    with self.assertRaisesRegex(CapabilityError, "quarantined"):
                        adapter.start_capture()
                    launch.assert_not_called()
        finally:
            adapter.shutdown()
        self.assertIsNotNone(process.poll())

    def test_shutdown_kills_descendants_without_recording_files(self):
        with tempfile.TemporaryDirectory(prefix="ada-stt-tree-") as directory:
            marker = Path(directory) / "pid"
            adapter = self.adapter("tree")
            adapter.worker_command.append(str(marker))
            adapter.start_capture()
            wait_phase(adapter, "recording")
            process = adapter.process
            child_pid = int(marker.read_text())
            self.assertTrue(process_alive(child_pid))
            adapter.shutdown()
            self.assertIsNotNone(process.poll())
            deadline = time.monotonic() + 2
            while process_alive(child_pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(process_alive(child_pid))
            self.assertEqual([item.name for item in Path(directory).iterdir()], ["pid"])

    def test_compatibility_buffer_rejection_starts_no_worker(self):
        from ada_core import CapabilityError, ErrorCode
        adapter = self.adapter()
        with self.assertRaises(CapabilityError) as caught:
            adapter.transcribe(b"unsupported")
        self.assertEqual(caught.exception.code, ErrorCode.EXECUTION_FAILED)
        self.assertIsNone(adapter.process)

    def test_direct_compatibility_transcribe_supports_patched_input_and_silence(self):
        adapter = self.adapter("silence")
        with patch("builtins.input", return_value="") as keyboard:
            self.assertEqual(adapter.transcribe(b""), "")
        keyboard.assert_called_once()

    def test_production_worker_protocol_caches_model_and_reserves_stdout(self):
        adapter = self.adapter()
        script = r'''
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
sys.path.insert(0, sys.argv[1])
import stt_worker
modules = {name: MagicMock() for name in ("pyaudio", "numpy", "faster_whisper")}
sys.modules.update(modules)
loads = 0
def model(*args, **kwargs):
    global loads
    loads += 1
    assert args == ("base",)
    assert kwargs == dict(device="cpu", compute_type="int8")
    print("third party stdout must not corrupt IPC")
    return SimpleNamespace(transcribe=lambda *a, **kw:
        ([SimpleNamespace(text=f"model-loads:{loads}")], None))
modules["faster_whisper"].WhisperModel.side_effect = model
audio = modules["pyaudio"]
stream = MagicMock()
def open_stream(**kwargs):
    kwargs["stream_callback"](b"\x00\x00", 1, {}, 0)
    return stream
audio.PyAudio.return_value.open.side_effect = open_stream
stt_worker.run()
'''
        adapter.worker_command = [sys.executable, "-B", "-c", script, str(ROOT)]
        self.assertEqual(self.turn(adapter), "model-loads:1")
        process = adapter.process
        self.assertEqual(self.turn(adapter), "model-loads:1")
        self.assertIs(adapter.process, process)

    def test_worker_failure_is_detected_while_no_stop_enter_is_available(self):
        from ada_core import CapabilityError
        from ada_adapters.voice_adapters import TerminalInput
        adapter = self.adapter("recording_crash")
        adapter.start_capture()
        wait_phase(adapter, "recording")
        if os.name == "nt":
            unavailable = patch("msvcrt.kbhit", return_value=False)
        else:
            unavailable = patch("select.select", return_value=([], [], []))
        with unavailable, patch.object(sys.stdin, "isatty", return_value=True), \
             self.assertRaises(CapabilityError):
            TerminalInput().wait_for_stop(adapter.poll_capture)
        self.assertIsNone(adapter.process)
        with patch("builtins.input", return_value="written once") as keyboard:
            self.assertEqual(TerminalInput().read_line("> "), "written once")
        keyboard.assert_called_once()


class HangRegressionTests(unittest.TestCase):
    def test_registry_timeout_does_not_wait_for_abandoned_recording(self):
        # An outer process makes the old indefinite finished.wait() safe to test.
        script = r'''
from threading import Event
from ada_core import *
from ada_core.orchestrator import SessionOrchestrator
registry = ExtensionRegistry()
class Stuck:
    def transcribe(self, audio):
        Event().wait()
registry.register(ExtensionManifest("test.stt", CapabilityKind.STT,
                                  SideEffectProfile.PURE_READ), Stuck)
execute = registry.execute
registry.execute = lambda kind, fn, **kw: execute(kind, fn, timeout_s=0.02)
session = SessionOrchestrator(registry, prompt_factory=lambda _: "system",
                             classify_input=lambda _: "casual",
                             memory_command=lambda *a: None)
assert session.listen().kind == "error"
assert session.handle_text("/estado").kind == "status"
assert session.handle_text("salir").kind == "exit"
'''
        try:
            result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT,
                                    capture_output=True, text=True, timeout=2)
        except subprocess.TimeoutExpired:
            self.fail("STT timeout still waits indefinitely for the abandoned call")
        self.assertEqual(result.returncode, 0, result.stderr)


PUBLIC_INPUT_PROBE = r'''
import copy
import hashlib
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import ada
import memory
from ada_core import (
    CapabilityKind, ExtensionManifest, ExtensionRegistry, HealthReport,
    ExtensionState, SideEffectProfile,
)
from ada_core.orchestrator import SessionOrchestrator
from ada_adapters.voice_adapters import WhisperPushToTalkAdapter, TerminalInput

mode, database = sys.argv[1:]
memory.DB_PATH = database
with sqlite3.connect(database) as connection:
    connection.execute("CREATE TABLE sentinel(value TEXT)")
    connection.execute("INSERT INTO sentinel VALUES ('unchanged')")
snapshot = hashlib.sha256(Path(database).read_bytes()).digest()
calls = []

class Reasoning:
    def complete(self, request):
        calls.append(request.messages[-1].content)
        return SimpleNamespace(text="confirmed", model="fake")

registry = ExtensionRegistry()
registry.register(ExtensionManifest("probe.reason", CapabilityKind.REASONING,
                                  SideEffectProfile.PURE_READ), Reasoning)
adapter = WhisperPushToTalkAdapter(
    worker_command=[sys.executable, "-B", "tests/test_stt_worker.py", "--fake", mode],
    init_timeout=0.25 if mode == "init_hang" else 3,
    stop_timeout=0.3, transcription_timeout=0.3,
)
registry.register(adapter.manifest, lambda: adapter,
                  probe=lambda: HealthReport(ExtensionState.READY))
session = SessionOrchestrator(
    registry, prompt_factory=lambda *a, **kw: "system",
    classify_input=lambda _: dict(mode="casual", temperature=0, max_tokens=10),
    memory_command=lambda *a: None,
)
before = copy.deepcopy(session.messages)
if mode == "recording_error":
    result = session.listen(TerminalInput().wait_for_stop)
    assert result.kind == "error" and "execution_failed" in result.text, result
    assert session.messages == before and calls == []
    assert adapter.process is None
    print("FAILURE_REAPED", flush=True)
    assert TerminalInput().read_line("") == "written once"
    assert session.handle_text("written once").kind == "chat"
    assert session.handle_text("/estado").kind == "status"
    assert session.handle_text("quit").kind == "exit"
else:
    ada.create_session = lambda **kw: session
    ada.print_header = lambda: None
    ada.chat_loop()
assert calls == ["written once"], calls
assert hashlib.sha256(Path(database).read_bytes()).digest() == snapshot
assert adapter.process is None
print("PROBE_OK", flush=True)
'''


class PublicInputContractTests(unittest.TestCase):
    """Independent verifier probes retained inside the authorized test surface."""

    def test_public_pipeline_after_initialization_failure_and_silence(self):
        for mode in ("init_hang", "silence"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="ada-stt-input-") as directory:
                typed = "\n" + ("\n" if mode == "silence" else "")
                typed += "written once\n/estado\nexit\n"
                result = subprocess.run(
                    [sys.executable, "-B", "-c", PUBLIC_INPUT_PROBE, mode, str(Path(directory) / "db")],
                    cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                    input=typed, text=True, encoding="utf-8", capture_output=True, timeout=12,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("PROBE_OK", result.stdout)

    def test_open_pipe_without_stop_enter_does_not_hide_worker_failure(self):
        import queue
        import threading

        with tempfile.TemporaryDirectory(prefix="ada-stt-pipe-") as directory:
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", PUBLIC_INPUT_PROBE,
                 "recording_error", str(Path(directory) / "db")],
                cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8",
            )
            lines = queue.Queue()

            def receive():
                for line in process.stdout:
                    lines.put(line)

            reader = threading.Thread(target=receive, daemon=True)
            reader.start()
            try:
                # No ENTER is available until the failed worker has been reaped.
                while "FAILURE_REAPED" not in lines.get(timeout=8):
                    pass
                process.stdin.write("written once\n")
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=8), 0, process.stderr.read())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                reader.join(timeout=1)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    def test_legacy_speech_port_receives_empty_bytes_and_succeeds_with_silence(self):
        from ada_core import (
            CapabilityKind, ExtensionManifest, ExtensionRegistry, SideEffectProfile, SpeechInputPort,
        )
        from ada_core.orchestrator import SessionOrchestrator

        seen = []

        class Legacy:
            def transcribe(self, audio: bytes) -> str:
                seen.append(audio)
                return ""

        self.assertIsInstance(Legacy(), SpeechInputPort)
        registry = ExtensionRegistry()
        registry.register(ExtensionManifest("probe.stt", CapabilityKind.STT,
                                            SideEffectProfile.PURE_READ), Legacy)
        session = SessionOrchestrator(
            registry, prompt_factory=lambda _: "system",
            classify_input=lambda _: None, memory_command=lambda *a: None,
        )
        self.assertEqual((session.listen().kind, seen), ("input", [b""]))
        self.assertIn("READY", session.status().text)


if __name__ == "__main__":
    unittest.main()
