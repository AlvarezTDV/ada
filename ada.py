import os
import re
import sys
import json
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

MODE_EMOJI = {
    "casual":    "💬",
    "task":      "📝",
    "reasoning": "🧠",
}

_CACHED_SYSTEM_PROMPT = None

def get_system_prompt(reload: bool = False) -> str:
    """Carga el system prompt e inyecta la BD de memoria viva."""
    global _CACHED_SYSTEM_PROMPT
    if _CACHED_SYSTEM_PROMPT is None or reload:
        try:
            with open("system_prompt.txt", "r", encoding="utf-8") as f:
                base_prompt = f.read().strip()
        except FileNotFoundError:
            base_prompt = "Eres A.D.A, la asistente de Dante."

        mem_context = memory.format_memories_for_prompt()
        _CACHED_SYSTEM_PROMPT = f"{base_prompt}\n\n{mem_context}"

    return _CACHED_SYSTEM_PROMPT

def get_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        use_cloud = Prompt.ask("¿Usar Nube o Local?", choices=["nube", "local"], default="local")
        if use_cloud == "nube":
            return OpenAI(api_key=api_key), "gpt-4o-mini"
    return OpenAI(base_url=DEFAULT_LOCAL_URL, api_key="not-needed"), DEFAULT_MODEL

# ─── Comandos Directos de Memoria (Arquitectura Híbrida) ──────────────────────

# Permite prefijos opcionales amables ("por favor", "puedes") seguidos del verbo
SAVE_TRIGGERS = r"^(?:por favor\s+|puedes\s+)?(?:anota|guarda|recuerda|memoriza)\b"
DELETE_TRIGGERS = r"^(?:por favor\s+|puedes\s+)?(?:borra|olvida|elimina)\b"

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
            with console.status("[dim magenta]Procesando orden de guardado...[/dim magenta]"):
                res = client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=150
                )
            content = res.choices[0].message.content.strip()
            # Buscar el bloque JSON en la respuesta por si generó texto extra
            match = re.search(r"\[.*\]", content, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                memory.save_memories(data)
                for item in data:
                    console.print(f"  [dim magenta]🧠 Anotado: {item.get('key')} -> {item.get('value')}[/dim magenta]")
                return "Información guardada, Dante."
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
            with console.status("[dim magenta]Procesando orden de borrado...[/dim magenta]"):
                res = client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=50
                )
            content = res.choices[0].message.content.strip()
            match = re.search(r"\[.*\]", content, re.DOTALL)
            if match:
                keys_to_delete = json.loads(match.group(0))
                if keys_to_delete:
                    deleted = memory.delete_memories(keys_to_delete)
                    for k in deleted:
                        console.print(f"  [dim magenta]🗑️ Olvidado: {k}[/dim magenta]")
                    return "Información eliminada de mi base de datos."
                else:
                    return "No encontré ese dato en mi memoria para borrarlo."
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
    console.print(Align.center(Text(title_art.rstrip(), style="bold magenta")))
    console.print(Align.center(Text("Advanced Desktop Assistant", style="dim magenta")))
    console.print(Align.center(Text("- " * 24, style="magenta")))
    console.print()

def print_ada_response(text: str, mode: str):
    emoji = MODE_EMOJI.get(mode, "💬")
    console.print(Panel(text, title=f"[bold magenta]Ada {emoji}[/bold magenta]", title_align="left", border_style="magenta", padding=(0, 1)))

# ─── Bucle Principal ──────────────────────────────────────────────────────────

def chat_loop():
    memory.init_db()
    print_header()

    client, model = get_client()
    voice_engine = VoiceEngine(mode="edge")

    messages = [{"role": "system", "content": get_system_prompt()}]

    while True:
        try:
            user_input = ""
            console.print("[bold magenta]🔴[/bold magenta] ", end="")
            typed_input = input("> ").strip()

            if typed_input:
                user_input = typed_input
            else:
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

            # ── 1. Intercepción de Comandos Explícitos ──
            cmd_response = execute_memory_command(user_input, client, model)
            if cmd_response:
                # Si el usuario dio un comando, ejecutamos DB, damos la respuesta rápida y cortamos aquí.
                print_ada_response(cmd_response, "task")
                voice_engine.speak(cmd_response)
                
                # Actualizamos historial y recargamos system prompt para turnos futuros
                messages.append({"role": "user", "content": user_input})
                messages.append({"role": "assistant", "content": cmd_response})
                messages[0] = {"role": "system", "content": get_system_prompt(reload=True)}
                if len(messages) > 11: messages = [messages[0]] + messages[-10:]
                
                continue  # NO invocamos al LLM para charlar. Salto directo al próximo turno.

            # ── 2. Conversación Normal ──
            messages[0] = {"role": "system", "content": get_system_prompt()}
            messages.append({"role": "user", "content": user_input})
            if len(messages) > 11:
                messages = [messages[0]] + messages[-10:]

            mode_info = classify_input_mode(user_input)
            mode, temperature, max_tokens, think = mode_info["mode"], mode_info["temperature"], mode_info["max_tokens"], mode_info["think"]

            think_label = "Ada razonando..." if think else "Ada..."
            with console.status(f"[dim magenta]{think_label}[/dim magenta]"):
                response = client.chat.completions.create(
                    model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
                    extra_body={"options": {"think": think, "num_ctx": 2048}},
                )
            ada_response = response.choices[0].message.content.strip()

            if not ada_response:
                with console.status("[dim magenta]Reintentando...[/dim magenta]"):
                    fallback_response = client.chat.completions.create(
                        model=model, messages=messages, temperature=temperature, max_tokens=max_tokens * 3,
                        extra_body={"options": {"think": False, "num_ctx": 2048}},
                    )
                ada_response = fallback_response.choices[0].message.content.strip()

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