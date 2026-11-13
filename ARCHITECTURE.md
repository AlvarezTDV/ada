# A.D.A. Architecture (Advanced Desktop Assistant)

A.D.A es un asistente conversacional de escritorio con transcripción local opcional, chat local o en la nube y memoria persistente en SQLite. Arranca por texto sin cargar componentes de audio. Con `--speak`, Edge TTS es el motor de síntesis inicial y utiliza un servicio online; esa modalidad no es completamente local.

## Hexagonal core and microkernel

`ada.py` is the composition root and terminal facade, not the session policy
owner. `create_session()` registers adapters with `register_default_adapters()`
and constructs `ada_core.orchestrator.SessionOrchestrator`. `chat_loop()` owns
terminal input/rendering and delegates text, microphone, and optional speech
output to that orchestrator.

```text
Terminal / ada.py
    -> SessionOrchestrator (ada_core)
        -> ExtensionRegistry.execute(capability, operation)
            -> ReasoningPort / MemoryPort / STT / TTS / WebNavigationPort
                -> ada_adapters -> existing provider, SQLite, voice, browser modules
```

`ada_core` imports no SDK, SQLite, voice engine, or browser implementation.
Immutable request/response values cross its ports; adapter factories perform no
I/O at registration. The registry is a microkernel: admission, compatibility,
enable/disable, lazy resolution, failure states, and bounded execution are
independent of each capability implementation. This is a cooperative extension
boundary, **not a sandbox or an in-process thread terminator**.

| Capability | Adapter / implementation | Policy |
|---|---|---|
| Reasoning | `reasoning_adapters.py` / existing `ada.py` provider helpers | Groq quota chain, or explicitly selected local Ollama; no automatic cross-provider fallback. |
| Memory | `memory_sqlite.py` / `memory.py` | Relevant bounded recall; atomic save/delete batches and actual committed results. |
| STT / TTS | `voice_adapters.py` / `voice.py` | Separate lazy engines and failure states; text remains usable if either is absent or fails. |
| Navigation | `browser_navigation_adapter.py` / isolated browser worker | Explicit URL/task only; output never enters memory, chat history, or TTS. |

### Memory and facade compatibility

`MemoryPort` retains its existing recall/single-item contract.
`MemoryCommandPort` is an additional structural protocol for complete ordered
listing and atomic batches; it does not overload filtered recall as a full key
catalogue. The SQLite adapter reuses `memory.save_memories()` and
`memory.delete_memories()` so batch validation, rollback, upsert order, and
actual-deletion acknowledgements remain unchanged. A replacement supporting only
`MemoryPort` can still provide chat context; memory commands report the missing
contract instead of writing directly to SQLite.

The public `get_client`, `get_system_prompt`, `execute_memory_command`,
`run_browser_task`, `VoiceEngine`, and `chat_loop` facades remain available.
Command extraction and persistence are injected through registry-backed
callbacks; pure formatting/validation reuse the existing helpers. Browser and
voice runner references are resolved from the facade at use time, preserving
test injection. Direct CLI execution binds the same facade module used by the
reasoning adapters.

### Diagnostics and fault isolation

`/estado` reports every registered extension's ID, installed/enabled/compatible/
available status, registry state, active selection, configured model, and reason.
The default composition includes both reasoning manifests and disables the
unselected one. Local probes do not call providers, initialize audio hardware,
launch a browser, or access SQLite. `READY` is local readiness only, not a remote
health or credential check; the last confirmed model comes from a successful
completion. Incompatible/disabled/failed entries are not revived by diagnostics.

`/navegar` is intercepted before memory commands and normal chat. The existing
pure URL guard runs before optional worker preparation, and execution crosses
`registry.execute(CapabilityKind.NAVIGATION, ...)`. Failures leave history and
memory unchanged. STT and TTS errors are independent of reasoning and memory.

For chat, selected memory is read through the registry; if unavailable, a visible
notice accompanies the memory-free response. A provider failure instead returns
an honest limited-mode error and does not commit the attempted turn. Extraction
failure never invokes a memory mutation. Empty/truncated chat responses get one
retry; writes/deletes never do. A new user turn explicitly retries reasoning,
but `enable()` refuses recovery while a timed-out invocation is still running.
Registry deadlines bound waiting, not SDK thread termination.
Interactive STT must additionally release its terminal reader before the next
prompt: after a registry timeout, the orchestrator waits for that same capture
to finish and can accept its completed transcription without recording again.
The timed-out extension remains marked failed until an explicit subsequent
microphone request can safely re-enable it. Interrupting exits the session
rather than starting a second competing stdin reader.

Tests injecting `ada.run_browser_task` also double the browser adapter's
readiness probe (or supply a registry with their own navigation extension).
Replacing only execution must not bypass production dependency checks.

`tests/test_architectural_resilience.py` exercises missing optional extensions,
navigation and voice failures, reasoning outages, safe memory-command aborts,
diagnostics, Groq model fallback, explicit local selection, and SQLite atomicity
using doubles and temporary databases. It makes no live provider requests.

## Flujo de datos y servicios externos

Chat and voice-output selection are independent. `get_client()` defaults to Groq
using `GROQ_API_KEY` and its OpenAI-compatible endpoint; it does not choose local
chat based on `OPENAI_API_KEY`. `chat_loop()` constructs the session registry;
STT and TTS adapters create their independent `VoiceEngine(mode="edge")` instances
only when the corresponding feature is requested.
Speech output remains disabled unless `--speak` is set; audio failures leave text
chat available.

### Provider configuration

Install core dependencies with `pip install -r requirements.txt`, set
`GROQ_API_KEY` in the launching environment, and run `python ada.py`.
`DEFAULT_GROQ_MODELS` defines `openai/gpt-oss-20b` → `openai/gpt-oss-120b`;
comma-separated `ADA_GROQ_MODELS` overrides the ordered chain.
`create_chat_completion_with_fallback()` rotates only on HTTP 429. It rebuilds
request parameters with `_build_completion_kwargs()` for each candidate: Groq
reasoning models receive low/medium reasoning effort and suppress reasoning
output, rather than receiving Ollama's `options` payload. Other failures do not
trigger a provider switch. The interactive SDK timeout is 25 seconds with zero
SDK retries; evaluation deadlines below are separate.

For local chat, run `ollama run qwen2.5:3b` and `python ada.py --provider local`.
`ADA_LOCAL_MODEL` changes that chat model without affecting the browser model.
No Groq key is required in local mode.

| Ruta | Datos y destino |
|---|---|
| Transcripción (`voice.py`, `listen_manual_push`) | Whisper recibe el audio del micrófono como una matriz NumPy en memoria. No se envía la grabación a un servicio de transcripción. El texto resultante se procesa como una entrada escrita. |
| Conversación (`ada.py`, `get_system_prompt` y `chat_loop`) | Groq by default, or Ollama with `--provider local`, receives the current message, recent history and system prompt. `memory.select_memories_for_prompt()` selects records locally; default cloud chat sends those selected values to Groq. Quota fallback resends the context to another Groq model. |
| Guardado (`ada.py`, `execute_memory_command`) | El proveedor seleccionado recibe la orden y su contenido para extraer los datos. SQLite guarda el resultado localmente. La solicitud de extracción no incluye el conjunto de recuerdos existentes. |
| Borrado (`ada.py`, `execute_memory_command`) | Si hay recuerdos, el proveedor seleccionado recibe la orden y la lista de claves existentes para identificar cuáles borrar. Esta solicitud no incluye sus valores. SQLite ejecuta el borrado localmente; si no hay recuerdos, no se solicita selección al modelo. |
| Historial de comandos (`ada.py`, `chat_loop`) | La orden de memoria y su confirmación se añaden al historial reciente y pueden enviarse en una conversación cloud posterior. Tras el comando se recargan las instrucciones base; la selección de recuerdos se calcula con el siguiente mensaje conversacional. |
| Síntesis opcional (`voice.py`, `_speak_edge`) | Con `--speak`, Edge TTS recibe fragmentos del texto de respuesta, después de la limpieza para lectura. Ese texto puede contener datos personales o recuerdos mencionados por ADA, aunque el proveedor del chat sea local. Sin esa opción no se solicita síntesis. |
| `/navegar` (`browser_navigation.py`, `browser_worker.py`) | Local Edge accesses the requested site. A separate local Ollama model receives only the explicit task and page data, not ADA memories or chat history. Groq credentials are not passed to this worker. |

La ubicación local de `ada_memory.db` no garantiza que los datos guardados permanezcan exclusivamente en el equipo. Eliminar un recuerdo de SQLite tampoco retira copias de información ya enviada a servicios externos. Estas rutas describen el procesamiento de conversaciones, no las descargas de instalación o de modelos.

### Selección acotada de recuerdos

Solo se mantienen en caché las instrucciones base. En cada turno se comparan palabras del mensaje actual con categoría, clave y valor, normalizando mayúsculas y tildes, separando guiones bajos y descartando palabras funcionales comunes. Se ordena por cantidad de coincidencias distintas; los empates usan `updated_at` e `id` descendentes.

El bloque completo incluye hasta cinco registros y 1.200 caracteres Unicode, contando encabezado y formato. Los registros que no caben se omiten completos y se prueban los siguientes; no se modifica SQLite. Sin coincidencias o espacio, se indica ausencia de recuerdos pertinentes para ese turno. El límite es aproximado respecto a tokens, sin tokenizadores ni dependencias nuevas; no garantiza 300 tokens ni limita toda la solicitud. No hay búsqueda semántica, catálogo de sinónimos ni controles nuevos de exclusión cloud. El historial puede conservar datos mencionados antes.

## Navegación opcional

### Local Browser Use worker

`/navegar <https-url> <task>` is intercepted before memory commands and chat.
`browser_navigation.py` uses only the standard library and starts
`browser_worker.py` with the interpreter from `.venv-browser`. Browser Use's
dependencies never need to be imported into ADA's core environment. The JSON
request contains only the URL and explicit task; external results are rendered
as plain data, not executed or added to memory, chat history or online speech.

The worker explicitly selects local Edge and a loopback Ollama model through
`ADA_BROWSER_MODEL` (default `qwen2.5:3b`), independently of Groq chat or
`ADA_LOCAL_MODEL`. Its `.venv-browser` environment remains separate. It does
not inherit provider credentials, load `.env`, enable Browser Use cloud sync or
use its default hosted model. A dedicated profile and temporary job directory
separate browser state from personal profiles and the repository. Browser Use
anonymous telemetry and automatic extensions/downloads are disabled.

The positive action set contains only `open_page`, `read_page`, and `done`.
Navigation is limited to the supplied public HTTPS host, with step/time bounds
and parent-owned process-tree cancellation. No generic shell, file, upload,
click, form-input or arbitrary-evaluation tool is exposed to the model. Trusted
adapter code reads page text through CDP. These controls are not a complete
network or operating-system sandbox; page scripts and subresources remain a
separate risk. Runtime checks must establish local model/browser compatibility,
not infer it from package installation or mocked tests.

## Synthetic provider evaluations

`tests/test_memory_prompt.py` and `tests/test_memory_retrieval.py` stay offline
unless `ADA_RUN_MODEL_EVALS=1`. `ADA_EVAL_PROVIDER` defaults to `groq`; `local`
selects Ollama. Both reuse the production client, completion options and quota
fallback. `IsolatedPromptFixture` supplies temporary SQLite databases and synthetic
records only; the real `ada_memory.db` is never read or transmitted.

The SDK uses `timeout=15, max_retries=0`. Each completion runs in a subprocess
with a 15-second wall-clock deadline, bounding the whole fallback chain as well
as stalled I/O. The enclosing authenticated suite is limited to 60 seconds and
uses fail-fast without automatic paid reruns; HTTP 401 prevents further requests.
The worker imports ADA/SDK types before the fixture's temporary module patch to
avoid duplicate SDK classes. An offline real-SDK/MockTransport regression covers
response parsing; mocked HTTP 429 covers fallback without spending real quota.

On 2026-11-09 the authenticated suite passed 101 tests in 35.128 s (37.469 s
including process startup), with six synthetic cases/seven completion calls using 20b as primary and
four optional browser checks skipped. Separate stateless streaming probes used
`time.perf_counter()` and a short Spanish prompt:

| Model | First visible content | Visible tokens/s after TTFT | Total |
|---|---:|---:|---:|
| `openai/gpt-oss-20b` | 1.278 s | 904.6 | 1.312 s |
| `openai/gpt-oss-120b` | 1.365 s | 669.9 | 1.407 s |

Throughput excludes the six reported reasoning tokens per response and divides
31/28 visible tokens by `(total - TTFT)`. TTFT excludes reasoning-only and blank
chunks. These are single short, potentially buffered samples, not sustained
generation-speed guarantees. See [the evaluation commands and measurement
details](README.md#measured-groq-evaluation-and-latency-2026-11-09).

## Ciclo de Vida del Comando de Voz (Push-To-Talk)

1. El usuario presiona `ENTER` en un prompt vacío. Solo entonces se importan las dependencias de entrada y se carga Whisper si todavía no está disponible. La primera carga puede requerir descargar el modelo.
2. Se abre el micrófono (`PyAudio`). Un callback recibe los fragmentos de audio, mientras el hilo principal espera el siguiente `ENTER` mediante `input()`.
3. Al presionar `ENTER` nuevamente, se detiene y cierra el flujo y se libera PyAudio. No queda un lector de teclado en segundo plano que pueda consumir el siguiente mensaje escrito.
4. Los bytes PCM se normalizan a Float32 con NumPy y se transcriben en memoria mediante el modelo reutilizable, sin archivos de grabación.
5. Si falla la carga, apertura o transcripción, se informa y se vuelve al chat escrito. Un fallo durante la captura requiere finalizar el intento con `ENTER`. La respuesta escrita se muestra antes de intentar la síntesis opcional.

La carga de Whisper se traslada al primer uso del micrófono. En Windows con Python 3.13.7, la mediana desde el lanzamiento del proceso hasta el primer prompt escrito pasó de **3,86 s a 1,94 s** frente al commit `353fa4b`: **1,92 s menos (49,8 %)**. Se descartó un calentamiento y se midieron cinco procesos nuevos por versión, alternando el orden, con dependencias y modelo en caché y descargas desactivadas. No mide el primer uso del micrófono, una caché fría ni el tiempo de respuesta; no garantiza el mismo resultado en otros equipos. Las tres evaluaciones locales de memoria con `qwen2.5:3b` también pasaron usando datos sintéticos; véanse los controles y las limitaciones en [README.md](README.md#measured-text-startup).

---

## Recuperación de la salida de voz

`speak()` devuelve un `SpeechResult` con éxito o fallo, motor, cantidad de fragmentos cuya reproducción terminó y, si corresponde, etapa y error. La generación de Edge usa un `AudioResult`: terminar sin excepción no basta si el archivo falta o está vacío.

La reproducción mantiene la precarga del siguiente fragmento. Si falla la generación, se termina de reproducir el fragmento actual antes de usar `pyttsx3` para el texto pendiente. Si falla la reproducción, el motor local reinicia únicamente el fragmento interrumpido y los siguientes; los fragmentos completados no se repiten. No hay recuperación exacta por palabra.

Antes del respaldo se resuelve la precarga pendiente y se intenta liberar la reproducción y eliminar los archivos temporales. Un error de limpieza se informa sin convertir una reproducción completada en un motivo para repetirla. Las cancelaciones no activan el respaldo. Si ambos motores fallan, se avisa y se conserva la respuesta escrita; el chat puede continuar. No se implementa una máquina de estados ni se alteran los controles de `--speak` o del micrófono.

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
