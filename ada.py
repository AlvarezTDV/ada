import os
import re
import sys
import json
import unicodedata
import argparse

# Suprimir el banner de pygame antes de importarlo
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "hide"

import openai
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
import pyfiglet

import memory
from voice import VoiceEngine
from browser_navigation import BrowserError, run_task as run_browser_task

console = Console()

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODELS = ("llama-3.3-70b-versatile", "openai/gpt-oss-20b")
DEFAULT_GROQ_MODEL = DEFAULT_GROQ_MODELS[0]
DEFAULT_LOCAL_URL = "http://localhost:11434/v1"
DEFAULT_LOCAL_MODEL = "qwen2.5:3b"
DEFAULT_MODEL = DEFAULT_GROQ_MODEL
CHAT_TIMEOUT_SECONDS = 25.0


class ChatConfigError(Exception):
    pass

MODE_EMOJI = {
    "casual":    "💬",
    "task":      "📝",
    "reasoning": "🧠",
}

_CACHED_SYSTEM_PROMPT = None

def get_system_prompt(reload: bool = False, *, query: str = "") -> str:
    """Cache base instructions only; select bounded memory for the current turn."""
    global _CACHED_SYSTEM_PROMPT
    if _CACHED_SYSTEM_PROMPT is None or reload:
        try:
            with open("system_prompt.txt", "r", encoding="utf-8") as f:
                base_prompt = f.read().strip()
        except FileNotFoundError:
            base_prompt = "Eres A.D.A., una asistente de IA personal."

        _CACHED_SYSTEM_PROMPT = base_prompt

    return f"{_CACHED_SYSTEM_PROMPT}\n\n{memory.select_memories_for_prompt(query)}"

def get_groq_model_chain() -> list[str]:
    configured = os.getenv("ADA_GROQ_MODELS")
    if configured and configured.strip():
        models = [model.strip() for model in configured.split(",") if model.strip()]
        return models or list(DEFAULT_GROQ_MODELS)
    legacy = (os.getenv("ADA_GROQ_MODEL") or "").strip()
    if configured is None and legacy:
        return list(dict.fromkeys([legacy, *DEFAULT_GROQ_MODELS]))
    return list(DEFAULT_GROQ_MODELS)


def get_client(provider: str = "groq") -> tuple[OpenAI, str]:
    provider = (provider or os.getenv("ADA_CHAT_PROVIDER") or "groq").strip().lower()
    if provider == "groq":
        api_key = (os.getenv("GROQ_API_KEY") or "").strip()
        if not api_key:
            raise ChatConfigError("Falta GROQ_API_KEY. Configúrala en tu entorno o inicia con --provider local.")
        return OpenAI(base_url=GROQ_BASE_URL, api_key=api_key,
                      timeout=CHAT_TIMEOUT_SECONDS, max_retries=0), get_groq_model_chain()[0]
    if provider == "local":
        return OpenAI(base_url=DEFAULT_LOCAL_URL, api_key="not-needed",
                      timeout=CHAT_TIMEOUT_SECONDS, max_retries=0), (os.getenv("ADA_LOCAL_MODEL") or DEFAULT_LOCAL_MODEL).strip()
    raise ChatConfigError("Proveedor de conversación no válido. Usa --provider groq o --provider local.")


def _is_reasoning_model(model: str) -> bool:
    return model.lower().startswith(("openai/gpt-oss-", "qwen/qwen3", "deepseek-r1"))


def _is_local_client(client, model: str) -> bool:
    base_url = str(getattr(client, "base_url", ""))
    return (any(host in base_url for host in ("11434", "localhost", "127.0.0.1"))
            or (model == DEFAULT_LOCAL_MODEL and not base_url.startswith(GROQ_BASE_URL)))


def _build_completion_kwargs(client, model: str, messages: list, *,
                             temperature: float, max_tokens: int, think: bool) -> dict:
    kwargs = {"model": model, "messages": messages,
              "temperature": temperature, "max_tokens": max_tokens}
    if _is_local_client(client, model):
        kwargs["extra_body"] = {"options": {"think": think, "num_ctx": 2048}}
    elif _is_reasoning_model(model):
        kwargs["extra_body"] = {"reasoning_effort": "medium" if think else "low",
                                "include_reasoning": False}
    return kwargs


def _redact_provider_text(text: str) -> str:
    api_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    return re.sub(r"gsk_[A-Za-z0-9]+", "[REDACTED]", text)


def create_chat_completion_with_fallback(client, model: str, messages: list, *,
                                         temperature: float, max_tokens: int,
                                         think: bool = False, provider: str = "groq") -> tuple[object, str]:
    chain = get_groq_model_chain()
    candidates = [model]
    if ((provider == "groq" or str(getattr(client, "base_url", "")).startswith(GROQ_BASE_URL))
            and not _is_local_client(client, model) and model in chain):
        candidates = chain[chain.index(model):]
    for index, candidate in enumerate(candidates):
        # A rotated reasoning model also needs room for its internal tokens.
        budget = max(max_tokens, 300) if _is_reasoning_model(candidate) else max_tokens
        try:
            response = client.chat.completions.create(**_build_completion_kwargs(
                client, candidate, messages, temperature=temperature,
                max_tokens=budget, think=think))
            return response, candidate
        except openai.RateLimitError:
            if index + 1 == len(candidates):
                raise
            notice = _redact_provider_text(
                f"Cuota agotada en {candidate}; cambiando a modelo de respaldo: {candidates[index + 1]}")
            console.print(Text(notice, style="dim #fd2636"))


def format_provider_error(error: Exception) -> str:
    if isinstance(error, ChatConfigError):
        message = str(error)
    elif isinstance(error, openai.AuthenticationError):
        message = "Error de autenticación (401). Revisa la clave del proveedor de conversación."
    elif isinstance(error, openai.PermissionDeniedError):
        message = "Acceso denegado (403). Revisa los permisos de la clave y del modelo."
    elif isinstance(error, openai.RateLimitError):
        message = "Cuota agotada (429) en todos los modelos disponibles. Inténtalo más tarde."
    elif isinstance(error, openai.APITimeoutError):
        message = "Se agotó el tiempo de espera del proveedor. Inténtalo de nuevo."
    elif isinstance(error, openai.APIConnectionError):
        message = "No se pudo establecer conexión con el proveedor. Revisa la red o el servicio local."
    elif isinstance(error, openai.BadRequestError):
        message = "El proveedor rechazó la solicitud (400). Revisa el modelo y sus parámetros."
    else:
        message = "No se pudo completar la operación. Inténtalo de nuevo."
    return _redact_provider_text(message)


def _completion_text(response) -> str:
    if not response.choices or getattr(response.choices[0], "finish_reason", None) == "length":
        return ""
    return (response.choices[0].message.content or "").strip()

# ─── Comandos Directos de Memoria (Arquitectura Híbrida) ──────────────────────

# Permite prefijos opcionales amables ("por favor", "puedes") seguidos del verbo
SAVE_TRIGGERS = r"^(?:por favor\s+|puedes\s+)?(?:anota|guarda|recuerda|memoriza)\b"
DELETE_TRIGGERS = r"^(?:por favor\s+|puedes\s+)?(?:borra|olvida|elimina)\b"


def _parse_memory_array(content: str) -> list:
    """Accept a JSON array, retaining support for prose or Markdown wrappers."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if not match:
            raise ValueError("Expected a JSON array")
        data = json.loads(match.group(0))
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array")
    return data


def execute_memory_command(text: str, client: OpenAI, model: str) -> str | None:
    """
    Detecta si el usuario usó una palabra clave al INICIO de su frase.
    Si es así, invoca al LLM puramente como parser JSON, ejecuta la DB, 
    y retorna la respuesta confirmada (saltando el flujo de chat normal).
    Retorna None si no es un comando explícito.
    """
    lowered = text.lower().strip()
    
    # 1. COMANDO ANOTA (Guarda / Actualiza)
    if re.search(SAVE_TRIGGERS, lowered):
        prompt = f"""El usuario dio una orden directa de guardar información.
Extrae los datos en un array de JSON. Cada objeto debe tener: "category", "key", "value".
REGLAS ESTRICTAS:
- El campo "key" DEBE estar siempre en ESPAÑOL, ser descriptivo y usar guiones bajos (ej: edad, color_favorito, fecha_cumpleaños). ¡Nunca en inglés!
Texto: "{text}"
Responde ÚNICAMENTE con el array JSON válido."""
        try:
            with console.status("[dim #fd2636]Procesando orden de guardado...[/dim #fd2636]"):
                res, _ = create_chat_completion_with_fallback(
                    client, model, [{"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=max(150, 300) if _is_reasoning_model(model) else 150,
                    provider="local" if _is_local_client(client, model) else "groq",
                )
            content = _completion_text(res)
            data = _parse_memory_array(content)
            saved = memory.save_memories(data)
            for item in saved:
                console.print(f"  [dim #fd2636]🧠 Anotado: {item['key']} -> {item['value']}[/dim #fd2636]")
            return "Información guardada."
        except openai.APIError:
            raise
        except Exception:
            pass
        return "Hubo un error al intentar extraer y guardar la información."

    # 2. COMANDO BORRA (Elimina)
    if re.search(DELETE_TRIGGERS, lowered):
        memories = memory.get_all_memories()
        if not memories:
            return "Mi memoria ya está vacía."
        
        # Le pasamos la lista de claves que existen para que sepa cuál escoger
        keys = [m[1] for m in memories]
        prompt = f"""El usuario ordenó borrar un dato.
Claves actuales en memoria: {keys}
Texto del usuario: "{text}"
¿A cuál clave de la lista se refiere el usuario por contexto o sinónimos? (Ej: si dice "mis años" y en la lista está "edad", debes deducir "edad").
Responde ÚNICAMENTE con un array JSON de strings con las claves de la lista que coincidan lógicamente. Si no hay coincidencias semánticas, responde []."""
        try:
            with console.status("[dim #fd2636]Procesando orden de borrado...[/dim #fd2636]"):
                res, _ = create_chat_completion_with_fallback(
                    client, model, [{"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=max(50, 300) if _is_reasoning_model(model) else 50,
                    provider="local" if _is_local_client(client, model) else "groq",
                )
            content = _completion_text(res)
            keys_to_delete = memory.validate_memory_keys(_parse_memory_array(content))
            if any(key not in keys for key in keys_to_delete):
                raise ValueError("Deletion keys must belong to the offered memories")
            deleted = memory.delete_memories(keys_to_delete)
            if deleted:
                for k in deleted:
                    console.print(f"  [dim #fd2636]🗑️ Olvidado: {k}[/dim #fd2636]")
                return "Información eliminada de mi base de datos."
            return "No encontré ese dato en mi memoria para borrarlo."
        except openai.APIError:
            raise
        except Exception:
            pass
        return "Hubo un error al procesar el borrado de memoria."

    # Si no empezó con una palabra clave, retornamos None para seguir como Chat normal
    return None

# ─── Clasificación de modo (Conversación General) ─────────────────────────────

def classify_input_mode(text: str) -> dict:
    CASUAL_SIGNALS = {"hola", "buenas", "buenos días", "buenas tardes", "buenas noches", "qué tal", "cómo estás", "cómo vas", "cómo te va", "hi", "hey", "jaja", "jeje", "xd", "lol", "ok", "okay", "sí", "no", "dale", "claro", "entendido", "gracias", "perfecto", "genial", "bien", "mal", "uff", "wow", "ah", "oh", "ya", "vale", "np"}
    TASK_KEYWORDS = {"planifica", "planea", "organiza", "crea", "escribe", "redacta", "lista", "enumera", "describe", "genera", "diseña", "resume", "traduce", "calcula", "desarrolla", "implementa", "configura", "cuéntame", "cuentame", "busca", "muéstrame", "muestrame", "dame", "hazme", "elabora", "elaborar", "prepara", "preparar", "construye", "construir", "sugiere", "sugerir", "propón", "propon", "proponer", "corrige", "corregir", "semana", "día", "mes"}
    REASONING_KEYWORDS = {"debería", "deberia", "vale más", "conviene", "es mejor", "cuál es mejor", "qué harías", "me recomiendas", "ventajas", "desventajas", "pros", "contras", "diferencia entre", "versus", "vs", "comparar", "compara", "contrasta", "analiza", "evalúa", "razona", "argumenta", "justifica", "explica por qué", "qué opinas", "qué piensas", "es viable", "tiene sentido", "vale la pena", "optimiza", "maximiza", "minimiza", "es ético", "dilema"}
    REASONING_PATTERNS = [r"\b(si|if)\b.{10,}.*(pero|aunque|sin embargo|however|but|yet)\b", r"(teniendo en cuenta|considerando|dado que).{5,}(y|,|e)\b", r"\b(entre|between)\b.{3,}\b(y|and|o|or)\b.{3,}\b(cual|which|mejor|better)\b"]

    lowered = text.lower().strip()
    normalized = unicodedata.normalize("NFD", lowered)
    normalized = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    words = normalized.split()
    word_count = len(words)

    def _hit(keyword_set: set) -> bool:
        for kw in keyword_set:
            if re.search(r"\b" + re.escape(kw) + r"\b", lowered): return True
            kw_norm = "".join(c for c in unicodedata.normalize("NFD", kw) if unicodedata.category(c) != "Mn")
            if re.search(r"\b" + re.escape(kw_norm) + r"\b", normalized): return True
        return False

    if word_count <= 5 and _hit(CASUAL_SIGNALS):
        return {"mode": "casual", "max_tokens": 256, "temperature": 0.4, "think": False}

    if _hit(REASONING_KEYWORDS) or any(re.search(p, normalized) for p in REASONING_PATTERNS):
        return {"mode": "reasoning", "max_tokens": 1500, "temperature": 0.3, "think": True}

    if _hit(TASK_KEYWORDS) or word_count > 12:
        return {"mode": "task", "max_tokens": 700, "temperature": 0.3, "think": False}

    return {"mode": "casual", "max_tokens": 256, "temperature": 0.4, "think": False}

# ─── UI Helpers ───────────────────────────────────────────────────────────────

def print_header():
    title_art = pyfiglet.figlet_format("A.D.A", font="slant")
    console.print()
    console.print(Align.center(Text(title_art.rstrip(), style="bold #fd2636")))
    console.print(Align.center(Text("Advanced Desktop Assistant", style="dim #fd2636")))
    console.print(Align.center(Text("- " * 30, style="#fd2636")))
    console.print()

def print_ada_response(text: str, mode: str):
    emoji = MODE_EMOJI.get(mode, "💬")
    # Al no fijar el width, el Panel se adaptará automáticamente al ancho de la terminal al momento de imprimirse
    console.print(Panel(text, title=f"[bold #fd2636]Ada {emoji}[/bold #fd2636]", title_align="left", border_style="#fd2636", padding=(0, 1)))

# ─── Bucle Principal ──────────────────────────────────────────────────────────

def chat_loop(speech_enabled=False, provider="groq"):
    memory.init_db()
    print_header()

    client, model = get_client(provider)
    voice_engine = VoiceEngine(mode="edge")

    messages = [{"role": "system", "content": get_system_prompt()}]

    while True:
        try:
            user_input = ""
            console.print("[bold #fd2636]🔴[/bold #fd2636] ", end="")
            typed_input = input("> ").strip()

            if typed_input:
                user_input = typed_input
            else:
                user_input = voice_engine.listen_manual_push()
                
                if user_input:
                    # Keep audio failure notices visible when transcription is empty.
                    sys.stdout.write("\033[1A\033[2K\033[1A\033[2K\r")
                    sys.stdout.flush()
                    # Reescribimos el prompt original pero esta vez con la voz transcrita en la misma línea
                    console.print(f"[bold #fd2636]🔴[/bold #fd2636] > [dim]{user_input}[/dim]")

            if not user_input:
                continue

            if user_input.strip().lower() in {"salir", "exit", "quit"}:
                farewell = "Hasta luego."
                print_ada_response(farewell, "casual")
                if speech_enabled:
                    voice_engine.speak(farewell)
                break

            # ── 1. Intercepción de Comandos Explícitos ──
            navigation = re.fullmatch(r"/navegar(?:\s+(.*))?", user_input, re.IGNORECASE | re.DOTALL)
            if navigation:
                try:
                    arguments = (navigation.group(1) or "").split(maxsplit=1)
                    if len(arguments) != 2:
                        raise BrowserError("Usa /navegar <https-url> <tarea de lectura>.")
                    result = run_browser_task(*arguments)
                    console.print(Panel(Text(result.text), title="Navegación local",
                                        border_style="#fd2636", padding=(0, 1)))
                    console.print(Text(result.url))
                except BrowserError as error:
                    console.print(Text(str(error), style="red"))
                # External results never enter chat history, memory or online speech.
                continue

            cmd_response = execute_memory_command(user_input, client, model)
            if cmd_response:
                if cmd_response.startswith("Hubo un error"):
                    console.print(Text(format_provider_error(ChatConfigError(cmd_response)), style="red"))
                    continue
                # Si el usuario dio un comando, ejecutamos DB, damos la respuesta rápida y cortamos aquí.
                print_ada_response(cmd_response, "task")
                if speech_enabled:
                    voice_engine.speak(cmd_response)
                
                # Actualizamos historial y recargamos system prompt para turnos futuros
                messages.append({"role": "user", "content": user_input})
                messages.append({"role": "assistant", "content": cmd_response})
                messages[0] = {"role": "system", "content": get_system_prompt(reload=True)}
                if len(messages) > 11: messages = [messages[0]] + messages[-10:]
                
                continue  # NO invocamos al LLM para charlar. Salto directo al próximo turno.

            # ── 2. Conversación Normal ──
            turn_messages = [{"role": "system", "content": get_system_prompt(query=user_input)},
                             *messages[1:], {"role": "user", "content": user_input}]
            if len(turn_messages) > 11:
                turn_messages = [turn_messages[0]] + turn_messages[-10:]

            mode_info = classify_input_mode(user_input)
            mode, temperature, max_tokens, think = mode_info["mode"], mode_info["temperature"], mode_info["max_tokens"], mode_info["think"]

            think_label = "Ada razonando..." if think else "Ada..."
            with console.status(f"[dim #fd2636]{think_label}[/dim #fd2636]"):
                response, model = create_chat_completion_with_fallback(
                    client, model, turn_messages, temperature=temperature, max_tokens=max_tokens,
                    think=think, provider=provider,
                )
            ada_response = _completion_text(response)

            if not ada_response:
                with console.status("[dim #fd2636]Reintentando...[/dim #fd2636]"):
                    fallback_response, model = create_chat_completion_with_fallback(
                        client, model, turn_messages, temperature=temperature, max_tokens=max_tokens * 3,
                        think=False, provider=provider,
                    )
                ada_response = _completion_text(fallback_response)

            if not ada_response:
                raise ChatConfigError("El proveedor devolvió una respuesta vacía o truncada tras el reintento.")

            print_ada_response(ada_response, mode)
            messages = [*turn_messages, {"role": "assistant", "content": ada_response}]
            if speech_enabled:
                voice_engine.speak(ada_response)

        except KeyboardInterrupt:
            console.print("\n[dim #fd2636]Nos vemos.[/dim #fd2636]")
            break
        except Exception as e:
            console.print(Text(f"\n❌ {format_provider_error(e)}\n", style="red"))


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ADA: chat por texto con audio opcional.",
                                     epilog="En el chat: /navegar <https-url> <tarea> usa Browser Use, "
                                            "Edge y Ollama locales; consulta README para preparar el entorno opcional.")
    parser.add_argument("--speak", action="store_true",
                        help="Activar respuestas habladas (Edge TTS online con respaldo local).")
    parser.add_argument("--provider", choices=["groq", "local"], default="groq",
                        help="Proveedor de conversación: groq (por defecto) o local (Ollama).")
    return parser


if __name__ == "__main__":
    args = build_argument_parser().parse_args()
    try:
        chat_loop(speech_enabled=args.speak, provider=args.provider)
    except ChatConfigError as error:
        console.print(Text(format_provider_error(error), style="red"))
        sys.exit(1)
