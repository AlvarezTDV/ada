import os
import sys
import json
import threading
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
                
            # 3. Respondo AL INSTANTE sin esperar a la memoria
            with console.status("[dim magenta]Ada pensando...[/dim magenta]"):
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.6,
                )
                
            ada_response = response.choices[0].message.content.strip()
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
