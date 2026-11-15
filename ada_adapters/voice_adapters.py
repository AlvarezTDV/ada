"""Lazy push-to-talk and speech output bridges; probes never initialize audio."""

from dataclasses import dataclass
from importlib import import_module, util
import builtins
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid

from ada_core import (
    CapabilityError, CapabilityKind, ErrorCode, ExtensionManifest, ExtensionState,
    HealthReport, SideEffectProfile, SpeechOutputReport,
)


@dataclass(frozen=True)
class _VoiceManifest(ExtensionManifest):
    optional_packages: tuple[str, ...] = ()


def _voice_factory():
    return import_module("voice").VoiceEngine()


def _available(package):
    try:
        return util.find_spec(package) is not None
    except (ImportError, ValueError):
        return False


class _VoiceAdapter:
    def __init__(self, *, voice_factory=None):
        self._voice_factory = voice_factory if voice_factory is not None else _voice_factory
        self._engine = None

    def _voice(self):
        if self._engine is None:
            self._engine = self._voice_factory()
        return self._engine

    def _error(self, code, message):
        return CapabilityError(code, self.manifest.capability, message,
                               extension_id=self.manifest.extension_id)


class WhisperPushToTalkAdapter(_VoiceAdapter):
    """Supervise a persistent worker; no audio-library calls execute in the host."""

    manifest = _VoiceManifest(
        "stt.faster_whisper", CapabilityKind.STT, SideEffectProfile.PURE_READ,
        optional_packages=("faster_whisper", "pyaudio", "numpy"),
    )

    def __init__(self, *, voice_factory=None, worker_command=None,
                 init_timeout=30.0, stop_timeout=5.0, transcription_timeout=30.0,
                 reap_timeout=1.0):
        super().__init__(voice_factory=voice_factory)
        self.worker_command = worker_command or [
            sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "stt_worker.py")]
        self.init_timeout = init_timeout
        self.stop_timeout = stop_timeout
        self.transcription_timeout = transcription_timeout
        self.reap_timeout = reap_timeout
        self.process = None
        self._job = None
        self._reader = None
        self._events = None
        self._job_id = None
        self._phase = "idle"
        self._deadline = None
        self._quarantined = False

    @staticmethod
    def probe() -> HealthReport:
        missing = [name for name in WhisperPushToTalkAdapter.manifest.optional_packages
                   if not _available(name)]
        if missing:
            return HealthReport(ExtensionState.UNAVAILABLE_DEPENDENCY,
                                "Missing speech input packages: " + ", ".join(missing))
        return HealthReport(ExtensionState.READY)

    def transcribe(self, audio: bytes = b"") -> str:
        """Compatibility entry point; sessions use the phased interactive port."""
        if audio:
            raise self._error(ErrorCode.EXECUTION_FAILED,
                              "Push-to-talk requires an empty audio buffer.")
        try:
            self.start_capture()
            while self.poll_capture()[0] != "recording":
                time.sleep(0.01)
            TerminalInput().wait_for_stop(lambda: self.poll_capture())
            self.stop_capture()
            while True:
                phase, text = self.poll_capture()
                if phase == "complete":
                    return text
                time.sleep(0.01)
        except BaseException:
            self.cancel_capture()
            raise

    def _launch(self):
        from browser_navigation import worker_job

        options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True})
        self.process = subprocess.Popen(
            self.worker_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, **options,
        )
        # The child must wait for its first command before any audio/native work.
        self._job = worker_job(self.process)
        self._job.__enter__()
        self._events = queue.Queue(maxsize=32)
        process, events = self.process, self._events

        def receive():
            try:
                while True:
                    line = process.stdout.readline(16_385)
                    if not line:
                        events.put_nowait(None)
                        return
                    if len(line) > 16_384 or not line.endswith(b"\n"):
                        raise ValueError("Invalid speech IPC size.")
                    events.put_nowait(json.loads(line))
            except Exception:
                # Never block the pipe reader on a noisy/broken worker.
                try:
                    events.put_nowait(None)
                except queue.Full:
                    pass

        self._reader = threading.Thread(target=receive, daemon=True)
        self._reader.start()

    def _send(self, kind):
        payload = json.dumps({"job_id": self._job_id, "type": kind}).encode() + b"\n"
        self.process.stdin.write(payload)
        self.process.stdin.flush()

    def _terminate(self):
        process = self.process
        if process is None:
            return
        # Invalidate first; each replacement has its own pipe/queue and identity.
        self._job_id = None
        if self._job is not None:
            try:
                self._job.__exit__(None, None, None)
            except Exception:
                pass
            self._job = None
        try:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            process.wait(timeout=self.reap_timeout)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            self._quarantined = True
            raise self._error(
                ErrorCode.EXECUTION_FAILED,
                "Microphone quarantined: worker termination could not be confirmed; continue using text.",
            )
        if self._reader is not None:
            self._reader.join(timeout=self.reap_timeout)
        for pipe in (process.stdin, process.stdout):
            if pipe:
                pipe.close()
        self.process = None
        self._reader = None
        self._events = None
        self._phase = "idle"

    def _fail(self, code, message):
        self._terminate()
        raise self._error(code, message)

    def start_capture(self):
        if self._quarantined:
            raise self._error(
                ErrorCode.EXECUTION_FAILED,
                "Microphone quarantined: worker termination could not be confirmed; continue using text.",
            )
        if self._phase not in {"idle", "complete"}:
            self._fail(ErrorCode.EXECUTION_FAILED, "A speech capture is already active.")
        try:
            if self.process is None:
                self._launch()
            elif self.process.poll() is not None:
                self._terminate()
                self._launch()
            self._job_id = uuid.uuid4().hex
            self._phase = "initializing"
            self._deadline = time.monotonic() + self.init_timeout
            self._send("start")
        except CapabilityError:
            raise
        except Exception:
            self._fail(ErrorCode.EXECUTION_FAILED, "Speech worker could not start; continue using text.")

    def poll_capture(self):
        if self.process is None:
            raise self._error(ErrorCode.EXECUTION_FAILED, "No active speech worker.")
        if self._deadline is not None and time.monotonic() >= self._deadline:
            self._fail(ErrorCode.TIMEOUT, f"Speech {self._phase} timed out; worker stopped. Continue using text.")
        no_event = object()
        try:
            event = self._events.get_nowait()
        except queue.Empty:
            event = no_event
            if self.process.poll() is not None:
                # A dead child may still have a structured error buffered in its
                # pipe. Let the reader drain to EOF before classifying the exit.
                self._reader.join(timeout=self.reap_timeout)
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    event = None
        if event is None:
            self._fail(ErrorCode.EXECUTION_FAILED, "Speech worker exited unexpectedly; continue using text.")
        if event is not no_event:
            if not isinstance(event, dict) or event.get("job_id") != self._job_id:
                self._fail(ErrorCode.EXECUTION_FAILED, "Invalid or stale speech worker response.")
            kind = event.get("type")
            if kind == "error":
                code = (ErrorCode.DEPENDENCY_MISSING if event.get("code") == "dependency_missing"
                        else ErrorCode.EXECUTION_FAILED)
                self._fail(code, "Speech dependencies are unavailable." if code == ErrorCode.DEPENDENCY_MISSING
                           else "Microphone, cleanup or transcription failed; continue using text.")
            elif kind == "initializing" and self._phase == "initializing":
                pass
            elif kind == "recording" and self._phase == "initializing":
                self._phase, self._deadline = "recording", None
            elif kind == "transcribing" and self._phase == "stopping":
                self._phase = "transcribing"
                self._deadline = time.monotonic() + self.transcription_timeout
            elif kind == "result" and self._phase == "transcribing" and isinstance(event.get("text"), str):
                self._phase, self._deadline = "complete", None
                return "complete", event["text"].strip()
            else:
                self._fail(ErrorCode.EXECUTION_FAILED, "Unexpected speech worker phase or result.")
        if self._deadline is not None and time.monotonic() >= self._deadline:
            self._fail(ErrorCode.TIMEOUT, f"Speech {self._phase} timed out; worker stopped. Continue using text.")
        return self._phase, ""

    def stop_capture(self):
        if self._phase != "recording":
            self._fail(ErrorCode.EXECUTION_FAILED, "Microphone is not recording.")
        self._phase = "stopping"
        self._deadline = time.monotonic() + self.stop_timeout
        try:
            self._send("stop")
        except OSError:
            self._fail(ErrorCode.EXECUTION_FAILED, "Speech worker disconnected while stopping.")

    def cancel_capture(self):
        self._terminate()

    def shutdown(self):
        # Kill-on-close also removes descendants after a normal successful turn.
        self._terminate()


_ORIGINAL_INPUT = builtins.input


class TerminalInput:
    """One host-owned reader; poll health without creating a blocking stdin thread."""

    def read_line(self, prompt):
        if builtins.input is not _ORIGINAL_INPUT or (os.name == "nt" and sys.stdin.isatty()):
            return builtins.input(prompt)
        # Use the same raw stream for normal and recording input: TextIOWrapper
        # read-ahead must not hide piped ENTERs from the stop poller.
        print(prompt, end="", flush=True)
        data = bytearray()
        while True:
            char = os.read(sys.stdin.fileno(), 1)
            if not char:
                if data:
                    return data.decode(sys.stdin.encoding or "utf-8")
                raise EOFError
            if char == b"\n":
                return data.decode(sys.stdin.encoding or "utf-8").rstrip("\r")
            data.extend(char)

    def wait_for_stop(self, tick):
        prompt = "Grabando... Presiona ENTER para detener y volver al chat.\n"
        tick()
        if builtins.input is not _ORIGINAL_INPUT:
            # Existing test/embedding seam: invoke the injected reader on this thread.
            builtins.input(prompt)
            tick()
            return
        print(prompt, end="", flush=True)
        while True:
            tick()
            if os.name == "nt" and sys.stdin.isatty():
                import msvcrt
                if msvcrt.kbhit():
                    char = msvcrt.getwch()
                    if char == "\x03":
                        raise KeyboardInterrupt
                    if char in "\r\n":
                        return
                    if char in {"\x00", "\xe0"} and msvcrt.kbhit():
                        msvcrt.getwch()
            else:
                if os.name == "nt":
                    import ctypes
                    import msvcrt
                    available = ctypes.c_ulong()
                    handle = msvcrt.get_osfhandle(sys.stdin.fileno())
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.PeekNamedPipe.argtypes = [
                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
                    if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
                        raise EOFError
                    ready = bool(available.value)
                else:
                    import select
                    ready = bool(select.select([sys.stdin], [], [], 0)[0])
                if ready:
                    char = os.read(sys.stdin.fileno(), 1)
                    if not char:
                        raise EOFError
                    if char == b"\n":
                        return
            time.sleep(0.02)


class EdgeLocalTTSAdapter(_VoiceAdapter):
    manifest = _VoiceManifest(
        "tts.edge_with_local_fallback", CapabilityKind.TTS,
        SideEffectProfile.NON_IDEMPOTENT_MUTATION,
        optional_packages=("edge_tts", "pygame", "pyttsx3"),
    )

    @staticmethod
    def probe() -> HealthReport:
        if _available("edge_tts") and _available("pygame"):
            return HealthReport(ExtensionState.READY)
        if _available("pyttsx3"):
            return HealthReport(ExtensionState.DEGRADED, "Only local speech output is available.")
        return HealthReport(ExtensionState.UNAVAILABLE_DEPENDENCY,
                            "Speech output requires edge_tts and pygame, or pyttsx3.")

    def speak(self, text: str) -> SpeechOutputReport:
        try:
            result = self._voice().speak(text)
        except ImportError as error:
            raise self._error(ErrorCode.DEPENDENCY_MISSING, "Speech output dependencies are unavailable.") from error
        except Exception as error:
            raise self._error(ErrorCode.EXECUTION_FAILED, "Speech output failed.") from error
        if not result.success:
            raise self._error(ErrorCode.EXECUTION_FAILED, "Speech output failed; continue using text.")
        return SpeechOutputReport(result.success, result.engine, result.completed_chunks,
                                  result.stage or "complete", str(result.error) if result.error else "")
