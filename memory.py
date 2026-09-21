import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "ada_memory.db")

def init_db():
    """Inicializa la base de datos de memoria persistente de ADA."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def save_memory(category: str, key: str, value: str):
    """Guarda una memoria valiosa para el joven Dante."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Actualiza si ya existe la clave o inserta una nueva
    cursor.execute("SELECT id FROM memories WHERE key = ?", (key,))
    row = cursor.fetchone()
    if row:
        cursor.execute("UPDATE memories SET value = ?, category = ? WHERE id = ?", (value, category, row[0]))
    else:
        cursor.execute("INSERT INTO memories (category, key, value) VALUES (?, ?, ?)", (category, key, value))
    conn.commit()
    conn.close()

def get_all_memories():
    """Recupera todas las memorias guardadas para inyectarlas en el contexto de ADA."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT category, key, value FROM memories")
    rows = cursor.fetchall()
    conn.close()
    return rows

def format_memories_for_prompt():
    """Formatea la memoria persistente para el System Prompt de ADA."""
    memories = get_all_memories()
    if not memories:
        return "No hay recuerdos almacenados aún."
    
    formatted = "MEMORIA PERSISTENTE DE ADA (Cosas aprendidas sobre el joven Dante):\n"
    for cat, key, val in memories:
        formatted += f"- [{cat.upper()}] {key}: {val}\n"
    return formatted

if __name__ == "__main__":
    init_db()
    print("Base de datos de memoria inicializada correctamente.")
