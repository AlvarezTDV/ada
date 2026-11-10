"""Exercise the runner's public CLI against isolated, synthetic suites."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "run_tests.py"


class TestRunnerTimeout(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ada-runner-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "scripts").mkdir()
        (self.root / "tests").mkdir()
        self.runner = self.root / "scripts" / "run_tests.py"
        shutil.copyfile(RUNNER, self.runner)

    def write_suite(self, source, name="test_example.py"):
        (self.root / "tests" / name).write_text(
            textwrap.dedent(source), encoding="utf-8"
        )

    def run_cli(self, *arguments):
        environment = os.environ.copy()
        environment.update(ADA_RUN_MODEL_EVALS="1", PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, "-B", str(self.runner), *arguments],
            cwd=self.root,
            env=environment,
            timeout=8,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_passing_suite_outputs_compact_summary_without_verbose_noise(self):
        for arguments, expected in (
            ((), "0"),
            (("--pattern", "test_example.py", "--timeout", "3"), "0"),
            (("--live-evals",), "1"),
        ):
            with self.subTest(arguments=arguments):
                self.write_suite(f"""
                    import os
                    import sys
                    import unittest

                    class Example(unittest.TestCase):
                        def test_offline_environment(self):
                            print("unexpected stdout noise")
                            print("unexpected stderr noise", file=sys.stderr)
                            self.assertEqual(os.environ["ADA_RUN_MODEL_EVALS"], {expected!r})
                """)
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertRegex(result.stdout, r"\ARan 1 tests? in [\d.]+s — OK\n\Z")
                self.assertEqual(result.stderr, "")

        self.write_suite("raise RuntimeError('pattern must exclude this file')", "test_excluded.py")
        self.assertEqual(
            self.run_cli("--pattern", "test_example.py", "--live-evals").returncode, 0
        )

    def test_failing_suite_triggers_verbose_diagnostic_output(self):
        self.write_suite("""
            from pathlib import Path
            import sys
            import unittest

            class Example(unittest.TestCase):
                def test_deliberate_failure(self):
                    log = Path("attempts.txt")
                    attempts = log.read_text() if log.exists() else ""
                    mode = "verbose" if "-v" in sys.argv else "quiet"
                    log.write_text(attempts + mode + "\\n")
                    if mode == "quiet":
                        self.fail("deliberate failure marker")
        """)
        result = self.run_cli("--pattern", "test_example.py", "--timeout", "3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deliberate failure marker", result.stdout + result.stderr)
        self.assertIn("test_deliberate_failure (test_example.Example", result.stdout + result.stderr)
        self.assertEqual((self.root / "attempts.txt").read_text(), "quiet\nverbose\n")

    def test_hanging_command_is_terminated_when_timeout_expires(self):
        for quiet_delay, limit in ((None, "0.3"), (1.5, "2")):
            with self.subTest(quiet_delay=quiet_delay):
                self.write_suite(f"""
                    from pathlib import Path
                    import sys
                    import time
                    import unittest

                    class Example(unittest.TestCase):
                        def test_hangs(self):
                            if {quiet_delay!r} is not None and "-q" in sys.argv:
                                time.sleep({quiet_delay!r})
                                self.fail("retry within remaining budget")
                            time.sleep(999)
                            Path("escaped_timeout.txt").touch()
                """)
                started = time.monotonic()
                result = self.run_cli("--timeout", limit)
                elapsed = time.monotonic() - started
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    f"TIMEOUT: la suite superó el límite de {limit} segundos",
                    result.stdout + result.stderr,
                )
                # Allow process startup/cleanup overhead, but not a fresh retry budget.
                self.assertLess(elapsed, float(limit) + 1)
                self.assertFalse((self.root / "escaped_timeout.txt").exists())


if __name__ == "__main__":
    unittest.main()
