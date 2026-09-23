import os
import sys
import json
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

import memory  # Módulo de Memoria Persistente SQLite
from voice import VoiceEngine  # Módulo de Voz I/O

console = Console()

DEFAULT_LOCAL_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen2.5:3b"

def load_system_prompt():
    """Carga el prompt del sistema e inyecta la memoria persistente."""
    try:
        with open("system_prompt.txt", "r", encoding="utf-8") as f:
            base_prompt = f.read().strip()
    except FileNotFoundError:
        base_prompt = "Eres A.D.A (Eida), la perspicaz asistente del joven Dante."
    
    mem_context = memory.format_memories_for_prompt()
    full_prompt = f"{base_prompt}\n\n{mem_context}"
    return full_prompt

def get_client():
    """Configuración rápida de conexión con fallback a Local."""
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        console.print("[cyan]Se detectó OPENAI_API_KEY.[/cyan]")
        use_cloud = Prompt.ask("¿Usar modelo Nube (OpenAI) o Local (Ollama)? (nube/local)", choices=["nube", "local"], default="local")
        if use_cloud == "nube":
            return OpenAI(api_key=api_key), "gpt-4o-mini"
    
    return OpenAI(base_url=DEFAULT_LOCAL_URL, api_key="not-needed"), DEFAULT_MODEL

def check_and_save_tool_call(user_input: str, client: OpenAI, model: str):
    """Filtro inteligente para guardar observaciones relevantes en la memoria SQLite."""
    if len(user_input.strip().split()) < 3 or user_input.lower() in ["hola", "buenos dias", "chau", "jaja", "xd"]:
        return

    extraction_prompt = (
        "Analiza el siguiente texto introducido por el usuario. "
        "Si contiene una preferencia personal, un proyecto, un dato relevante sobre sí mismo o una instrucción permanente, "
        "extrae la información en formato JSON con las llaves 'should_save' (boolean), 'category' (string), 'key' (string) y 'value' (string). "
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
        if "{" in content and "}" in content:
            json_str = content[content.find("{"):content.rfind("}")+1]
            data = json.loads(json_str)
            if data.get("should_save"):
                memory.save_memory(
                    category=data.get("category", "general"),
                    key=data.get("key", "dato"),
                    value=data.get("value", "")
                )
                console.print(f"[dim green]🧠 [Memoria ADA guardada]: {data.get('key')} -> {data.get('value')}[/dim green]")
    except Exception:
        pass

def chat_loop():
    """Bucle principal de conversación interactiva (Texto / Voz)."""
    memory.init_db()
    
    console.print(Panel.fit(
        "[bold magenta]A.D.A (Ada) — Sistema de Control & Voz[/bold magenta]\n"
        "[dim]Asistente Personal del joven Dante[/dim]",
        border_style="magenta"
    ))
    
    client, model = get_client()
    
    # Preguntar si se activa el modo voz
    voice_choice = Prompt.ask("¿Activar interacción por Voz (Micrófono y Altavoz)? (s/n)", choices=["s", "n"], default="n")
    voice_active = (voice_choice == "s")
    
    voice_engine = VoiceEngine(mode="edge") if voice_active else None
    
    console.print(f"[green]✔ ADA lista en local: [bold]{model}[/bold][/green]")
    if voice_active:
        console.print("[yellow]🎙️  Modo VOZ activado (Push-to-Talk activado).[/yellow]")
    console.print("[yellow]Escribe 'salir' para terminar.[/yellow]\n")
    
    messages = [
        {"role": "system", "content": load_system_prompt()}
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
                    # En modo voz, presionar ENTER inicia la escucha y presionar ENTER de nuevo corta inmediatamente
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
            
            check_and_save_tool_call(user_input, client, model)
            messages[0] = {"role": "system", "content": load_system_prompt()}
            messages.append({"role": "user", "content": user_input})
            
            if len(messages) > 11:
                messages = [messages[0]] + messages[-10:]
                
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
