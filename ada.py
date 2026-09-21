import os
import sys
import json
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

import memory  # Importamos nuestro nuevo módulo de memoria

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
    
    # Inyectamos los recuerdos actuales de la base de datos de memoria
    mem_context = memory.format_memories_for_prompt()
    full_prompt = f"{base_prompt}\n\n{mem_context}"
    return full_prompt

def get_client():
    """Configuración rápida de conexión con fallback automático a Local."""
    api_key = os.getenv("OPENAI_API_KEY")
    
    # Si hay API Key de OpenAI, preguntar rápido si prefiere local o nube
    if api_key:
        console.print("[cyan]Se detectó OPENAI_API_KEY.[/cyan]")
        use_cloud = Prompt.ask("¿Usar modelo Nube (OpenAI) o Local (Ollama)? (nube/local)", choices=["nube", "local"], default="local")
        if use_cloud == "nube":
            return OpenAI(api_key=api_key), "gpt-4o-mini"
    
    # Por defecto, conecta directamente al servidor local de Ollama sin rodeos
    return OpenAI(base_url=DEFAULT_LOCAL_URL, api_key="not-needed"), DEFAULT_MODEL

def check_and_save_tool_call(user_input: str, client: OpenAI, model: str):
    """
    Evalúa si la entrada del usuario contiene una preferencia o dato relevante 
    para guardarlo en la memoria persistente inteligente.
    """
    # Filtro rápido: si es un saludo corto o charla vacía, no gastar llamadas
    if len(user_input.strip().split()) < 3 or user_input.lower() in ["hola", "buenos dias", "chau", "jaja", "xd"]:
        return

    extraction_prompt = (
        "Analiza el siguiente texto introducido por el usuario. "
        "Si contiene una preferencia personal, un proyecto, un dato relevante sobre sí mismo o una instrucción permanente, "
        "extrae la información en formato JSON con las llaves 'should_save' (boolean), 'category' (string), 'key' (string) y 'value' (string). "
        "Si es solo charla casual, saludos, chistes o conversación efímera, responde únicamente {\"should_save\": false}.\n\n"
        f"Texto: \"{user_input}\""
    )
    
    try:
        res = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": extraction_prompt}],
            temperature=0.1
        )
        content = res.choices[0].message.content.strip()
        # Intentar parsear JSON
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
        pass # Si falla el análisis de memoria, continuar la conversación normalmente

def chat_loop():
    """Bucle principal de conversación de ADA."""
    memory.init_db()
    
    console.print(Panel.fit(
        "[bold magenta]A.D.A (Ada) — Sistema de Control[/bold magenta]\n"
        "[dim]Asistente Personal del joven Dante[/dim]",
        border_style="magenta"
    ))
    
    client, model = get_client()
    
    console.print(f"[green]✔ ADA lista en local: [bold]{model}[/bold][/green]")
    console.print("[yellow]Escribe 'salir' para terminar.[/yellow]\n")
    
    messages = [
        {"role": "system", "content": load_system_prompt()}
    ]
    
    while True:
        try:
            user_input = Prompt.ask("[bold cyan]Dante[/bold cyan]")
            if user_input.strip().lower() in ["salir", "exit", "quit"]:
                console.print("\n[magenta]Ada: Hasta luego, joven Dante.[/magenta]")
                break
                
            if not user_input.strip():
                continue
            
            # 1. Analizar si el mensaje contiene algo valioso para la memoria a largo plazo
            check_and_save_tool_call(user_input, client, model)
            
            # Recargar system prompt por si se añadió una nueva memoria
            messages[0] = {"role": "system", "content": load_system_prompt()}
            
            messages.append({"role": "user", "content": user_input})
            
            # Ventana de contexto deslizante (últimos 10 mensajes)
            if len(messages) > 11:
                messages = [messages[0]] + messages[-10:]
                
            with console.status("[dim magenta]Ada...[/dim magenta]"):
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.6,
                )
                
            ada_response = response.choices[0].message.content.strip()
            console.print(f"[bold magenta]Ada:[/bold magenta] {ada_response}\n")
            messages.append({"role": "assistant", "content": ada_response})
            
        except KeyboardInterrupt:
            console.print("\n\n[magenta]Ada: Nos vemos, joven Dante.[/magenta]")
            break
        except Exception as e:
            console.print(f"\n[red]❌ Error: {e}[/red]\n")

if __name__ == "__main__":
    chat_loop()
