import os
import re
import sys
import json
import threading
import unicodedata

# Suprimir el banner de pygame antes de importarlo
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "hide"

from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text
from rich.align import Align

import pyfiglet

import memory
from voice import VoiceEngine

console = Console()

DEFAULT_LOCAL_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen2.5:3b"

# Emojis por modo — se usan en el panel de respuesta y en la línea de debug interna
MODE_EMOJI = {
    "casual":    "💬",
    "task":      "📝",
    "reasoning": "🧠",
}

_CACHED_SYSTEM_PROMPT = None

# ─── Detección de borrado de memoria por orden directa ────────────────────────
_DELETE_PATTERNS = [
    r"\b(olvida|olv[ií]date|borra|elimina|quita|remove|forget|delete)\b.{0,40}\b(que|lo de|mi|mis|el dato|la info)?\b",
]

def _detect_delete_intent(text: str) -> str | None:
    """
    Retorna el keyword objetivo del borrado si el input parece una orden de olvidar.
    Ej: 'olvida que tengo 20 años' → '20 años' / 'edad'
    Retorna None si no es una orden de borrado.
    """
    lowered = text.lower().strip()
    for pat in _DELETE_PATTERNS:
        if re.search(pat, lowered):
            # Extraer lo que viene después del verbo de borrado
            # Quitar el verbo y palabras puente para aislar el sujeto
            cleaned = re.sub(
                r"^.{0,60}?(olvida(te)?|borra|elimina|quita|remove|forget|delete)\s*(que\s*)?",
                "", lowered, flags=re.IGNORECASE
            ).strip()
            # Quitar frases comunes que no son el dato
            cleaned = re.sub(r"^(tengo|soy|mi|mis|el|la|lo de)\s*", "", cleaned).strip()
            if len(cleaned) >= 2:
                return cleaned
    return None

# ─── System Prompt ─────────────────────────────────────────────────────────────

def get_system_prompt(reload: bool = False) -> str:
    """Carga y almacena en caché el prompt del sistema e inyecta la memoria persistente."""
    global _CACHED_SYSTEM_PROMPT
    if _CACHED_SYSTEM_PROMPT is None or reload:
        try:
            with open("system_prompt.txt", "r", encoding="utf-8") as f:
                base_prompt = f.read().strip()
        except FileNotFoundError:
            base_prompt = "Eres A.D.A (Eida), la perspicaz asistente del joven Dante."

        mem_context = memory.format_memories_for_prompt()
        _CACHED_SYSTEM_PROMPT = f"{base_prompt}\n\n{mem_context}"

    return _CACHED_SYSTEM_PROMPT

# ─── Cliente LLM ──────────────────────────────────────────────────────────────

def get_client():
    """Configuración de conexión con fallback a Local."""
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        use_cloud = Prompt.ask(
            "¿Usar modelo Nube (OpenAI) o Local (Ollama)?",
            choices=["nube", "local"],
            default="local"
        )
        if use_cloud == "nube":
            return OpenAI(api_key=api_key), "gpt-4o-mini"

    return OpenAI(base_url=DEFAULT_LOCAL_URL, api_key="not-needed"), DEFAULT_MODEL

# ─── Memoria asíncrona ────────────────────────────────────────────────────────

def _async_extract_and_save_memory(user_input: str, client: OpenAI, model: str):
    """
    Worker en segundo plano: analiza el input y guarda memoria si corresponde.
    No bloquea la respuesta principal.
    """
    if len(user_input.strip().split()) < 3:
        return
    if user_input.lower().strip() in {"hola", "buenos dias", "chau", "jaja", "xd"}:
        return

    extraction_prompt = (
        "Analiza el siguiente texto. "
        "Si contiene un dato personal del usuario (edad, cumpleaños, preferencia, proyecto, instrucción permanente), "
        "extrae la información en JSON estrictamente válido con: "
        "'should_save' (boolean), 'category' (string), 'key' (string), 'value' (string). "
        "Si es charla casual o efímera, responde solo {\"should_save\": false}.\n\n"
        f"Texto: \"{user_input}\""
    )

    try:
        res = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": extraction_prompt}],
            temperature=0.1
        )
        content = res.choices[0].message.content.strip()

        if "{" in content and "}" in content:
            json_str = content[content.find("{"):content.rfind("}") + 1]
            data = json.loads(json_str)
            if isinstance(data, dict) and data.get("should_save"):
                memory.save_memory(
                    category=str(data.get("category", "general")),
                    key=str(data.get("key", "dato")),
                    value=str(data.get("value", ""))
                )
                get_system_prompt(reload=True)
                # Notificación visible discreta
                key_label = data.get("key", "dato")
                val_label = data.get("value", "")
                console.print(f"  [dim magenta]🧠 {key_label} → {val_label}[/dim magenta]")
    except Exception:
        pass

def schedule_memory_check(user_input: str, client: OpenAI, model: str):
    """Lanza el análisis de memoria en un hilo secundario (latencia 0ms)."""
    t = threading.Thread(
        target=_async_extract_and_save_memory,
        args=(user_input, client, model),
        daemon=True
    )
    t.start()

# ─── Clasificación de modo ────────────────────────────────────────────────────

def classify_input_mode(text: str) -> dict:
    """
    Clasifica el input en casual / task / reasoning usando heurísticas locales.
    Prioridad: casual → reasoning → task
    """
    CASUAL_SIGNALS = {
        "hola", "buenas", "buenos días", "buenas tardes", "buenas noches",
        "qué tal", "cómo estás", "cómo vas", "cómo te va",
        "hi", "hey", "good morning", "good night", "good afternoon",
        "jaja", "jeje", "xd", "lol", "ok", "okay", "sí", "no", "dale",
        "claro", "entendido", "gracias", "thanks", "perfecto", "genial",
        "bien", "mal", "uff", "wow", "ah", "oh", "ya", "vale", "np",
        "de nada", "no hay de qué", "con gusto",
    }

    TASK_KEYWORDS = {
        "planifica", "planea", "organiza", "crea", "escribe", "redacta",
        "lista", "enumera", "describe", "genera", "diseña", "resume",
        "traduce", "calcula", "desarrolla", "implementa", "configura",
        "cuéntame", "cuentame", "busca", "muéstrame", "muestrame", "dame", "hazme",
        "elabora", "elaborar", "prepara", "preparar", "construye", "construir",
        "sugiere", "sugerir", "propón", "propon", "proponer", "corrige", "corregir",
        "planificación", "planificacion", "borrador", "ideas", "ejemplo", "ejemplos",
        "plantilla", "estructura", "esquema", "guía", "guia", "tutorial", "código", "codigo", "script",
        "plan", "create", "write", "list", "describe", "generate", "design",
        "summarize", "translate", "calculate", "implement", "configure",
        "show me", "give me", "make me", "find", "suggest", "draft", "code", "template",
        "semana", "semanas", "día", "dia", "días", "dias", "mes", "meses", "mañana", "manana",
        "pasos", "paso", "agenda", "horario", "rutina", "cronograma",
        "tareas", "tarea", "proyecto", "itinerario", "calendario",
        "week", "days", "month", "tomorrow", "steps", "schedule",
        "tasks", "project", "itinerary", "calendar",
    }

    REASONING_KEYWORDS = {
        "debería", "deberia", "deberías", "deberias", "vale más", "vale mas", "conviene",
        "es mejor", "cuál es mejor", "cual es mejor", "cuál elegiría", "cual elegiria",
        "qué harías", "que harias", "qué elegirías", "que elegirias",
        "me recomiendas", "me recomendarías", "me recomendarias",
        "ventajas", "desventajas", "pros", "contras", "diferencia entre",
        "versus", "vs", "comparar", "compara", "contrasta",
        "a favor", "en contra", "trade-off", "tradeoff",
        "analiza", "evalúa", "evalua", "razona", "argumenta", "justifica",
        "explica por qué", "explica por que", "por qué crees", "por que crees",
        "qué opinas", "que opinas", "qué piensas", "que piensas",
        "cuál es tu opinión", "cual es tu opinion", "qué tan viable", "que tan viable",
        "es viable", "es posible que", "tiene sentido", "vale la pena", "merece la pena",
        "optimiza", "maximiza", "minimiza", "de la manera más eficiente", "de la manera mas eficiente",
        "considerando que", "teniendo en cuenta", "dado que", "si tuvieras que",
        "en caso de que", "suponiendo que", "bajo qué condiciones", "bajo que condiciones",
        "cuál sería la mejor forma", "cual seria la mejor forma",
        "cómo priorizarías", "como priorizarias",
        "es correcto", "es ético", "es etico", "moralmente", "filosóficamente", "filosoficamente",
        "dilema", "conflicto de", "en tu opinión", "en tu opinion",
        "should i", "should we", "is it better", "which is better",
        "recommend", "pros and cons", "advantages", "disadvantages",
        "compare", "contrast", "versus", "trade-off", "worth it",
        "analyze", "evaluate", "reason", "argue", "justify",
        "explain why", "what do you think", "your opinion", "feasible",
        "makes sense", "worth", "viable",
        "optimize", "maximize", "minimize", "most efficient",
        "considering that", "given that", "if you had to",
        "best way to", "how would you prioritize",
        "ethical", "morally", "dilemma", "philosophically",
    }

    REASONING_PATTERNS = [
        r"\b(si|if)\b.{10,}.*(pero|aunque|sin embargo|however|but|yet)\b",
        r"(teniendo en cuenta|considerando|dado que|given that|considering).{5,}(y|,|e)\b",
        r"\b(entre|between)\b.{3,}\b(y|and|o|or)\b.{3,}\b(cual|which|mejor|better)\b",
        r"(por un lado|on one hand|on the other hand|por otro lado)",
        r"(.*(,|y|pero|aunque|sin embargo|however|but).{5,}){2,}",
        r"\b(si tuvieras|si pudieras|what would you|if you had to|if you could)\b",
        r"\b(que implicaciones|what implications|por que crees|why do you think)\b",
    ]

    lowered = text.lower().strip()
    normalized = unicodedata.normalize("NFD", lowered)
    normalized = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    words = normalized.split()
    word_count = len(words)

    def _hit(keyword_set: set) -> bool:
        for kw in keyword_set:
            if re.search(r"\b" + re.escape(kw) + r"\b", lowered):
                return True
            kw_norm = unicodedata.normalize("NFD", kw)
            kw_norm = "".join(c for c in kw_norm if unicodedata.category(c) != "Mn")
            if re.search(r"\b" + re.escape(kw_norm) + r"\b", normalized):
                return True
        return False

    # 1. CASUAL (prioridad máxima para inputs cortos)
    if word_count <= 5 and _hit(CASUAL_SIGNALS):
        return {"mode": "casual", "max_tokens": 256, "temperature": 0.4, "think": False}

    # 2. REASONING
    reasoning_keyword_hit = _hit(REASONING_KEYWORDS)
    reasoning_pattern_hit = any(re.search(p, normalized) for p in REASONING_PATTERNS)
    complexity_by_length = (
        word_count >= 25
        and any(w in normalized for w in {
            "o", "o bien", "pero", "aunque", "sin embargo",
            "or", "but", "however", "although", "yet",
            "mientras", "while", "a menos que", "unless",
        })
    )

    if reasoning_keyword_hit or reasoning_pattern_hit or complexity_by_length:
        return {"mode": "reasoning", "max_tokens": 1500, "temperature": 0.3, "think": True}

    # 3. TASK
    if _hit(TASK_KEYWORDS):
        return {"mode": "task", "max_tokens": 700, "temperature": 0.3, "think": False}

    if word_count > 12:
        return {"mode": "task", "max_tokens": 700, "temperature": 0.3, "think": False}

    # 4. Default
    return {"mode": "casual", "max_tokens": 256, "temperature": 0.4, "think": False}

# ─── UI helpers ───────────────────────────────────────────────────────────────

def print_header():
    """Imprime el header de ADA: título ASCII + subtítulo."""
    title_art = pyfiglet.figlet_format("A.D.A", font="slant")
    console.print()
    console.print(Align.center(Text(title_art.rstrip(), style="bold magenta")))
    console.print(Align.center(Text("Advanced Desktop Assistant", style="dim magenta")))
    console.print(Align.center(Text("- " * 24, style="magenta")))
    console.print()

def print_ada_response(text: str, mode: str):
    """Imprime la respuesta de Ada en un panel con el emoji del modo."""
    emoji = MODE_EMOJI.get(mode, "💬")
    console.print(Panel(
        text,
        title=f"[bold magenta]Ada {emoji}[/bold magenta]",
        title_align="left",
        border_style="magenta",
        padding=(0, 1),
    ))

# ─── Bucle principal ──────────────────────────────────────────────────────────

def chat_loop():
    """Bucle principal de conversación (Voz siempre activa + Texto)."""
    memory.init_db()
    print_header()

    client, model = get_client()

    # Voz siempre activa
    voice_engine = VoiceEngine(mode="edge")

    messages = [
        {"role": "system", "content": get_system_prompt()}
    ]

    while True:
        try:
            user_input = ""

            # Indicador de grabación
            console.print("[bold magenta]🔴[/bold magenta] ", end="")
            typed_input = input("> ").strip()

            if typed_input:
                # El usuario escribió directamente
                user_input = typed_input
            else:
                # ENTER vacío → grabar voz
                user_input = voice_engine.listen_manual_push()
                if user_input:
                    console.print(f"[dim]💬 {user_input}[/dim]")

            if not user_input:
                continue

            if user_input.strip().lower() in {"salir", "exit", "quit"}:
                farewell = "Hasta luego, Dante."
                print_ada_response(farewell, "casual")
                voice_engine.speak(farewell)
                break

            # ── Detección de borrado de memoria por orden directa ─────────────
            delete_keyword = _detect_delete_intent(user_input)
            if delete_keyword:
                deleted_keys = memory.delete_memory_fuzzy(delete_keyword)
                get_system_prompt(reload=True)
                if deleted_keys:
                    for k in deleted_keys:
                        console.print(f"  [dim magenta]🗑️  Olvidado: {k}[/dim magenta]")
                    confirmation = "Olvidado, Dante."
                else:
                    confirmation = "No encontré nada que olvidar sobre eso."
                print_ada_response(confirmation, "casual")
                voice_engine.speak(confirmation)
                messages.append({"role": "user", "content": user_input})
                messages.append({"role": "assistant", "content": confirmation})
                continue

            # ── Análisis de memoria en segundo plano ──────────────────────────
            schedule_memory_check(user_input, client, model)

            # ── Actualizar system prompt con memoria actual ────────────────────
            messages[0] = {"role": "system", "content": get_system_prompt()}
            messages.append({"role": "user", "content": user_input})

            # Ventana de contexto deslizante (últimos 10 mensajes)
            if len(messages) > 11:
                messages = [messages[0]] + messages[-10:]

            # ── Clasificar modo ────────────────────────────────────────────────
            mode_info  = classify_input_mode(user_input)
            mode       = mode_info["mode"]
            temperature = mode_info["temperature"]
            max_tokens  = mode_info["max_tokens"]
            think       = mode_info["think"]

            # ── Llamada al modelo ──────────────────────────────────────────────
            think_label = "Ada razonando..." if think else "Ada..."
            with console.status(f"[dim magenta]{think_label}[/dim magenta]"):
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    extra_body={"options": {"think": think, "num_ctx": 2048}},
                )

            ada_response = response.choices[0].message.content.strip()

            # Guard: respuesta vacía (thinking consumió el budget)
            if not ada_response:
                with console.status("[dim magenta]Reintentando...[/dim magenta]"):
                    response = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens * 3,
                        extra_body={"options": {"think": False, "num_ctx": 2048}},
                    )
                ada_response = response.choices[0].message.content.strip()

            if not ada_response:
                ada_response = "..."

            print_ada_response(ada_response, mode)
            messages.append({"role": "assistant", "content": ada_response})

            voice_engine.speak(ada_response)

        except KeyboardInterrupt:
            console.print("\n[dim magenta]Nos vemos, Dante.[/dim magenta]")
            break
        except Exception as e:
            console.print(f"\n[red]❌ {e}[/red]\n")

if __name__ == "__main__":
    chat_loop()
