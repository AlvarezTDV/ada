"""Session policy over capability ports; no SDK, SQLite, audio or browser imports."""

from dataclasses import dataclass, replace
import re
from threading import Event

from .domain import ChatMessage, CompletionRequest, MemoryItem, TurnMode, WebNavigationRequest
from .errors import CapabilityError, CapabilityKind, ErrorCode, redact_secrets
from .ports import MemoryCommandPort
from .registry import ExtensionState


@dataclass(frozen=True)
class SessionResult:
    text: str
    kind: str = "chat"
    mode: str = "casual"
    url: str = ""
    notices: tuple[str, ...] = ()


class _MemoryCommands:
    """Legacy command-shaped facade whose storage calls all cross the registry."""

    def __init__(self, registry):
        self.registry = registry

    def _execute(self, operation):
        def invoke(port):
            if not isinstance(port, MemoryCommandPort):
                raise CapabilityError(
                    ErrorCode.INCOMPATIBLE_CONTRACT, CapabilityKind.MEMORY,
                    "This memory extension does not support atomic memory commands.",
                )
            return operation(port)
        return self.registry.execute(CapabilityKind.MEMORY, invoke)

    def get_all_memories(self):
        return [(item.category, item.key, item.value)
                for item in self._execute(lambda port: port.list_all())]

    def save_memories(self, records):
        if not isinstance(records, list) or not records or any(
                not isinstance(record, dict) for record in records):
            raise ValueError("Expected a non-empty array of memory objects")
        items = tuple(MemoryItem(record.get("category", "general"),
                                 record.get("key"), record.get("value"))
                      for record in records)
        return [dict(category=item.category, key=item.key, value=item.value)
                for item in self._execute(lambda port: port.save_batch(items))]

    def delete_memories(self, keys):
        return list(self._execute(lambda port: port.delete_batch(tuple(keys))))


class SessionOrchestrator:
    """Own history and turn routing; capability failures never commit failed turns."""

    def __init__(self, registry, *, prompt_factory, classify_input, memory_command,
                 configured_models=None, navigation_validator=None):
        self.registry = registry
        self._prompt = prompt_factory
        self._classify = classify_input
        self._memory_command = memory_command
        self._store = _MemoryCommands(registry)
        self._configured_models = configured_models or {}
        self._navigation_validator = navigation_validator
        self.model = ""
        self.messages = [ChatMessage("system", self._prompt(()))]
        self._reasoning_failure = None
        self._reload_prompt = False
        self._timed_out_recording = False

    def _complete(self, request):
        # A new user turn is an explicit retry, never a replay of a timed-out
        # mutation. enable() refuses while a timed-out read is still running.
        if self._reasoning_failure is not None:
            error = self._reasoning_failure
            if error.extension_id:
                self.registry.enable(error.extension_id)
            self._reasoning_failure = None
        try:
            response = self.registry.execute(
                CapabilityKind.REASONING, lambda port: port.complete(request))
        except CapabilityError as error:
            self._reasoning_failure = error
            raise
        self.model = response.model
        return response

    def _extract(self, prompt, max_tokens):
        request = CompletionRequest(
            (ChatMessage("user", prompt),), TurnMode.TASK,
            model=self.model, temperature=0, max_tokens=max_tokens,
        )
        return self._complete(request).text

    def _error(self, error, *, memory_command=False):
        if (error.capability == CapabilityKind.NAVIGATION
                and error.code == ErrorCode.PERMISSION_DENIED):
            # A local URL-policy rejection is not an HTTP response.
            return SessionResult(error.user_message, kind="error")
        details = {
            ErrorCode.AUTH_INVALID: "Error de autenticación (401). Revisa la clave del proveedor.",
            ErrorCode.PERMISSION_DENIED: "Acceso denegado (403). Revisa los permisos.",
            ErrorCode.QUOTA_EXCEEDED: "Cuota agotada (429) en todos los modelos disponibles.",
            ErrorCode.NETWORK_UNAVAILABLE: "No se pudo establecer conexión con el proveedor.",
            ErrorCode.TIMEOUT: "Se agotó el tiempo de espera.",
            ErrorCode.EMPTY_RESPONSE: "El proveedor devolvió una respuesta vacía o truncada tras el reintento.",
            ErrorCode.INCOMPLETE_RESPONSE: "El proveedor devolvió una respuesta vacía o truncada tras el reintento.",
        }
        prefix = ""
        if error.capability == CapabilityKind.REASONING:
            prefix = ("No puedo extraer la orden de memoria: el proveedor de razonamiento "
                      "no está disponible. " if memory_command else
                      "Modo limitado: el proveedor de razonamiento no está disponible. ")
        states = ", ".join(
            f"{row.extension_id}={row.state.name}" for row in self.registry.diagnostics()
            if row.capability == error.capability
        ) or "extensión no instalada"
        message = details.get(error.code, "No se pudo completar la solicitud.")
        uncertainty = (" El resultado es incierto; no repitas la operación sin comprobarlo."
                       if error.side_effect_uncertain else "")
        return SessionResult(redact_secrets(
            f"{prefix}{message} [{error.capability.value}/{error.code.value}; "
            f"{states}] {error.user_message}{uncertainty}"), kind="error")

    def _recall(self, query):
        try:
            return self.registry.execute(CapabilityKind.MEMORY, lambda port: port.recall(query)), ()
        except CapabilityError as error:
            return (), (f"Memoria no disponible para este turno. {self._error(error).text}",)

    def status(self):
        """Refresh only lazy local resolution/probes, never execute a capability."""
        for row in self.registry.diagnostics():
            if row.state == ExtensionState.ADMITTED:
                try:
                    self.registry.resolve(row.capability)
                except CapabilityError:
                    pass
        lines = ["extension | installed | enabled | compatible | available | state | active | model"]
        for row in self.registry.diagnostics():
            compatible = row.state != ExtensionState.REJECTED_INCOMPATIBLE
            enabled = compatible and row.state != ExtensionState.DISABLED
            available = ("yes" if row.state == ExtensionState.READY else
                         "limited" if row.state == ExtensionState.DEGRADED else "no")
            model = (self.model or self._configured_models.get(row.extension_id, "-")
                     if row.active and row.capability == CapabilityKind.REASONING
                     else self._configured_models.get(row.extension_id, "-"))
            lines.append(
                f"{row.extension_id} | yes | {'yes' if enabled else 'no'} | "
                f"{'yes' if compatible else 'no'} | {available} | "
                f"{row.state.name} | {'yes' if row.active else 'no'} | {model}"
                + (f" | {row.reason}" if row.reason else "")
            )
        lines.append(f"Active model (last confirmed): {self.model or 'not confirmed'}")
        lines.append("READY indica disponibilidad local; la red/cuota se verifica al enviar un turno.")
        return SessionResult(redact_secrets("\n".join(lines)), kind="status")

    def listen(self):
        started, finished, captured = Event(), Event(), []

        def transcribe(port):
            started.set()
            try:
                text = port.transcribe(b"")
                captured.append(text)
                return text
            finally:
                finished.set()

        try:
            if self._timed_out_recording:
                for row in self.registry.diagnostics():
                    if row.capability == CapabilityKind.STT and row.state == ExtensionState.FAILED:
                        self.registry.enable(row.extension_id)
                self._timed_out_recording = False
            text = self.registry.execute(CapabilityKind.STT, transcribe)
            return SessionResult(text, kind="input")
        except CapabilityError as error:
            if error.code == ErrorCode.TIMEOUT and started.is_set():
                # Bounded waiting cannot revoke an interactive stdin reader.
                # Preserve the original ENTER/interrupt lifecycle before the
                # terminal may ask another question. Never repeat recording.
                self._timed_out_recording = True
                finished.wait()
                if captured:
                    return SessionResult(captured[0], kind="input")
            return self._error(error)

    def speak(self, text):
        try:
            self.registry.execute(CapabilityKind.TTS, lambda port: port.speak(text))
            return SessionResult("", kind="speech")
        except CapabilityError as error:
            return self._error(error)

    def handle_text(self, text):
        text = text.strip()
        if text.lower() in {"salir", "exit", "quit"}:
            return SessionResult("Hasta luego.", kind="exit")
        if text.lower() == "/estado":
            return self.status()
        navigation = re.fullmatch(r"/navegar(?:\s+(.*))?", text, re.IGNORECASE | re.DOTALL)
        if navigation:
            arguments = (navigation.group(1) or "").split(maxsplit=1)
            if len(arguments) != 2:
                return SessionResult("Usa /navegar <https-url> <tarea de lectura>.", kind="error")
            try:
                if self._navigation_validator is not None:
                    self._navigation_validator(arguments[0])
                result = self.registry.execute(
                    CapabilityKind.NAVIGATION,
                    lambda port: port.navigate(WebNavigationRequest(*arguments)),
                    timeout_s=240,
                )
                return SessionResult(result.text, kind="navigation", url=result.url)
            except CapabilityError as error:
                return self._error(error)
        try:
            command = self._memory_command(text, self._extract, self._store)
            if command:
                if command.startswith("Hubo un error"):
                    return SessionResult(command, kind="error")
                # No prompt reload or secondary read after a committed mutation:
                # a read failure must never obscure a successful write.
                self.messages = [self.messages[0], *self.messages[1:],
                                 ChatMessage("user", text), ChatMessage("assistant", command)]
                self.messages = [self.messages[0], *self.messages[-10:]] if len(self.messages) > 11 else self.messages
                self._reload_prompt = True
                return SessionResult(command, kind="memory", mode="task")
        except CapabilityError as error:
            return self._error(error, memory_command=True)
        items, notices = self._recall(text)
        prompt = self._prompt(items, reload=self._reload_prompt)
        self._reload_prompt = False
        turn = [ChatMessage("system", prompt),
                *self.messages[1:], ChatMessage("user", text)]
        if len(turn) > 11:
            turn = [turn[0], *turn[-10:]]
        mode = self._classify(text)
        request = CompletionRequest(
            tuple(turn), TurnMode(mode["mode"]), self.model,
            mode["temperature"], mode["max_tokens"],
        )
        try:
            try:
                response = self._complete(request)
            except CapabilityError as error:
                if error.code not in (ErrorCode.EMPTY_RESPONSE, ErrorCode.INCOMPLETE_RESPONSE):
                    raise
                response = self._complete(replace(
                    request, max_tokens=request.max_tokens * 3, mode=TurnMode.TASK))
        except CapabilityError as error:
            return self._error(error)
        self.messages = [*turn, ChatMessage("assistant", response.text)]
        return SessionResult(response.text, mode=mode["mode"], notices=notices)
