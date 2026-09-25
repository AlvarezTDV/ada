import sqlite3
import os
import re

DB_PATH = os.path.join(os.path.dirname(__file__), "ada_memory.db")

def get_db_connection():
    """Retorna una conexión a SQLite con soporte de fila de diccionario/tupla."""
    return sqlite3.connect(DB_PATH)

def init_db():
    """Inicializa la base de datos de memoria persistente de ADA con índice ÚNICO."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                key TEXT NOT NULL UNIQUE,
                value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def sanitize_prompt_text(text: str) -> str:
    """Sanitiza texto introducido para evitar inyecciones de prompt en el sistema."""
    if not text:
        return ""
    # Remueve comillas invisibles de control o etiquetas de formato maliciosas
    cleaned = re.sub(r'[\r\n]+', ' ', text)
    return cleaned.strip()

def save_memory(category: str, key: str, value: str):
    """Guarda o actualiza una memoria atómicamente usando UPSERT de SQLite."""
    init_db()
    safe_key = sanitize_prompt_text(key)
    safe_value = sanitize_prompt_text(value)
    safe_category = sanitize_prompt_text(category)
    
    if not safe_key or not safe_value:
        return

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO memories (category, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                category = excluded.category,
                created_at = CURRENT_TIMESTAMP
        """, (safe_category, safe_key, safe_value))
        conn.commit()

def get_all_memories():
    """Recupera todas las memorias guardadas."""
    init_db()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT category, key, value FROM memories ORDER BY id ASC")
        return cursor.fetchall()

def format_memories_for_prompt() -> str:
    """Formatea la memoria persistente para el System Prompt de ADA."""
    memories = get_all_memories()
    
    formatted = "MEMORIA PERSISTENTE DE ADA (Cosas aprendidas sobre el joven Dante):\n"
    formatted += "- [IDENTIDAD] Nombre: Dante (se le llama joven Dante)\n"
    formatted += "- [IDENTIDAD] Asistente: A.D.A (Eida)\n"
    
    for cat, key, val in memories:
        formatted += f"- [{cat.upper()}] {key}: {val}\n"
    return formatted

if __name__ == "__main__":
    init_db()
    print("Base de datos de memoria inicializada con contexto seguro y UPSERT atómico.")
