# A.D.A. Architecture (Advanced Desktop Assistant)

A.D.A es un asistente conversacional de escritorio con transcripción local opcional, chat local o en la nube y memoria persistente en SQLite. Arranca por texto sin cargar componentes de audio. Con `--speak`, Edge TTS es el motor de síntesis inicial y utiliza un servicio online; esa modalidad no es completamente local.

## Diseño Core

La transcripción del micrófono y el almacenamiento de recuerdos ocurren en el equipo del usuario. El razonamiento depende del proveedor de chat elegido y la síntesis con Edge TTS envía el texto a un servicio externo, incluso cuando el chat usa Ollama local.

| Componente | Módulo | Tecnología / Patrón |
|---|---|---|
| **Cerebro (LLM)** | `ada.py` | Ollama local (`qwen2.5:3b`) o chat en la nube (OpenAI por defecto, `gpt-4o-mini`). Enrutamiento híbrido. |
| **Oídos (STT)** | `voice.py` | `faster-whisper`, cargado en el primer uso del micrófono y reutilizado, procesa matrices NumPy en memoria RAM. |
| **Voz (TTS)** | `voice.py` | Opcional mediante `--speak`. `edge-tts` con pre-fetching + respaldo `pyttsx3`; dependencias y reproducción inicializadas bajo demanda, sin cargar Whisper. |
| **Memoria** | `memory.py` | SQLite3 (`ada_memory.db`) con extracción JSON estructurada. |
| **Interfaz (UI)** | `ada.py` | `rich` (Terminal minimalista, anclado a la izquierda, fixed-width panel). |

## Flujo de datos y servicios externos

La elección del proveedor de chat y la salida de voz son independientes: `get_client()` selecciona el cliente, mientras `chat_loop()` crea un `VoiceEngine(mode="edge")` ligero, sin importar dependencias de audio ni abrir dispositivos. La salida hablada está desactivada salvo que se indique `--speak`. Si existe `OPENAI_API_KEY`, se ofrece elegir entre nube y local, con local como opción predeterminada. Los fallos de audio se informan y permiten continuar por texto.

| Ruta | Datos y destino |
|---|---|
| Transcripción (`voice.py`, `listen_manual_push`) | Whisper recibe el audio del micrófono como una matriz NumPy en memoria. No se envía la grabación a un servicio de transcripción. El texto resultante se procesa como una entrada escrita. |
| Conversación (`ada.py`, `get_system_prompt` y `chat_loop`) | El proveedor seleccionado recibe el mensaje actual, el historial reciente y el prompt del sistema. Este último incorpora los recuerdos cargados mediante `memory.format_memories_for_prompt()`. Con Ollama local, ese procesamiento ocurre en el equipo; con chat cloud, esos datos se envían al proveedor externo. |
| Guardado (`ada.py`, `execute_memory_command`) | El proveedor seleccionado recibe la orden y su contenido para extraer los datos. SQLite guarda el resultado localmente. La solicitud de extracción no incluye el conjunto de recuerdos existentes. |
| Borrado (`ada.py`, `execute_memory_command`) | Si hay recuerdos, el proveedor seleccionado recibe la orden y la lista de claves existentes para identificar cuáles borrar. Esta solicitud no incluye sus valores. SQLite ejecuta el borrado localmente; si no hay recuerdos, no se solicita selección al modelo. |
| Historial de comandos (`ada.py`, `chat_loop`) | La orden de memoria y su confirmación se añaden al historial reciente y pueden enviarse en una conversación cloud posterior. Tras el comando se recarga el contexto de recuerdos. |
| Síntesis opcional (`voice.py`, `_speak_edge`) | Con `--speak`, Edge TTS recibe fragmentos del texto de respuesta, después de la limpieza para lectura. Ese texto puede contener datos personales o recuerdos mencionados por ADA, aunque el proveedor del chat sea local. Sin esa opción no se solicita síntesis. |

La ubicación local de `ada_memory.db` no garantiza que los datos guardados permanezcan exclusivamente en el equipo. Eliminar un recuerdo de SQLite tampoco retira copias de información ya enviada a servicios externos. Estas rutas describen el procesamiento de conversaciones, no las descargas de instalación o de modelos.

## Ciclo de Vida del Comando de Voz (Push-To-Talk)

1. El usuario presiona `ENTER` en un prompt vacío. Solo entonces se importan las dependencias de entrada y se carga Whisper si todavía no está disponible. La primera carga puede requerir descargar el modelo.
2. Se abre el micrófono (`PyAudio`). Un callback recibe los fragmentos de audio, mientras el hilo principal espera el siguiente `ENTER` mediante `input()`.
3. Al presionar `ENTER` nuevamente, se detiene y cierra el flujo y se libera PyAudio. No queda un lector de teclado en segundo plano que pueda consumir el siguiente mensaje escrito.
4. Los bytes PCM se normalizan a Float32 con NumPy y se transcriben en memoria mediante el modelo reutilizable, sin archivos de grabación.
5. Si falla la carga, apertura o transcripción, se informa y se vuelve al chat escrito. Un fallo durante la captura requiere finalizar el intento con `ENTER`. La respuesta escrita se muestra antes de intentar la síntesis opcional.

La carga de Whisper se traslada al primer uso del micrófono. En Windows con Python 3.13.7, la mediana desde el lanzamiento del proceso hasta el primer prompt escrito pasó de **3,86 s a 1,94 s** frente al commit `353fa4b`: **1,92 s menos (49,8 %)**. Se descartó un calentamiento y se midieron cinco procesos nuevos por versión, alternando el orden, con dependencias y modelo en caché y descargas desactivadas. No mide el primer uso del micrófono, una caché fría ni el tiempo de respuesta; no garantiza el mismo resultado en otros equipos. Las tres evaluaciones locales de memoria con `qwen2.5:3b` también pasaron usando datos sintéticos; véanse los controles y las limitaciones en [README.md](README.md#measured-text-startup).

---

## Decisiones de Diseño y Caminos Descartados (ADRs)

Para evitar regresiones, se documentan rigurosamente los enfoques arquitectónicos que se probaron y fueron descartados:

### 1. Extracción de Memoria: Tool Calling vs. Híbrido
- ❌ **Descartado (Tool Calling Nativo):** Obligaba a hacer dos llamadas completas al LLM por turno. Modelos pequeños (como el 3B) alucinaban los formatos JSON o entraban en bucles. Esto duplicaba la latencia percibida.
- ✅ **Decisión (Actual):** Enrutamiento híbrido. Expresiones regulares interceptan intenciones duras al inicio de la frase (`"Anota..."`, `"Borra..."`). Si hay match, el LLM actúa puramente como un parser JSON veloz. La latencia general se reduce a la mitad.

### 2. Captura de Eventos de Teclado (Push-To-Talk)
- ❌ **Descartado (`msvcrt.kbhit`):** Librería nativa de C exclusiva de Windows. Rompía el programa instantáneamente con un `ImportError` en entornos Linux o macOS.
- ✅ **Decisión (Actual):** `input()` permanece en el hilo principal y PyAudio captura mediante callback. Evita lectores de teclado abandonados tras errores y conserva el control con `ENTER`, sin `msvcrt` ni el módulo `keyboard`.

### 3. I/O de Archivos de Audio para Whisper
- ❌ **Descartado (Archivos Temporales `.wav`):** Python 3.13 modificó el manejo de descriptores, y Windows bloquea agresivamente archivos abiertos (`NamedTemporaryFile`). Lanzaba `PermissionError` o `metadata_errors` cuando Whisper intentaba acceder a un archivo que Python aún tenía "tomado". Además, reducía la vida útil del disco (SSD) por I/O constante.
- ✅ **Decisión (Actual):** Procesamiento directo en RAM. Convertimos los frames crudos PCM a un `numpy.ndarray` in-memory. Whisper lo consume directamente. Cero bloqueos, máxima velocidad.

### 4. Interfaz Gráfica (CLI) y Adaptabilidad
- ❌ **Descartado (Anclaje a la izquierda y Ancho fijo):** Aunque fijar un límite duro (ej. `width=100`) y anclar el diseño a la izquierda previene que los bordes ASCII se rompan al redimensionar la ventana, arruinaba la estética fluida, simétrica y centrada que busca el proyecto.
- ✅ **Decisión (Actual):** Mantener el renderizado fluido adaptado al ancho total y el logo centrado dinámicamente (`Align.center`). Se acepta la limitación técnica inherente de las terminales estándar (TTY), en las cuales achicar agresivamente la ventana *después* del renderizado empuja los caracteres físicos y descuadra los bordes. Se establece que para solucionar esto definitivamente en el futuro sin sacrificar estética, se debe migrar de un script CLI a un framework completo de TUI (como `Textual`).
