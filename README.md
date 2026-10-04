# A.D.A. (Advanced Desktop Assistant)

A.D.A es un asistente conversacional personal diseñado para ejecutarse localmente en tu máquina. Prioriza la privacidad absoluta (cero envíos a la nube), respuestas ultrarrápidas y una memoria persistente a largo plazo.

## 🌟 Capacidades Actuales

- 🎙️ **Push-to-Talk 100% Local:** Entiende tu voz sin enviar audios a Google o Apple. Utiliza `faster-whisper` directo en la memoria RAM para una transcripción privada e instantánea.
- 🧠 **Memoria Inteligente a Largo Plazo:** A.D.A recuerda quién eres, tus preferencias y reglas. Todo se guarda en una base de datos local SQLite (`ada_memory.db`).
- 💬 **Terminal Minimalista:** Interfaz visual limpia y cuidada usando la librería `Rich`, con código de borrado ANSI para ocultar ruidos de consola.
- ⚡ **Latencia Reducida:** Enrutamiento híbrido para comandos y TTS (Texto a Voz) concurrente que pre-genera el audio mientras el bot sigue hablando.

## ⚙️ Requisitos Previos

- Python 3.10 o superior.
- [Ollama](https://ollama.com/) ejecutándose localmente con el modelo configurado.
- (Recomendado) Entorno Windows/Linux/macOS; el código es 100% multiplataforma.

## 🚀 Instalación

1. Clona este repositorio o entra a la carpeta del proyecto.
2. Instala las dependencias requeridas (incluyen soporte de IA, audio e interfaz):
   ```bash
   pip install -r requirements.txt
   ```
3. Asegúrate de tener Ollama corriendo tu modelo local (por defecto `qwen2.5:3b`):
   ```bash
   ollama run qwen2.5:3b
   ```

## 🎮 Uso del Asistente

Para encender a A.D.A, simplemente ejecuta el script principal:

```bash
python ada.py
```

### Controles de Interacción
- **Por Texto:** Escribe directamente al lado del prompt `🔴 > ` y presiona `ENTER`.
- **Por Voz:** Asegúrate de que no haya texto escrito. Presiona `ENTER` una vez para activar el micrófono. Escucharás un bip y verás "Grabando...". Habla con naturalidad y **vuelve a presionar ENTER** para detener la grabación. La transcripción se enviará sola.

### Gestión de la Memoria
Puedes darle comandos directos a A.D.A para que aprenda o se olvide de cosas. Debes empezar tu frase con palabras clave:

- **Para Guardar datos:** Usa palabras como *"Anota"*, *"Guarda"*, *"Memoriza"*.
  - Ej: *"Anota que mi nombre es Dante."*
  - Ej: *"Por favor guarda que soy ingeniero de software."*
- **Para Borrar datos:** Usa palabras como *"Borra"*, *"Olvida"*, *"Elimina"*.
  - Ej: *"Borra que me llamo Vicente."*
  - Ej: *"Olvida mi edad."* (A.D.A usa inferencia semántica para entender a qué dato te refieres, incluso si usas sinónimos).

---
*Si deseas entender a profundidad cómo funciona el motor interno, por qué es tan rápido y qué problemas arquitectónicos superamos, revisa el archivo [ARCHITECTURE.md](ARCHITECTURE.md).*