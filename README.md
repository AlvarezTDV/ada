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

## Etiquetas de versión

Use annotated Semantic Versioning tags in `vMAJOR.MINOR.PATCH` format. Format each tag message as `Release vX.Y.Z: <concise change summary>`.

---
*Revisa el archivo [ARCHITECTURE.md](ARCHITECTURE.md) para más información sobre el proyecto.*
