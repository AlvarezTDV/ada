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
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
TIMEOUT_SECONDS = 180
MAX_OUTPUT = 40_000


class BrowserError(Exception):
    """Safe user-facing failure, never raw browser/model diagnostics."""


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
        raise BrowserError("Usa una URL HTTPS pública, sin credenciales y del dominio autorizado.") from None


def validate_text(text: str) -> str:
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_OUTPUT
            or any((ord(c) < 32 and c not in "\n\t") or 127 <= ord(c) < 160 for c in text)):
        raise BrowserError("El navegador no devolvió texto seguro y completo dentro del límite.")
    return text


def worker_python() -> Path:
    executable = ROOT / ".venv-browser" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        raise BrowserError("Prepara .venv-browser e instala requirements-browser.txt; consulta README.")
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

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
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
        # The worker is still waiting on stdin; it cannot launch Edge before assignment.
        yield
    finally:
        kernel.CloseHandle(handle)


def stop_worker(process) -> None:
    """Stop only the owned worker and its browser descendants."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        killer = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/taskkill.exe"
        subprocess.run([str(killer), "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)


def run_task(url: str, task: str) -> BrowserResult:
    host = validate_url(url)
    if not task.strip() or len(task) > 4000:
        raise BrowserError("Usa /navegar <https-url> <tarea de lectura> (máximo 4000 caracteres).")
    executable = worker_python()
    with tempfile.TemporaryDirectory(prefix="ada-browser-job-") as directory:
        job = Path(directory)
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen([str(executable), "-B", str(ROOT / "browser_worker.py")],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   cwd=job, env=worker_environment(job), **options)
        with worker_job(process):
            try:
                output, _ = process.communicate(input=json.dumps({"url": url, "task": task}).encode(),
                                                timeout=TIMEOUT_SECONDS)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                stop_worker(process)
                if isinstance(error, KeyboardInterrupt):
                    raise
                raise BrowserError("La navegación agotó su tiempo y se detuvo; no se reintentó.") from None
        try:
            data = json.loads(output) if len(output) <= MAX_OUTPUT * 8 else {}
            if isinstance(data, dict) and data.get("error") == "model_task_failed":
                raise BrowserError("El modelo local no completó la tarea. Comprueba ADA_BROWSER_MODEL; no se mostró una respuesta fallida.")
            if process.returncode or data.get("ok") is not True:
                raise ValueError
            validate_url(data["url"], host=host)
            return BrowserResult(validate_text(data["text"]), data["url"])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise BrowserError("La tarea local no se completó. Comprueba Edge y Ollama; no se reintentó.") from None


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
