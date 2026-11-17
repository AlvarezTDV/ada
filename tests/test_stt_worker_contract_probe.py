"""Independent subprocess probes for the worker's initialization contract."""

import json
from pathlib import Path
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKER = r"""
import sys
import threading
from types import SimpleNamespace
import stt_worker

mode = sys.argv[1]
background = False
source = sys.stdin.buffer

class Input:
    def readline(self, limit):
        global background
        if threading.current_thread() is not threading.main_thread():
            background = True
        return source.readline(limit)

sys.stdin = SimpleNamespace(buffer=Input())

class Capture:
    starts = 0

    def start(self):
        if mode == "ordering":
            assert not background
        if mode == "import":
            raise ImportError("private dependency detail")
        if mode == "execution":
            raise RuntimeError("private device detail")
        self.starts += 1
        print("START", file=sys.stderr)

    def check(self):
        pass

    def stop(self):
        print("STOP", file=sys.stderr)

    def transcribe(self):
        return "" if self.starts == 1 else "second"

stt_worker.run(Capture())
"""


def command(kind, job="a"):
    return json.dumps(dict(type=kind, job_id=job)) + "\n"


def event(kind, job="a", **extra):
    return dict(job_id=job, type=kind, **extra)


def error(job=None, *, dependency=False):
    return event(
        "error", job,
        code="dependency_missing" if dependency else "execution_failed",
        message=("Speech dependencies are unavailable." if dependency
                 else "Microphone, cleanup or transcription failed."),
    )


class WorkerInitializationContractTests(unittest.TestCase):
    def close_process(self, process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()

    def launch(self, mode="normal"):
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", WORKER, mode],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.close_process, process)
        return process

    def exchange(self, data, expected, *, mode="normal", stderr=None):
        process = self.launch(mode)
        output, diagnostic = process.communicate(data, timeout=3)
        self.assertEqual(process.returncode, 0, diagnostic)
        self.assertEqual(
            output,
            "".join(json.dumps(item, ensure_ascii=True) + "\n" for item in expected),
        )
        if stderr is not None:
            self.assertEqual(diagnostic, stderr)

    def test_no_native_work_until_first_start(self):
        process = self.launch()
        time.sleep(0.15)
        self.assertIsNone(process.poll())
        output, diagnostic = process.communicate(command("shutdown"), timeout=3)
        self.assertEqual(
            (process.returncode, output, diagnostic),
            (0, json.dumps(event("shutdown")) + "\n", "STOP\n"),
        )

    def test_first_start_ordering(self):
        self.exchange(
            command("start") + command("shutdown"),
            [event("initializing"), event("recording"), event("shutdown")],
            mode="ordering", stderr="START\nSTOP\n",
        )

    def test_reuse_silence_and_protocol_stdout(self):
        self.exchange(
            command("start") + command("stop") + command("start", "b")
            + command("stop", "b") + command("shutdown", "b"),
            [
                event("initializing"), event("recording"),
                event("transcribing"), event("result", text=""),
                event("initializing", "b"), event("recording", "b"),
                event("transcribing", "b"), event("result", "b", text="second"),
                event("shutdown", "b"),
            ],
            stderr="START\nSTOP\nSTART\nSTOP\nSTOP\n",
        )

    def test_first_start_errors(self):
        for mode in ("import", "execution"):
            with self.subTest(mode=mode):
                self.exchange(
                    command("start"),
                    [event("initializing"), error("a", dependency=mode == "import")],
                    mode=mode, stderr="",
                )

    def test_prestart_rejections_and_eof(self):
        self.exchange("", [], stderr="STOP\n")
        self.exchange(command("shutdown"), [event("shutdown")], stderr="STOP\n")
        rejected = (
            "{\n", "[]\n", command("stop"), command("start", ""),
            command("start", "x" * 81), "x" * 16_385 + "\n", "{}",
        )
        for data in rejected:
            with self.subTest(data=data[:30]):
                self.exchange(data, [error()], stderr="")


if __name__ == "__main__":
    unittest.main()
