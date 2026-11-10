"""Run offline unittest discovery quietly, with one shared hard timeout."""

import argparse
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pattern", default="test_*.py", help="discovery filename pattern (default: test_*.py)"
    )
    parser.add_argument(
        "--timeout", type=positive_seconds, default=45,
        help="total seconds shared by quiet and verbose runs (default: 45)",
    )
    parser.add_argument(
        "--live-evals", action="store_true",
        help="explicitly enable model evaluations (may make network requests)",
    )
    args = parser.parse_args(argv)
    deadline = time.monotonic() + args.timeout
    environment = os.environ.copy()
    environment.update(
        ADA_RUN_MODEL_EVALS="1" if args.live_evals else "0",
        PYTHONIOENCODING="utf-8",
    )
    command = [
        sys.executable, "-B", "-m", "unittest", "discover",
        "-s", "tests", "-p", args.pattern,
    ]

    def run(mode):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, args.timeout)
        return subprocess.run(
            [*command, mode],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            timeout=remaining,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def show(result):
        print(result.stdout, end="", file=sys.stdout)
        print(result.stderr, end="", file=sys.stderr)

    try:
        quiet = run("-q")
        if quiet.returncode == 0:
            summary = re.search(
                r"^Ran \d+ tests? in [\d.]+s$", quiet.stderr, re.MULTILINE
            )
            if summary is None:
                print("ERROR: unittest returned no test summary", file=sys.stderr)
                return 1
            line = f"{summary.group()} — OK"
            print(f"\033[32m{line}\033[0m" if sys.stdout.isatty() else line)
            return 0
        show(quiet)
        show(run("-v"))
        # A passing diagnostic retry must not hide the initial failure.
        return 1
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for the child before raising.
        print(
            f"TIMEOUT: la suite superó el límite de {args.timeout:g} segundos",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
