"""Optional local browser worker; importing this module needs only Python."""

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
from threading import Lock
import time
from urllib.parse import urlsplit

from ada_core.registry import _interruptible_resource

ROOT = Path(__file__).resolve().parent
TIMEOUT_SECONDS = 180
MAX_OUTPUT = 40_000
MAX_PAGE_TEXT = 20_000
URL_POLICY_MESSAGE = "Usa una URL HTTPS pública, sin credenciales y del dominio autorizado."
WORKER_MISSING_MESSAGE = "Prepara .venv-browser e instala requirements-browser.txt; consulta README."
EDGE_MISSING_MESSAGE = "Microsoft Edge no está instalado en una ubicación compatible."
WORKER_TIMEOUT_MESSAGE = "La navegación agotó su tiempo y se detuvo; no se reintentó."
PAGE_FAILED_MESSAGE = "No se pudo abrir o leer la página en Edge; no se reintentó."


class BrowserError(Exception):
    """Safe user-facing failure, never raw browser/model diagnostics."""

    def __init__(self, message, *, code="browser_failed"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BrowserResult:
    text: str
    url: str


def validate_url(url: str, *, host: str | None = None, resolve: bool = False) -> str:
    try:
        parsed = urlsplit(url)
        name = parsed.hostname or ""
        if (not url or any(ord(c) <= 32 or ord(c) == 127 for c in url)
                or parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or not re.fullmatch(r"[a-z0-9.-]+", name)
                or name.endswith((".local", ".localhost", ".internal")) or "." not in name
                or (host is not None and name != host)):
            raise ValueError
        try:
            address = ipaddress.ip_address(name)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        if resolve:
            addresses = socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise ValueError
        return name
    except (ValueError, OSError):
        raise BrowserError(URL_POLICY_MESSAGE, code="permission_denied") from None


def validate_observation(data, *, host: str) -> dict[str, str]:
    fields = ("url", "title", "language", "text")
    if not isinstance(data, dict) or any(not isinstance(data.get(key), str) for key in fields):
        raise ValueError("Invalid page observation")
    validate_url(data["url"], host=host)
    return data


def bounded_observation(data, *, host: str) -> dict[str, str]:
    data = validate_observation(data, host=host)
    return {"url": data["url"], "title": data["title"][:2000],
            "language": data["language"][:64], "text": data["text"][:MAX_PAGE_TEXT]}


def validate_text(text: str) -> str:
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_OUTPUT
            or any((ord(c) < 32 and c not in "\n\t") or 127 <= ord(c) < 160 for c in text)):
        raise BrowserError("El navegador no devolvió texto seguro y completo dentro del límite.")
    return text


def worker_python() -> Path:
    executable = ROOT / ".venv-browser" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        raise BrowserError(WORKER_MISSING_MESSAGE, code="dependency_missing")
    return executable


def worker_environment(job: Path) -> dict[str, str]:
    # Edge needs a consistent Windows identity for known-folder/CDP startup.
    # This preserves OS paths, not credentials or personal browser profiles.
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA",
            "PROGRAMFILES", "PROGRAMFILES(X86)", "USERPROFILE", "USERNAME", "USERDOMAIN",
            "HOMEDRIVE", "HOMEPATH", "SYSTEMDRIVE", "USERDOMAIN_ROAMINGPROFILE")
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env.update({
        "HOME": str(job), "BROWSER_USE_CONFIG_DIR": str(job / "config"),
        "ANONYMIZED_TELEMETRY": "false", "BROWSER_USE_CLOUD_SYNC": "false",
        "BROWSER_USE_CLOUD_HINTS": "false", "PYTHON_DOTENV_DISABLED": "1",
        "BROWSER_USE_LOGGING_LEVEL": "critical", "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": "localhost,127.0.0.1",
        "ADA_BROWSER_MODEL": os.environ.get("ADA_BROWSER_MODEL", "qwen2.5:3b"),
    })
    return env


@contextmanager
def worker_job(process):
    """A kill-on-close Windows Job owns descendants, even if the worker crashes."""
    if os.name != "nt":
        yield
        return
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                     ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    class BasicAccounting(ctypes.Structure):
        _fields_ = [("times", ctypes.c_longlong * 4), ("page_faults", wintypes.DWORD),
                    ("total_processes", wintypes.DWORD), ("active_processes", wintypes.DWORD),
                    ("terminated_processes", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        stop_worker(process)
        raise BrowserError("No se pudo aislar el proceso del navegador.")
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle)))):
            stop_worker(process)
            raise BrowserError("No se pudo aislar el proceso del navegador.")
        def terminate():
            if not kernel.TerminateJobObject(handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            # Termination is asynchronous for descendants. Keep the job
            # handle valid until the OS confirms the tree is no longer active.
            accounting = BasicAccounting()
            deadline = time.monotonic() + 10
            while True:
                if not kernel.QueryInformationJobObject(
                        handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not accounting.active_processes:
                    break
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired("browser job termination", 10)
                time.sleep(0.01)

        # The worker is still waiting on stdin; it cannot launch Edge before assignment.
        yield terminate
    finally:
        kernel.CloseHandle(handle)


def stop_worker(process) -> None:
    """Stop only the owned worker and its browser descendants."""
    if os.name == "nt":
        if process.poll() is not None:
            return
        killer = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/taskkill.exe"
        subprocess.run([str(killer), "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


@contextmanager
def _browser_process(executable, job):
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen([str(executable), "-B", str(ROOT / "browser_worker.py")],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               cwd=job, env=worker_environment(job), **options)
    with worker_job(process) as terminate_job:
        stop_lock, stopped = Lock(), False

        def stop():
            nonlocal stopped
            # A browser deadline and Ctrl+C can race; only stop this job once.
            with stop_lock:
                if not stopped:
                    if terminate_job is None:
                        stop_worker(process)
                    else:
                        terminate_job()
                        process.wait(timeout=10)
                    stopped = True

        yield process, stop


def validate_task(task: str):
    if not task.strip() or len(task) > 4000:
        raise BrowserError("Usa /navegar <https-url> <tarea de lectura> (máximo 4000 caracteres).")


def _run_worker(payload):
    executable = worker_python()
    with tempfile.TemporaryDirectory(prefix="ada-browser-job-") as directory:
        with _interruptible_resource(_browser_process(executable, Path(directory))) as (process, stop):
            try:
                output, _ = process.communicate(input=json.dumps(payload).encode(),
                                                timeout=TIMEOUT_SECONDS)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                stop()
                if isinstance(error, KeyboardInterrupt):
                    raise
                raise BrowserError(WORKER_TIMEOUT_MESSAGE, code="timeout") from None
        try:
            data = json.loads(output) if len(output) <= MAX_OUTPUT * 8 else None
        except ValueError:
            data = None
        return process.returncode, data if isinstance(data, dict) else {}


def observe_page(url: str) -> dict[str, str]:
    """Read one public page in the isolated worker, without a task or model."""
    host = validate_url(url)
    status, data = _run_worker({"url": url})
    if not status and data.get("ok") is True:
        try:
            return bounded_observation(data.get("observation"), host=host)
        except ValueError:
            pass
    elif data.get("error") == "permission_denied":
        raise BrowserError(URL_POLICY_MESSAGE, code="permission_denied")
    elif data.get("error") == "dependency_missing":
        raise BrowserError(EDGE_MISSING_MESSAGE, code="dependency_missing")
    raise BrowserError(PAGE_FAILED_MESSAGE)


def run_task(url: str, task: str) -> BrowserResult:
    """Standalone legacy CLI; the ADA session uses observe_page instead."""
    host = validate_url(url)
    validate_task(task)
    status, data = _run_worker({"url": url, "task": task})
    if data.get("error") == "model_task_failed":
        raise BrowserError("El modelo local no completó la tarea. Comprueba ADA_BROWSER_MODEL; no se mostró una respuesta fallida.")
    if not status and data.get("ok") is True:
        try:
            validate_url(data["url"], host=host)
            return BrowserResult(validate_text(data["text"]), data["url"])
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
    raise BrowserError("La tarea local no se completó. Comprueba Edge y Ollama; no se reintentó.")


def main():
    parser = argparse.ArgumentParser(description="Ejecutar una tarea local de lectura con Browser Use y Edge.")
    parser.add_argument("--url", required=True, help="URL HTTPS pública y dominio autorizado.")
    parser.add_argument("--task", required=True, help="Tarea de lectura; no inicia sesión ni escribe en sitios.")
    args = parser.parse_args()
    try:
        print(json.dumps(asdict(run_task(args.url, args.task)), ensure_ascii=True))
        return 0
    except BrowserError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
