<p align="center">
  <img src="assets/brand/ada-banner.gif" alt="A.D.A Animated Banner" width="100%">
</p>

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
- Entorno Windows/Linux/macOS.

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

### Error 403 al generar audio

Si Edge TTS devuelve `403, message='Invalid response status'`, actualiza la dependencia en el mismo entorno de Python que ejecuta ADA y reinicia el asistente:

```bash
python -m pip install --upgrade "edge-tts>=7.3.1"
```

La versión mínima requerida incluye el protocolo Sec-MS-GEC v2 y una corrección del procesamiento de la fecha del servidor. La actualización corrige esa incompatibilidad; no garantiza resolver todos los errores 403. Consulta las [notas de las versiones 7.3.0 y 7.3.1](https://github.com/rany2/edge-tts/releases).

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
  - Ej: *"Anota que prefiero el té verde."*
  - Ej: *"Por favor guarda que soy ingeniero de software."*
- **Para Borrar datos:** Usa palabras como *"Borra"*, *"Olvida"*, *"Elimina"*.
  - Ej: *"Borra mi edad."*
  - Ej: *"Olvida mi edad."*

## Prompt tests

Run the offline regression tests without starting audio hardware or a language model:

```bash
python -B -m unittest discover -s tests -v
```

The tests use temporary SQLite databases, never your `ada_memory.db`. They check that the storage-disclosure rule has a single source in `system_prompt.txt`, memory-as-data/current-turn guidance, prompt composition, and cache/reload behavior. The memory context does not duplicate the disclosure rule. These checks do not prove that a language model follows the instructions.

For a bounded behavioral evaluation, start Ollama with the configured default model (`qwen2.5:3b`), then opt in from PowerShell:

```powershell
$env:ADA_RUN_MODEL_EVALS = "1"
python -B -m unittest discover -s tests -v
Remove-Item Env:ADA_RUN_MODEL_EVALS
```

This evaluation sends three synthetic examples only to `127.0.0.1:11434`: a current preference overriding a stored one, an instruction embedded in a memory, and a direct storage question. The preference case asks about the same favorite-drink fact stored in memory and checks a labeled current value, ending at punctuation or a line break; an explanation may mention the old preference afterward on the same or another line. A missing label is a response-format failure, not proof of a precedence failure. It uses no cloud credentials, personal memories, or audio. Each request has a 30-second timeout and no retries. A missing server/model or a mismatched response fails the evaluation; a successful run is evidence for these examples, not a general prompt-injection guarantee.

## Etiquetas de versión

Use annotated Semantic Versioning tags in `vMAJOR.MINOR.PATCH` format. Format each tag message as `Release vX.Y.Z: <concise change summary>`.

---
*Revisa el archivo [ARCHITECTURE.md](ARCHITECTURE.md) para más información sobre el proyecto.*
