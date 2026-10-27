# A.D.A. Architecture (Advanced Desktop Assistant)

A.D.A es un asistente conversacional de escritorio con transcripción local, chat local o en la nube y memoria persistente en SQLite. Edge TTS es la voz inicial y utiliza un servicio online; el sistema completo no es 100% local ni funciona íntegramente sin conexión.

## Diseño Core

La transcripción del micrófono y el almacenamiento de recuerdos ocurren en el equipo del usuario. El razonamiento depende del proveedor de chat elegido y la síntesis con Edge TTS envía el texto a un servicio externo, incluso cuando el chat usa Ollama local.

| Componente | Módulo | Tecnología / Patrón |
|---|---|---|
| **Cerebro (LLM)** | `ada.py` | Ollama local (`qwen2.5:3b`) o chat en la nube (OpenAI por defecto, `gpt-4o-mini`). Enrutamiento híbrido. |
| **Oídos (STT)** | `voice.py` | `faster-whisper` procesando matrices NumPy en memoria RAM (Zero I/O). |
| **Voz (TTS)** | `voice.py` | `edge-tts` con pre-fetching en hilos concurrentes + fallback a `pyttsx3`. |
| **Memoria** | `memory.py` | SQLite3 (`ada_memory.db`) con extracción JSON estructurada. |
| **Interfaz (UI)** | `ada.py` | `rich` (Terminal minimalista, anclado a la izquierda, fixed-width panel). |

## Flujo de datos y servicios externos

La elección del proveedor de chat y la salida de voz son independientes: `get_client()` selecciona el cliente, mientras `chat_loop()` crea `VoiceEngine(mode="edge")`. Si existe `OPENAI_API_KEY`, se ofrece elegir entre nube y local, con local como opción predeterminada. Esta documentación no añade avisos, indicadores ni confirmaciones a la interfaz.

| Ruta | Datos y destino |
|---|---|
| Transcripción (`voice.py`, `listen_manual_push`) | Whisper recibe el audio del micrófono como una matriz NumPy en memoria. No se envía la grabación a un servicio de transcripción. El texto resultante se procesa como una entrada escrita. |
| Conversación (`ada.py`, `get_system_prompt` y `chat_loop`) | El proveedor seleccionado recibe el mensaje actual, el historial reciente y el prompt del sistema. Este último incorpora los recuerdos cargados mediante `memory.format_memories_for_prompt()`. Con Ollama local, ese procesamiento ocurre en el equipo; con chat cloud, esos datos se envían al proveedor externo. |
| Guardado (`ada.py`, `execute_memory_command`) | El proveedor seleccionado recibe la orden y su contenido para extraer los datos. SQLite guarda el resultado localmente. La solicitud de extracción no incluye el conjunto de recuerdos existentes. |
| Borrado (`ada.py`, `execute_memory_command`) | Si hay recuerdos, el proveedor seleccionado recibe la orden y la lista de claves existentes para identificar cuáles borrar. Esta solicitud no incluye sus valores. SQLite ejecuta el borrado localmente; si no hay recuerdos, no se solicita selección al modelo. |
| Historial de comandos (`ada.py`, `chat_loop`) | La orden de memoria y su confirmación se añaden al historial reciente y pueden enviarse en una conversación cloud posterior. Tras el comando se recarga el contexto de recuerdos. |
| Síntesis (`voice.py`, `_speak_edge`) | Edge TTS recibe fragmentos del texto de respuesta, después de la limpieza para lectura. Ese texto puede contener datos personales o recuerdos mencionados por ADA. La respuesta hablada se envía aunque el proveedor del chat sea local. |

La ubicación local de `ada_memory.db` no garantiza que los datos guardados permanezcan exclusivamente en el equipo. Eliminar un recuerdo de SQLite tampoco retira copias de información ya enviada a servicios externos. Estas rutas describen el procesamiento de conversaciones, no las descargas de instalación o de modelos.

## Ciclo de Vida del Comando de Voz (Push-To-Talk)

1. El usuario presiona `ENTER` en un prompt vacío para abrir el hardware de micrófono (`PyAudio`).
2. Un hilo demonio en segundo plano (`threading.Thread`) ejecuta un `input()` que duerme esperando la detención.
3. Al presionar `ENTER` nuevamente, el hilo despierta, lanza una señal (Event) y cierra el flujo de audio limpiamente.
4. Los bytes crudos de audio PCM se normalizan a Float32 (-1.0 a 1.0) usando Numpy y se mantienen en la memoria RAM.
5. `faster-whisper` transcribe la matriz Numpy en milisegundos sin crear archivos físicos.

---

## Decisiones de Diseño y Caminos Descartados (ADRs)

Para evitar regresiones, se documentan rigurosamente los enfoques arquitectónicos que se probaron y fueron descartados:

### 1. Extracción de Memoria: Tool Calling vs. Híbrido
- ❌ **Descartado (Tool Calling Nativo):** Obligaba a hacer dos llamadas completas al LLM por turno. Modelos pequeños (como el 3B) alucinaban los formatos JSON o entraban en bucles. Esto duplicaba la latencia percibida.
- ✅ **Decisión (Actual):** Enrutamiento híbrido. Expresiones regulares interceptan intenciones duras al inicio de la frase (`"Anota..."`, `"Borra..."`). Si hay match, el LLM actúa puramente como un parser JSON veloz. La latencia general se reduce a la mitad.

### 2. Captura de Eventos de Teclado (Push-To-Talk)
- ❌ **Descartado (`msvcrt.kbhit`):** Librería nativa de C exclusiva de Windows. Rompía el programa instantáneamente con un `ImportError` en entornos Linux o macOS.
- ✅ **Decisión (Actual):** Hilo en segundo plano (`daemon=True`) esperando por el bloqueador `input()`. Es 100% nativo de la Standard Library de Python, multiplataforma, no bloquea el hilo de audio principal y no requiere permisos de Administrador/Root (a diferencia del módulo `keyboard`).

### 3. I/O de Archivos de Audio para Whisper
- ❌ **Descartado (Archivos Temporales `.wav`):** Python 3.13 modificó el manejo de descriptores, y Windows bloquea agresivamente archivos abiertos (`NamedTemporaryFile`). Lanzaba `PermissionError` o `metadata_errors` cuando Whisper intentaba acceder a un archivo que Python aún tenía "tomado". Además, reducía la vida útil del disco (SSD) por I/O constante.
- ✅ **Decisión (Actual):** Procesamiento directo en RAM. Convertimos los frames crudos PCM a un `numpy.ndarray` in-memory. Whisper lo consume directamente. Cero bloqueos, máxima velocidad.

### 4. Interfaz Gráfica (CLI) y Adaptabilidad
- ❌ **Descartado (Anclaje a la izquierda y Ancho fijo):** Aunque fijar un límite duro (ej. `width=100`) y anclar el diseño a la izquierda previene que los bordes ASCII se rompan al redimensionar la ventana, arruinaba la estética fluida, simétrica y centrada que busca el proyecto.
- ✅ **Decisión (Actual):** Mantener el renderizado fluido adaptado al ancho total y el logo centrado dinámicamente (`Align.center`). Se acepta la limitación técnica inherente de las terminales estándar (TTY), en las cuales achicar agresivamente la ventana *después* del renderizado empuja los caracteres físicos y descuadra los bordes. Se establece que para solucionar esto definitivamente en el futuro sin sacrificar estética, se debe migrar de un script CLI a un framework completo de TUI (como `Textual`).
