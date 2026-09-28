import sqlite3
import os
import re

DB_PATH = os.path.join(os.path.dirname(__file__), "ada_memory.db")

def get_db_connection():
    """Retorna una conexión a SQLite con soporte de fila de diccionario/tupla."""
    return sqlite3.connect(DB_PATH)

def init_db():
    """Inicializa la base de datos de memoria persistente de ADA."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                key TEXT NOT NULL UNIQUE,
                value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migración segura: agrega updated_at si la tabla ya existía sin esa columna
        try:
            cursor.execute("ALTER TABLE memories ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        except sqlite3.OperationalError:
            pass  # La columna ya existe, no hay problema
        conn.commit()

def sanitize_prompt_text(text: str) -> str:
    """Sanitiza texto para evitar inyecciones de prompt."""
    if not text:
        return ""
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
                updated_at = CURRENT_TIMESTAMP
        """, (safe_category, safe_key, safe_value))
        conn.commit()

def delete_memory(key: str) -> bool:
    """
    Elimina una memoria por su clave exacta.
    Retorna True si se eliminó algo, False si no existía.
    """
    init_db()
    safe_key = sanitize_prompt_text(key)
    if not safe_key:
        return False

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM memories WHERE key = ?", (safe_key,))
        conn.commit()
        return cursor.rowcount > 0

def delete_memory_fuzzy(keyword: str) -> list[str]:
    """
    Elimina memorias cuya clave o valor contenga el keyword (búsqueda parcial).
    Retorna la lista de claves eliminadas.
    """
    init_db()
    safe_kw = sanitize_prompt_text(keyword).lower()
    if not safe_kw:
        return []

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT key FROM memories WHERE LOWER(key) LIKE ? OR LOWER(value) LIKE ?",
            (f"%{safe_kw}%", f"%{safe_kw}%")
        )
        rows = cursor.fetchall()
        if not rows:
            return []
        keys = [r[0] for r in rows]
        cursor.execute(
            f"DELETE FROM memories WHERE key IN ({','.join('?' * len(keys))})",
            keys
        )
        conn.commit()
        return keys

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

    formatted = "MEMORIA PERSISTENTE DE ADA (datos conocidos sobre Dante):\n"
    formatted += "- [IDENTIDAD] nombre: Dante\n"
    formatted += "- [IDENTIDAD] asistente: A.D.A (Eida)\n"

    for cat, key, val in memories:
        formatted += f"- [{cat.upper()}] {key}: {val}\n"
    return formatted

if __name__ == "__main__":
    init_db()
    print("Base de datos de memoria inicializada.")
