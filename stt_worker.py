"""Persistent local speech worker. stdin/stdout are JSON-lines, never a terminal."""

import contextlib
import json
import queue
import sys
import threading


MAX_MESSAGE = 16_384


class MicrophoneCapture:
    """All optional imports, native calls and audio state live in this process."""

    def __init__(self):
        self.model = None
        self.device = None
        self.stream = None
        self.frames = []
        self.error = None

    def start(self):
        import pyaudio
        import numpy as np
        from faster_whisper import WhisperModel

        self.np = np
        if self.model is None:
            self.model = WhisperModel("base", device="cpu", compute_type="int8")
        self.frames = []
        self.error = None
        self.device = pyaudio.PyAudio()

        def record(data, frame_count, time_info, status):
            if status:
                self.error = RuntimeError(f"Audio capture failed ({status}).")
                return None, pyaudio.paAbort
            self.frames.append(data)
            return None, pyaudio.paContinue

        self.stream = self.device.open(
            format=pyaudio.paInt16, channels=1, rate=16000, input=True,
            frames_per_buffer=1024, stream_callback=record,
        )
        self.check()

    def check(self):
        if self.error:
            raise self.error
        if self.stream is not None and not self.stream.is_active():
            raise RuntimeError("Microphone stream stopped unexpectedly.")

    def stop(self):
        # Attempt every cleanup step, but never disguise cleanup failure as silence.
        errors = []
        stream, device = self.stream, self.device
        self.stream = self.device = None
        operations = []
        if stream is not None:
            operations.extend((stream.stop_stream, stream.close))
        if device is not None:
            operations.append(device.terminate)
        for operation in operations:
            try:
                operation()
            except Exception as error:
                errors.append(error)
        if self.error:
            errors.append(self.error)
        if errors:
            raise errors[0]

    def transcribe(self):
        try:
            if not self.frames:
                return ""
            audio = self.np.frombuffer(b"".join(self.frames), dtype=self.np.int16)
            audio = audio.astype(self.np.float32) / 32768.0
            segments, _ = self.model.transcribe(audio, language="es")
            return " ".join(segment.text for segment in segments).strip()
        finally:
            self.frames = []


def run(capture=None):
    capture = capture or MicrophoneCapture()
    protocol = sys.stdout
    commands = queue.Queue(maxsize=8)

    def receive():
        try:
            while True:
                line = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
                if not line:
                    commands.put(None)
                    return
                if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                    commands.put(ValueError("Invalid IPC message size."))
                    return
                commands.put(json.loads(line))
        except Exception as error:
            commands.put(error)

    def send(job_id, kind, **fields):
        message = json.dumps(dict(job_id=job_id, type=kind, **fields), ensure_ascii=True)
        if len(message) > MAX_MESSAGE - 1:
            raise ValueError("Transcription exceeds IPC limit.")
        protocol.write(message + "\n")
        protocol.flush()

    threading.Thread(target=receive, daemon=True).start()
    job_id = None
    recording = False
    # Third-party libraries may print: stdout remains exclusively protocol-owned.
    with contextlib.redirect_stdout(sys.stderr):
        while True:
            try:
                try:
                    command = commands.get(timeout=0.05)
                except queue.Empty:
                    if recording:
                        capture.check()
                    continue
                if command is None:
                    capture.stop()
                    return
                if not isinstance(command, dict):
                    raise ValueError("Invalid IPC command.")
                kind, incoming = command.get("type"), command.get("job_id")
                if not isinstance(incoming, str) or not incoming or len(incoming) > 80:
                    raise ValueError("Invalid job identity.")
                if kind == "start" and not recording:
                    job_id = incoming
                    send(job_id, "initializing")
                    capture.start()
                    recording = True
                    send(job_id, "recording")
                elif kind == "stop" and recording and incoming == job_id:
                    capture.check()
                    capture.stop()
                    recording = False
                    send(job_id, "transcribing")
                    send(job_id, "result", text=capture.transcribe())
                elif kind == "shutdown":
                    job_id = incoming
                    capture.stop()
                    send(job_id, "shutdown")
                    return
                else:
                    raise ValueError("Unexpected IPC command or stale job.")
            except Exception as error:
                send(job_id, "error",
                     code="dependency_missing" if isinstance(error, ImportError) else "execution_failed",
                     message="Speech dependencies are unavailable." if isinstance(error, ImportError)
                     else "Microphone, cleanup or transcription failed.")
                # The parent reaps this failed worker; do not retry native cleanup here.
                return


if __name__ == "__main__":
    run()
