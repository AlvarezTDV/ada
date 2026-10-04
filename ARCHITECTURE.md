# A.D.A. Architecture (Advanced Desktop Assistant)

A.D.A es un asistente conversacional 100% local, centrado en la privacidad, la baja latencia y la memoria persistente.

## Diseño Core

El sistema opera bajo el principio de **Cero Dependencia de Nube**. El razonamiento y el procesamiento de audio ocurren en el hardware del usuario, protegiendo los datos biométricos (voz) y la información personal (memoria).

| Componente | Módulo | Tecnología / Patrón |
|---|---|---|
| **Cerebro (LLM)** | `ada.py` | Ollama (`qwen2.5:3b`) vía OpenAI API local. Enrutamiento híbrido. |
| **Oídos (STT)** | `voice.py` | `faster-whisper` procesando matrices NumPy en memoria RAM (Zero I/O). |
| **Voz (TTS)** | `voice.py` | `edge-tts` con pre-fetching en hilos concurrentes + fallback a `pyttsx3`. |
| **Memoria** | `memory.py` | SQLite3 (`ada_memory.db`) con extracción JSON estructurada. |
| **Interfaz (UI)** | `ada.py` | `rich` (Terminal minimalista, anclado a la izquierda, fixed-width panel). |

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