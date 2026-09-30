import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "ada_memory.db")

def get_db_connection():
    return sqlite3.connect(DB_PATH)

def init_db():
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
        conn.commit()

def reset_db():
    init_db()
    with get_db_connection() as conn:
        conn.cursor().execute("DELETE FROM memories")
        conn.commit()

def sanitize_text(text: str) -> str:
    if not text: return ""
    return str(text).replace('\r', ' ').replace('\n', ' ').strip()

def save_memories(memories_list: list):
    """Guarda una lista de diccionarios con category, key, value. Actualiza si existe (UPSERT)."""
    init_db()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        for mem in memories_list:
            cat = sanitize_text(mem.get("category", "general"))
            key = sanitize_text(mem.get("key", ""))
            val = sanitize_text(mem.get("value", ""))
            if key and val:
                cursor.execute("""
                    INSERT INTO memories (category, key, value)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        category = excluded.category,
                        updated_at = CURRENT_TIMESTAMP
                """, (cat, key, val))
        conn.commit()

def delete_memories(keys: list) -> list:
    """Elimina las claves exactas provistas y retorna las que sí se eliminaron."""
    init_db()
    deleted = []
    with get_db_connection() as conn:
        cursor = conn.cursor()
        for k in keys:
            safe_k = sanitize_text(k)
            cursor.execute("DELETE FROM memories WHERE key = ?", (safe_k,))
            if cursor.rowcount > 0:
                deleted.append(safe_k)
        conn.commit()
    return deleted

def get_all_memories():
    init_db()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT category, key, value FROM memories ORDER BY id ASC")
        return cursor.fetchall()

def format_memories_for_prompt() -> str:
    memories = get_all_memories()
    formatted = "\n=== RECUERDOS Y CONOCIMIENTOS SOBRE DANTE ===\n"
    formatted += "(USA ESTA INFORMACIÓN PARA CONVERSAR NATURALMENTE. ASIMÍLALA COMO SI FUERA TU PROPIA MEMORIA. PROHIBIDO MENCIONAR LAS PALABRAS 'CLAVE', 'BASE DE DATOS', O 'CATEGORÍA' AL HABLAR).\n\n"
    if not memories:
        formatted += "(No tienes recuerdos guardados actualmente)\n"
    else:
        for cat, key, val in memories:
            formatted += f"- [Tema: {cat}] {key}: {val}\n"
    formatted += "=============================================\n"
    return formatted

if __name__ == "__main__":
    init_db()
    print("Memoria inicializada en limpio.")
