import os
import re
import sys
import json
import threading
import unicodedata
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

import memory  # Módulo de Memoria Persistente SQLite
from voice import VoiceEngine  # Módulo de Voz I/O

console = Console()

DEFAULT_LOCAL_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen2.5:3b"

# Cache en memoria para evitar I/O constante de disco y SQLite por turno
_CACHED_SYSTEM_PROMPT = None

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

def get_client():
    """Configuración rápida de conexión con fallback a Local."""
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        console.print("[cyan]Se detectó OPENAI_API_KEY.[/cyan]")
        use_cloud = Prompt.ask("¿Usar modelo Nube (OpenAI) o Local (Ollama)? (nube/local)", choices=["nube", "local"], default="local")
        if use_cloud == "nube":
            return OpenAI(api_key=api_key), "gpt-4o-mini"
    
    return OpenAI(base_url=DEFAULT_LOCAL_URL, api_key="not-needed"), DEFAULT_MODEL

def _async_extract_and_save_memory(user_input: str, client: OpenAI, model: str):
    """
    Tarea secundaria en segundo plano (Worker Thread):
    Analiza y guarda la memoria de forma asíncrona sin bloquear la respuesta de ADA al usuario.
    """
    if len(user_input.strip().split()) < 3 or user_input.lower() in ["hola", "buenos dias", "chau", "jaja", "xd"]:
        return

    extraction_prompt = (
        "Analiza el siguiente texto introducido por el usuario. "
        "Si contiene una preferencia personal, un proyecto, un dato relevante sobre sí mismo o una instrucción permanente, "
        "extrae la información en formato JSON estrictamente válido con las llaves 'should_save' (boolean), 'category' (string), 'key' (string) y 'value' (string). "
        "Si es solo charla casual, saludos o conversación efímera, responde únicamente {\"should_save\": false}.\n\n"
        f"Texto: \"{user_input}\""
    )
    
    try:
        res = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": extraction_prompt}],
            temperature=0.1
        )
        content = res.choices[0].message.content.strip()
        
        # Extracción segura de JSON
        if "{" in content and "}" in content:
            json_str = content[content.find("{"):content.rfind("}")+1]
            data = json.loads(json_str)
            if isinstance(data, dict) and data.get("should_save"):
                memory.save_memory(
                    category=str(data.get("category", "general")),
                    key=str(data.get("key", "dato")),
                    value=str(data.get("value", ""))
                )
                # Invalidar caché de prompt para incluir la nueva memoria en los turnos futuros
                get_system_prompt(reload=True)
                console.print(f"\n[dim green]🧠 [Memoria ADA guardada]: {data.get('key')} -> {data.get('value')}[/dim green]")
    except Exception as e:
        # Registro silencioso o de depuración en lugar de ignorar fallos a ciegas
        pass

def schedule_memory_check(user_input: str, client: OpenAI, model: str):
    """Lanza el análisis de memoria en un hilo secundario sin esperar su finalización (Latencia 0ms)."""
    t = threading.Thread(
        target=_async_extract_and_save_memory,
        args=(user_input, client, model),
        daemon=True
    )
    t.start()

def classify_input_mode(text: str) -> dict:
    """
    Classify user input into one of three modes using only local heuristics:
      - casual     : greetings, reactions, simple questions  → think OFF, fast
      - task       : structured output requests              → think OFF, detailed
      - reasoning  : multi-condition decisions, trade-offs,
                     ethical/philosophical dilemmas,
                     optimization problems                   → think ON, deep

    Priority order: casual → reasoning → task
    Reasoning is evaluated before task to prevent task keywords
    from stealing complex inputs that genuinely need deep thought.
    """

    # ── CASUAL signals ────────────────────────────────────────────────────────
    CASUAL_SIGNALS = {
        "hola", "buenas", "buenos días", "buenas tardes", "buenas noches",
        "qué tal", "cómo estás", "cómo vas", "cómo te va",
        "hi", "hey", "good morning", "good night", "good afternoon",
        "jaja", "jeje", "xd", "lol", "ok", "okay", "sí", "no", "dale",
        "claro", "entendido", "gracias", "thanks", "perfecto", "genial",
        "bien", "mal", "uff", "wow", "ah", "oh", "ya", "vale", "np",
        "de nada", "no hay de qué", "con gusto",
    }

    # ── TASK keywords ─────────────────────────────────────────────────────────
    # Straightforward output generation: lists, summaries, translations,
    # schedules, code, etc. No multi-condition reasoning required.
    TASK_KEYWORDS = {
        # Structured output — Spanish
        "planifica", "planea", "organiza", "crea", "escribe", "redacta",
        "lista", "enumera", "describe", "genera", "diseña", "resume",
        "traduce", "calcula", "desarrolla", "implementa", "configura",
        "cuéntame", "cuentame", "busca", "muéstrame", "muestrame", "dame", "hazme",
        "elabora", "elaborar", "prepara", "preparar", "construye", "construir",
        "sugiere", "sugerir", "propón", "propon", "proponer", "corrige", "corregir",
        "planificación", "planificacion", "borrador", "ideas", "ejemplo", "ejemplos",
        "plantilla", "estructura", "esquema", "guía", "guia", "tutorial", "código", "codigo", "script",
        # Structured output — English
        "plan", "create", "write", "list", "describe", "generate", "design",
        "summarize", "translate", "calculate", "implement", "configure",
        "show me", "give me", "make me", "find", "suggest", "draft", "code", "template",
        # Time / structure signals — Spanish
        "semana", "semanas", "día", "dia", "días", "dias", "mes", "meses", "mañana", "manana",
        "pasos", "paso", "agenda", "horario", "rutina", "cronograma",
        "tareas", "tarea", "proyecto", "itinerario", "calendario",
        # Time / structure signals — English
        "week", "days", "month", "tomorrow", "steps", "schedule",
        "tasks", "project", "itinerary", "calendar",
    }

    # ── REASONING signals ─────────────────────────────────────────────────────
    # Keywords that alone suggest the need for deep analysis.
    REASONING_KEYWORDS = {
        # Decision / comparison — Spanish
        "debería", "deberia", "deberías", "deberias", "debería yo", "deberia yo", "vale más", "vale mas", "conviene",
        "es mejor", "cuál es mejor", "cual es mejor", "cuál elegiría", "cual elegiria", "qué harías", "que harias",
        "qué elegirías", "que elegirias", "me recomiendas", "me recomendarías", "me recomendarias",
        "ventajas", "desventajas", "pros", "contras", "diferencia entre",
        "versus", "vs", "comparar", "compara", "contrasta",
        "a favor", "en contra", "trade-off", "tradeoff",
        # Analytical depth — Spanish
        "analiza", "evalúa", "evalua", "razona", "argumenta", "justifica",
        "explica por qué", "explica por que", "por qué crees", "por que crees", "qué opinas", "que opinas", "qué piensas", "que piensas",
        "cuál es tu opinión", "cual es tu opinion", "qué tan viable", "que tan viable", "es viable", "es posible que",
        "tiene sentido", "vale la pena", "merece la pena",
        # Optimization / planning with constraints — Spanish
        "optimiza", "maximiza", "minimiza", "de la manera más eficiente", "de la manera mas eficiente",
        "considerando que", "teniendo en cuenta", "dado que", "si tuvieras que",
        "en caso de que", "suponiendo que", "bajo qué condiciones", "bajo que condiciones",
        "cuál sería la mejor forma", "cual seria la mejor forma", "cómo priorizarías", "como priorizarias",
        # Ethical / philosophical — Spanish
        "es correcto", "es ético", "es etico", "moralmente", "filosóficamente", "filosoficamente",
        "dilema", "conflicto de", "en tu opinión", "en tu opinion",
        # Decision / comparison — English
        "should i", "should we", "is it better", "which is better",
        "recommend", "pros and cons", "advantages", "disadvantages",
        "compare", "contrast", "versus", "trade-off", "worth it",
        # Analytical depth — English
        "analyze", "evaluate", "reason", "argue", "justify",
        "explain why", "what do you think", "your opinion", "feasible",
        "makes sense", "worth", "viable",
        # Optimization — English
        "optimize", "maximize", "minimize", "most efficient",
        "considering that", "given that", "if you had to",
        "best way to", "how would you prioritize",
        # Ethical — English
        "ethical", "morally", "dilemma", "philosophically",
    }

    # ── Structural complexity patterns ────────────────────────────────────────
    # These regex patterns detect multi-condition sentence structures that
    # signal reasoning regardless of keyword presence.
    REASONING_PATTERNS = [
        # Conditional chains: "si X entonces Y, pero considerando Z"
        r"\b(si|if)\b.{10,}.*(pero|aunque|sin embargo|however|but|yet)\b",
        # Multi-condition: "teniendo en cuenta A, B y C"
        r"(teniendo en cuenta|considerando|dado que|given that|considering).{5,}(y|,|e)\b",
        # Explicit comparison structure: "X vs Y", "entre A y B, cuál"
        r"\b(entre|between)\b.{3,}\b(y|and|o|or)\b.{3,}\b(cual|which|mejor|better)\b",
        # Weighing options: "por un lado... por otro"
        r"(por un lado|on one hand|on the other hand|por otro lado)",
        # Multiple nested conditions (3+ clauses with conjunctions)
        r"(.*(,|y|pero|aunque|sin embargo|however|but).{5,}){2,}",
        # Hypotheticals: "si tuvieras que elegir", "qué harías si"
        r"\b(si tuvieras|si pudieras|what would you|if you had to|if you could)\b",
        # Cause-effect reasoning: "por qué", "qué implicaciones"
        r"\b(que implicaciones|what implications|por que crees|why do you think)\b",
    ]

    lowered = text.lower().strip()
    # Normalize: strip accents so "debería"=="deberia", "cuéntame"=="cuentame", etc.
    normalized = unicodedata.normalize("NFD", lowered)
    normalized = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    words      = normalized.split()
    word_count = len(words)

    def _hit(keyword_set: set) -> bool:
        """Check if any keyword (normalized or original) matches whole words in the input."""
        for kw in keyword_set:
            # Check original keyword
            if re.search(r"\b" + re.escape(kw) + r"\b", lowered):
                return True
            
            # Normalize keyword to check against normalized input
            kw_norm = unicodedata.normalize("NFD", kw)
            kw_norm = "".join(c for c in kw_norm if unicodedata.category(c) != "Mn")
            
            if re.search(r"\b" + re.escape(kw_norm) + r"\b", normalized):
                return True
                
        return False

    # ── 1. CASUAL gate (always first, highest priority) ───────────────────────
    # Short inputs with casual signals are never reasoning or task,
    # even if they happen to contain ambiguous keywords.
    if word_count <= 5 and _hit(CASUAL_SIGNALS):
        return {"mode": "casual", "max_tokens": 256, "temperature": 0.4, "think": False}

    # Remove the generic <= 5 words casual guard. Let short inputs be evaluated
    # for REASONING and TASK. If they don't hit any keywords, they will naturally
    # fall through to the default casual return at the end.

    # ── 2. REASONING gate (evaluated before task) ─────────────────────────────
    # Trigger on keyword match OR structural complexity pattern.
    # A single reasoning keyword in a longer input is enough.
    reasoning_keyword_hit = _hit(REASONING_KEYWORDS)
    reasoning_pattern_hit = any(re.search(p, normalized) for p in REASONING_PATTERNS)

    # Extra complexity signal: very long input (25+ words) that contains
    # any comparative or conditional word — likely needs deep thought.
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

    # ── 3. TASK gate ──────────────────────────────────────────────────────────
    if _hit(TASK_KEYWORDS):
        return {"mode": "task", "max_tokens": 700, "temperature": 0.3, "think": False}

    # Long input with no specific signal → treat as task (needs structured output)
    if word_count > 12:
        return {"mode": "task", "max_tokens": 700, "temperature": 0.3, "think": False}

    # ── 4. Default → casual ───────────────────────────────────────────────────
    return {"mode": "casual", "max_tokens": 256, "temperature": 0.4, "think": False}


def chat_loop():
    """Bucle principal de conversación interactiva optimizado (Texto / Voz)."""
    memory.init_db()
    
    console.print(Panel.fit(
        "[bold magenta]A.D.A (Ada) — Sistema de Control & Voz[/bold magenta]\n"
        "[dim]Asistente Personal del joven Dante[/dim]",
        border_style="magenta"
    ))
    
    client, model = get_client()
    
    voice_choice = Prompt.ask("¿Activar interacción por Voz (Micrófono y Altavoz)? (s/n)", choices=["s", "n"], default="n")
    voice_active = (voice_choice == "s")
    
    voice_engine = VoiceEngine(mode="edge") if voice_active else None
    
    console.print(f"[green]✔ ADA lista en local: [bold]{model}[/bold][/green]")
    if voice_active:
        console.print("[yellow]🎙️  Modo VOZ activado (Push-to-Talk instantáneo activado).[/yellow]")
    console.print("[yellow]Escribe 'salir' para terminar.[/yellow]\n")
    
    messages = [
        {"role": "system", "content": get_system_prompt()}
    ]
    
    while True:
        try:
            user_input = ""
            
            if voice_active:
                console.print("[bold cyan]Presiona ENTER para HABLAR (o escribe tu mensaje directamente):[/bold cyan]")
                typed_input = input("Dante > ")
                if typed_input.strip():
                    user_input = typed_input.strip()
                else:
                    user_input = voice_engine.listen_manual_push()
                    if user_input:
                        console.print(f"[bold cyan]Dante (Voz):[/bold cyan] {user_input}")
            else:
                user_input = Prompt.ask("[bold cyan]Dante[/bold cyan]")
                
            if user_input.strip().lower() in ["salir", "exit", "quit"]:
                farewell = "Hasta luego, joven Dante."
                console.print(f"\n[magenta]Ada:[/magenta] {farewell}")
                if voice_active and voice_engine:
                    voice_engine.speak(farewell)
                break
                
            if not user_input.strip():
                continue
            
            # 1. Lanzo la extracción de memoria en SEGUNDO PLANO (0ms de espera para el usuario)
            schedule_memory_check(user_input, client, model)
            
            # 2. Actualizo el mensaje system con el caché actual
            messages[0] = {"role": "system", "content": get_system_prompt()}
            messages.append({"role": "user", "content": user_input})
            
            # Ventana de contexto deslizante (últimos 10 mensajes)
            if len(messages) > 11:
                messages = [messages[0]] + messages[-10:]

            # Detección de modo: casual / task / reasoning (lógica local, 0ms)
            mode_info   = classify_input_mode(user_input)
            mode        = mode_info["mode"]
            temperature = mode_info["temperature"]
            max_tokens  = mode_info["max_tokens"]
            think       = mode_info["think"]

            mode_color = {"casual": "dim", "task": "cyan", "reasoning": "yellow"}[mode]
            think_label = "🧠 ON" if think else "OFF"
            console.print(f"[{mode_color}]🎯 Modo: {mode} | think={think_label} | temp={temperature} | max_tokens={max_tokens}[/{mode_color}]")

            # 3. Respondo AL INSTANTE sin esperar a la memoria
            status_label = "[dim magenta]Ada razonando (puede tardar unos segundos)...[/dim magenta]" if think else "[dim magenta]Ada pensando...[/dim magenta]"
            with console.status(status_label):
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    extra_body={"options": {"think": think, "num_ctx": 2048}},
                )

            ada_response = response.choices[0].message.content.strip()

            # Guard: si el modelo devolvió vacío (thinking consumió el budget),
            # reintentamos una vez con triple presupuesto y thinking forzado a off.
            if not ada_response:
                console.print("[dim red]⚠ Respuesta vacía, reintentando...[/dim red]")
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
            console.print(f"[bold magenta]Ada:[/bold magenta] {ada_response}\n")
            messages.append({"role": "assistant", "content": ada_response})
            
            if voice_active and voice_engine:
                voice_engine.speak(ada_response)
            
        except KeyboardInterrupt:
            console.print("\n\n[magenta]Ada: Nos vemos, joven Dante.[/magenta]")
            break
        except Exception as e:
            console.print(f"\n[red]❌ Error: {e}[/red]\n")

if __name__ == "__main__":
    chat_loop()
