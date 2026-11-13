# WU-08 — Session orchestrator cutover
Branch: `feat/orchestrator-cutover`; baseline: `70b71fb`; delivery: one logical Entrega-E, two explicitly requested commits, no publishing.
Runner: `python -B scripts/run_tests.py --timeout 45`; implementation inline after bounded mapping; forecast approximately 900 authored changed lines (coherent cutover, not a line cap).

## Specs
S1. “Crear `SessionOrchestrator` en `ada_core/orchestrator.py` y conectar `ada.py` para que toda la sesión funcione a través del `ExtensionRegistry` y los adaptadores de `ada_adapters/`”. Preserve the existing facade and user commands.
S2. “Comando `/estado`: devuelve tabla/resumen con cada extensión registrada (`reasoning.groq`, `reasoning.ollama_local`, `memory.sqlite`, `stt.faster_whisper`, `tts.edge_with_local_fallback`, `navigation.browser_use_edge`), su estado (`READY`, `DISABLED`, `UNAVAILABLE_DEPENDENCY`, `DEGRADED`, `FAILED`) y el modelo activo.”
S3. “Si la extensión está deshabilitada, no instalada o falla, informa la limitación exacta sin tocar el historial ni afirmar éxito, y la conversación normal sigue funcionando.”
S4. “Si el proveedor de razonamiento está caído o sin cuota en todos los modelos de la cadena, informa que no puede extraer la orden de memoria porque el proveedor de razonamiento no está disponible y conserva `ada_memory.db` intacta.”
S5. “Si `ReasoningPort` falla (sin red, clave inválida o cuota agotada en toda la cadena de modelos), explica qué falló sin fingir razonar ni corromper `messages`, manteniendo la sesión viva para comandos locales (`/estado`, `salir`) o para reintentar cuando vuelva la red/cuota.”
S6. “Todos los comandos de usuario (`/navegar`, `Anota...`, `Borra...`, `salir`, voz con `ENTER` vacío, `--speak` y `--provider local`) funcionan igual de cara al usuario, además del nuevo comando `/estado`.”
S7. “Actualizar `ARCHITECTURE.md` y `README.md` describiendo la arquitectura hexagonal + microkernel (`ada_core` + `ada_adapters`) y el comando `/estado`.”
S8. “Suite completa pasando con `scripts/run_tests.py --timeout 45` y documentación actualizada.” Add the seven named resilience tests, use doubles and temporary SQLite, run `git diff --check`.
S9. Additional authorized scope: “Autorizar ampliación” for `ada_core/ports.py` and `ada_adapters/memory_sqlite.py`, to preserve complete listing, batch transactions, and actual mutation acknowledgements.

## Tasks
- T1 — S1–S6,S8–S9 — inline, mapped by explorer because evidence exceeded inline budget — complete: RED observed, full 146-test GREEN, independent blocker recheck passed, native review acknowledged — commit: `3f029e7`, `feat(core): wire SessionOrchestrator, degraded mode, and /estado diagnostics into ADA`.
- T2 — S7–S8 — inline, same dependent behavior, no parallel writer — complete: README/architecture and help updated, structural readback passed — commit: documentation commit containing this final task update, `docs(architecture): document hexagonal microkernel extension system and /estado command`.
- T3 — S8 — inline, bounded CI-fixture repair after authorized publishing — complete: RED reproduced with all optional audio packages unavailable, focused 10/10 and full 147-case suite GREEN — commit: CI repair containing this log entry.

## Log
L1. Original authorized mission (verbatim):

# Misión de Ejecución Acotada — Unidad WU-08

> ADVERTENCIA CRÍTICA SOBRE IDENTIFICADORES DE PR/ENTREGA:
> `Entrega-E` es un identificador lógico de este plan y NO tiene relación con los PRs históricos de GitHub.

1. **ID estable**: `WU-08`
2. **Objetivo único y motivo**: Crear `SessionOrchestrator` en `ada_core/orchestrator.py` y conectar `ada.py` para que toda la sesión funcione a
través del `ExtensionRegistry` y los adaptadores de `ada_adapters/`, añadiendo el comando `/estado` (diagnóstico en vivo de qué extensiones están
instaladas, habilitadas, compatibles y disponibles), garantizando el modo degradado ante fallos de cualquier pieza y actualizando `README.md` y
`ARCHITECTURE.md`.
3. **Requisitos que cubre**: Secciones 2, 3, 4 y 7 completas (funcionar como rompecabezas de piezas reemplazables, seguir conversando si falla una
herramienta, entrar en modo limitado honesto si falla el proveedor de razonamiento, no inventar éxito cuando una herramienta falla, comando `/estado`,
y adaptación/limpieza de tests antiguos incompatibles).
4. **Dependencias y estado esperado al comenzar**:
   - Crear o estar en la rama `feat/orchestrator-cutover` a partir del estado actualizado con `WU-07`.
5. **Rama y Entrega a la que pertenece**: Rama `feat/orchestrator-cutover` (`Entrega-E`; cierra el Plan Principal).
6. **Archivos existentes que deben inspeccionarse o cambiarse**:
   - Modificar: `ada.py`, `ada_core/__init__.py`, `README.md`, `ARCHITECTURE.md`.
   - Si algún test antiguo en `tests/` choca con la nueva estructura interna de `ada.py`, adaptarlo a la nueva arquitectura o eliminar el test
   redundante si ya está cubierto por `tests/test_architectural_resilience.py`.
7. **Archivos nuevos propuestos y responsabilidad de cada uno**:
   - `ada_core/orchestrator.py`: `SessionOrchestrator` que coordina los turnos usando únicamente `ExtensionRegistry`:
     - Comando `/estado`: devuelve tabla/resumen con cada extensión registrada (`reasoning.groq`, `reasoning.ollama_local`, `memory.sqlite`,
     `stt.faster_whisper`, `tts.edge_with_local_fallback`, `navigation.browser_use_edge`), su estado (`READY`, `DISABLED`, `UNAVAILABLE_DEPENDENCY`,
     `DEGRADED`, `FAILED`) y el modelo activo.
     - Comando `/navegar <url> <tarea>`: invoca `WebNavigationPort` vía `registry.execute(CapabilityKind.NAVIGATION, ...)`. Si la extensión está
     deshabilitada, no instalada o falla, informa la limitación exacta sin tocar el historial ni afirmar éxito, y la conversación normal sigue
     funcionando.
     - Comandos `Anota...` / `Borra...`: requieren `ReasoningPort` y `MemoryPort`. Si el proveedor de razonamiento está caído o sin cuota en todos los
     modelos de la cadena, informa que no puede extraer la orden de memoria porque el proveedor de razonamiento no está disponible y conserva
     `ada_memory.db` intacta.
     - Conversación normal: usa `ReasoningPort` y `MemoryPort`. Si `ReasoningPort` falla (sin red, clave inválida o cuota agotada en toda la cadena de
     modelos), explica qué falló sin fingir razonar ni corromper `messages`, manteniendo la sesión viva para comandos locales (`/estado`, `salir`) o
     para reintentar cuando vuelva la red/cuota.
   - `tests/test_architectural_resilience.py`: Suite de integración para todos los escenarios de tolerancia a fallos y modo degradado.
8. **Superficie de edición permitida**:
   - `ada_core/orchestrator.py`, `ada_core/__init__.py`, `ada.py`, `tests/test_architectural_resilience.py`, `README.md`, `ARCHITECTURE.md`, y
   adaptación puntual de `tests/test_*.py` si fuera necesario.
9. **Símbolos, contratos o adaptadores que se deben reutilizar**:
   - `ExtensionRegistry`, `register_default_adapters`, y los símbolos de fachada en `ada.py` (`get_client`, `get_system_prompt`,
   `execute_memory_command`, `run_browser_task`, `VoiceEngine`, `chat_loop`).
10. **Secuencia concreta de implementación (RED → GREEN → REFACTOR)**:
    - **Paso 10.1 (RED)**: Crear `tests/test_architectural_resilience.py` con los 7 escenarios del punto 12 y verificar que falla.
    - **Paso 10.2 (GREEN)**: Implementar `SessionOrchestrator` en `ada_core/orchestrator.py` y conectar `ada.py` para que construya el registro y
    delegue en el orquestador.
    - **Paso 10.3 (Adaptación de tests antiguos y REFACTOR)**: Ejecutar `python -B scripts/run_tests.py --timeout 45`. Adaptar cualquier test antiguo
    que dependiera de detalles internos reemplazados, eliminando duplicados obsoletos.
    - **Paso 10.4 (Documentación)**: Actualizar `ARCHITECTURE.md` y `README.md` describiendo la arquitectura hexagonal + microkernel (`ada_core` +
    `ada_adapters`) y el comando `/estado`.
11. **Entradas, salidas, errores e invariantes relevantes**:
    - Invariante 1: Si falla `/navegar`, el micrófono o el TTS, el chat con Groq continúa funcionando sin interrupción.
    - Invariante 2: Si falla toda la cadena de modelos de Groq (`ReasoningPort`), Ada nunca inventa respuestas ni afirma haber completado un
    guardado/borrado de memoria.
12. **Pruebas que deben añadirse en `tests/test_architectural_resilience.py`**:
    1. `test_ada_starts_and_chats_when_optional_browser_and_voice_extensions_are_uninstalled`.
    2. `test_navigation_extension_failure_or_removal_is_isolated_and_chat_continues`.
    3. `test_reasoning_provider_outage_enters_honest_limited_mode_without_corrupting_db_or_history`.
    4. `test_memory_command_aborts_safely_when_reasoning_provider_is_unavailable`.
    5. `test_status_command_lists_installed_enabled_compatible_and_available_extensions`.
    6. `test_groq_primary_exhaustion_transparently_uses_backup_model_inside_orchestrator`.
    7. `test_provider_local_flag_switches_active_reasoning_extension_to_ollama`.
13. **Comandos exactos de verificación**:
    - `python -B scripts/run_tests.py --timeout 45`
    - `git diff --check`
14. **Qué se verifica con dobles y qué requiere prueba real**:
    - Verificado al 100% con dobles de prueba y bases SQLite temporales.
15. **Criterios observables de aceptación**:
    - Suite completa pasando con `scripts/run_tests.py --timeout 45` y documentación actualizada.
16. **Comportamientos existentes que no deben cambiar**:
    - Todos los comandos de usuario (`/navegar`, `Anota...`, `Borra...`, `salir`, voz con `ENTER` vacío, `--speak` y `--provider local`) funcionan
    igual de cara al usuario, además del nuevo comando `/estado`.
17. **Riesgos y medidas de contención**:
    - *Riesgo*: Que los tests antiguos que parchean `ada.get_client` o `ada.run_browser_task` no surtan efecto si el orquestador ignora los atributos
    del módulo `ada`.
    - *Contención*: Pasar las referencias de `ada.py` al registrar los adaptadores en `chat_loop`, o actualizar los tests antiguos para usar el
    registro del orquestador (borrando tests antiguos duplicados).
18. **Aproximación de commits, mensajes y contenido**:
    - **Commit 1**: `feat(core): wire SessionOrchestrator, degraded mode, and /estado diagnostics into ADA`
    - **Commit 2**: `docs(architecture): document hexagonal microkernel extension system and /estado command`
19. **Límites de rollback y tratamiento del estado persistido**:
    - Rollback de la rama `feat/orchestrator-cutover` sin impacto en `ada_memory.db`.
20. **Condiciones para detenerse y pedir una decisión**:
    - Detenerse al completar los commits y reportar el estado final de la suite.
21. **Qué entrega la unidad y qué deja explícitamente fuera**:
    - *Entrega*: Arquitectura hexagonal + microkernel completa y operativa en producción (`Entrega-E`).
    - *Deja fuera*: Las ideas futuras opcionales (mejora de modelo STT, agente OpenCode y servidor MCP).
````*

L2. 2026-11-13: Baseline 134 tests pass in 22.233s; branch created from WU-07 merge; RDD is globally enabled. Real database hash captured; tests must never use it.
L3. 2026-11-13: Native scope question added exactly `ada_core/ports.py` and `ada_adapters/memory_sqlite.py`; answer verbatim: “Autorizar ampliación”. Full listing and atomic batches cannot be implemented using filtered recall and single-item mutations.
L4. Risk: item 1 (existing persistent memory commands), item 3 (consumed facade and memory port contracts). Independent verification required after own checks; no parallel writer reason.
L5. RED: 8 new resilience tests failed because `ada.create_session` did not exist. GREEN: 8 pass; full suite 142 tests in 23.659s. Earlier full run exposed five integration/fixture regressions, all corrected together: preserve pre-worker URL guard, use complete SDK-shaped response doubles, retain the facade identity in isolated audio tests, and explicitly mark stubbed audio dependencies available. Shared SQLite test cleanup now permits closing test-only worker-thread connections.
L6. Added one further complete-catalogue/confirmed-batch regression, prompt reload preservation, and docs; final own checks and independent verification pending. Risk also includes item 4 (voice input now crosses the registry's threaded execution boundary).
L7. Delivery follows the user's explicit two-commit boundary, even though the cohesive cutover exceeds the advisory 400-line slice. No PR is planned or authorized. Tracking mirror pending until the local document is synchronized.
L8. Final own pre-verifier suite: 143 tests pass in 20.128s; diff check passes; real DB hash unchanged. Local `/estado`/exit CLI smoke passes with UTF-8 output; PowerShell redirected cp1252 cannot encode the existing prompt emoji.
L9. Independent verifier found three baseline-confirmed blockers: live microphone reader after registry timeout, browser tests missing a readiness double in a clean environment, and a pure URL guard incorrectly prefixed with HTTP 403. One correction batch retains the interactive reader until it finishes (without repeating capture), adapts authorized old browser doubles, restores exact local rejection text, and commits three regression probes in the resilience suite. Also corrected initial status model display discovered by the CLI smoke. Recheck must be limited to these blockers; user explicitly allowed adapting facade-dependent old tests rather than bypassing production readiness.
L10. Independent targeted recheck passed all three original blockers: actual 30.2-second recording yielded one reasoning request and no competing terminal prompt; explicit second microphone request passes; clean-scratch browser suite 9/9 and six correction checks pass. The verifier's index-change note is accounted for: parent explicitly staged implementation/tests/tracking with `git add` while verification ran; no unexplained mutation occurred.
L11. Own post-correction full suite: 146 tests pass in 23.745s; diff-check passes. Native assessment is high for persisted-memory routing (item 1); consent/review pending. The full mirror was read back as observation 728; final synchronization follows each task update.
L12. Commit 1: `3f029e7`, 11 paths, 867 additions/113 deletions (980 authored lines including tracking). Parent spot check of verifier-reported browser suite passes 9/9 in 0.194s. Native assessment high; provider consent envelope medium, explicitly granted through the native question UI. Four provider-bound lens results were relayed unchanged in lens order after launch transport errors; native admission completed for all results. Review `review-ae51b8f8ef21ac89` approved and exact acknowledgement returned `gentle-ai.review-acknowledged/v1`, `action: acknowledged`, `authority: burned`, target `sha256:0ed8f1d406b41b98decbf6c0f88fd07963d7f0b2fef4659a21c9ba00c9263c19`. No source correction or review retry followed approval.
L13. Non-blocking native advisories retained for separate later work, without extending WU-08: name the browser timeout and retry-token multiplier; unify history trimming; interactive STT waits for its existing reader and cannot recover from a permanently hung device without interrupting; the patch-only resilience review questioned clean SQLite bootstrap (existing memory helpers initialize schema on access). No advisory grants approval for extra changes.
L14. Documentation explains ports/microkernel, six default extensions, local-only readiness versus remote health, honest provider limitations, atomic memory commands, independent audio/browser errors, and the interactive-reader timeout boundary. Work-unit rollback affects code/tests/docs only, with no database migration. Remaining optional model/browser live evaluations are outside the requested doubles-only proof. No remote publishing, future STT model work, OpenCode agent, or MCP server included.
L15. User later explicitly accepted the single-PR size exception and authorized publishing, merging, and deleting the local/remote feature branch using the GitHub account: “Hagamos una excepción, hacemos el PR, luego haces Merge, posterior a todo eso, eliminas la rama local y del repositorio, autorizo usar mi cuenta de github para todo eso.” PR created: https://github.com/AlvarezTDV/ada/pull/10. Remote main was an exact ancestor. Merge remains contingent on normal checks, with no admin bypass.
L16. Initial GitHub Linux CI failed ten assertions in two existing Groq provider tests: their mocked VoiceEngine was blocked by the real TTS dependency probe because CI intentionally installs no audio packages. A new deterministic regression forces all audio-package checks unavailable and observed RED (expected one speak call, got zero). The fixture now doubles TTS readiness alongside its existing fake engine, retaining every existing assertion and all production checks. Risk: none (test-only fixture and passive task log; no production behavior changed).
L17. CI-repair GREEN: Groq provider suite 10/10 in 0.479s; exact full runner 147 cases OK in 22.271s; git diff --check passes and real DB hash remains unchanged. The repair is a separate test-only commit; it does not alter or reopen the previously acknowledged production candidate.
