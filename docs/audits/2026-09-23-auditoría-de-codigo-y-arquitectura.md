# Auditoría de Código y Arquitectura: Proyecto A.D.A (Ada Assistant)

**Fecha:** 23 de Septiembre, 2026  
**Auditor:** Panel de Revisión Técnica & Arquitectura Senior  
**Estado General del Proyecto:** 🟡 `OPERA CON ADVERTENCIAS` (Score: **72/100**)

---

## 1. Resumen Ejecutivo

El proyecto **A.D.A** ha logrado construir con éxito su primera fase: un asistente conversacional conciso con personalidad definida, memoria persistente SQLite local e interacción por voz (STT/TTS). 

Sin embargo, tras una inspección técnica profunda del código fuente (`ada.py`, `voice.py`, `memory.py`, `system_prompt.txt`, `requirements.txt`), el equipo de auditoría ha identificado **deficiencias estructurales, fugas potenciales de recursos, cuellos de botella de latencia y riesgos de portabilidad**.

---

## 2. Evaluación Detallada por Componentes

### 2.1. Arquitectura General y Latencia (`ada.py`)
* 🔴 **Doble Llamada Asíncrona/Síncrona a la IA por Turno (Cuello de Botella):**
  * **Problema:** En cada mensaje del usuario, la función `check_and_save_tool_call` realiza una llamada completa al modelo para analizar si debe guardar memoria, y **luego** el bucle principal realiza la llamada para responder al usuario.
  * **Impacto:** Esto duplica el tiempo de respuesta total del sistema en cada turno de conversación.
* 🟡 **Lectura Ineficiente de Disco:**
  * **Problema:** `load_system_prompt()` lee el archivo `system_prompt.txt` del disco y ejecuta una consulta SQL `SELECT` en cada iteración del bucle.
  * **Impacto:** Operaciones I/O innecesarias en disco que deberían estar cacheadas en memoria RAM.
* 🟡 **Extracción de JSON Frágil:**
  * **Problema:** En `check_and_save_tool_call`, la captura del JSON de memoria depende de cortar cadenas con `content[find('{'):rfind('}')+1]`, y el bloque ignora cualquier error con `except Exception: pass`.
  * **Impacto:** Oculta fallos de ejecución y puede fallar si el modelo devuelve formatos inesperados.

---

### 2.2. Módulo de Voz e I/O (`voice.py`)
* 🔴 **Dependencia Exclusiva de Windows (`msvcrt`):**
  * **Problema:** El lector Push-To-Talk utiliza `import msvcrt`, una librería nativa y exclusiva de Windows.
  * **Impacto:** El proyecto romperá con un `ImportError` de inmediato si se intenta ejecutar en Linux o macOS.
* 🔴 **Riesgo de Fuga de Hardware (Audio Stream Unhandled):**
  * **Problema:** En `listen_manual_push()`, el flujo de PyAudio (`stream.open()`, `stream.read()`) no está dentro de un bloque `try...finally`.
  * **Impacto:** Si ocurre un error inesperado durante la lectura de audio, el dispositivo de micrófono de la computadora quedará bloqueado por el sistema operativo.
* 🟡 **Privacidad de Voz (STT Remoto vs Local):**
  * **Problema:** La transcripción de voz utiliza `recognize_google`, enviando el audio por internet a los servidores de Google.
  * **Impacto:** Contradice el objetivo de tener una arquitectura de asistente 100% privada y local.

---

### 2.3. Base de Datos y Memoria Persistente (`memory.py`)
* 🟡 **Reconexiones Múltiples Redundantes a SQLite:**
  * **Problema:** `save_memory()` llama a `init_db()` (que abre y cierra la conexión), e inmediatamente vuelve a abrir otra conexión `sqlite3.connect()`.
  * **Impacto:** Falta de un gestor de contexto limpio (`with sqlite3.connect...`) o pool de conexiones.
* 🟡 **Riesgo de Inyección de Prompts por Memoria:**
  * **Problema:** Los valores de la base de datos se concatenan directamente en el `System Prompt` mediante `format_memories_for_prompt()`.
  * **Impacto:** Si se guarda en memoria una frase maliciosa, podría sobrescribir la personalidad o reglas de A.D.A.

---

### 2.4. Entorno y Configuración (`requirements.txt`, `.gitignore`)
* 🔴 **Falta de Librerías en `requirements.txt`:**
  * **Problema:** Se está usando `import pygame` en `voice.py`, pero `pygame` no está incluido en `requirements.txt`. Además, ninguna librería tiene versión fijada (ej. `openai>=1.0.0`).
  * **Impacto:** Si otra persona o servidor intenta instalar el proyecto desde cero, fallará al iniciar la voz.

---

## 3. Matriz de Deficiencias y Propuestas de Mejora

| Componente | Deficiencia Detectada | Gravedad | Propuesta de Mejora (Solución Técnica) |
| :--- | :--- | :--- | :--- |
| **`ada.py`** | Doble llamada al modelo por turno (evaluación de memoria + respuesta). | **ALTA** | **Evaluación Asíncrona / Tool Calling:** Mover la evaluación de memoria a un hilo secundario en segundo plano o usar *Function Calling* nativo del modelo. |
| **`voice.py`** | Uso de `msvcrt` exclusivo de Windows. | **ALTA** | **Portabilidad Multiplataforma:** Reemplazar `msvcrt` por `pynput` o un controlador de eventos de teclado multiplataforma. |
| **`voice.py`** | Fuga de recursos de micrófono en excepciones. | **ALTA** | **Gestión de Recursos Segura:** Envolver todo el flujo de PyAudio en bloques `try...finally` garantizando `stream.close()` y `p.terminate()`. |
| **`voice.py`** | Transcripción mediante Google API (Nube). | **MEDIA** | **STT 100% Local:** Migrar `SpeechRecognition` a **`faster-whisper`** o **`whisper.cpp`** para transcripción instantánea y privada en local. |
| **`memory.py`** | Ineficiencia en consultas y aperturas de DB. | **MEDIA** | **Context Manager & Caching:** Usar gestores de contexto `with sqlite3.connect` y cachear el prompt en RAM, actualizándolo solo cuando cambie la base de datos. |
| **`requirements.txt`** | Falta `pygame` y versiones desprotegidas. | **ALTA** | **Fijar Dependencias:** Añadir `pygame` y congelar versiones (`pip freeze > requirements.txt`). |

---

## 4. Propuestas para Expandir el Proyecto (Roadmap Futuro)

Para llevar a A.D.A a su siguiente nivel funcional como un asistente tipo **JARVIS complete**, recomendamos la siguiente hoja de ruta en tres fases:

### 🚀 Fase 1: Capa de Automatización y "Manos" (Computer Control)
1. **Creación del Módulo de Herramientas (`ada/tools/`):**
   * **`system_tools.py`:** Abrir/cerrar aplicaciones (Spotify, VSCode, Navegador), consultar uso de CPU/RAM/Batería.
   * **`browser_tools.py`:** Automatizar búsquedas web y lectura de sitios usando `Playwright`.
   * **`file_tools.py`:** Leer, crear o resumir archivos de texto y código en la computadora del usuario.

### 🧠 Fase 2: Orquestación e Inteligencia Híbrida (Model Router)
1. **Detección de Intención (Router Local vs Nube):**
   * Implementar un clasificador ligero: las preguntas sencillas o charlas las resuelve **Qwen 2.5 3B local**.
   * Las tareas pesadas de programación, análisis o código complejo se delegan automáticamente a **GPT-4o / Claude 3.5 Sonnet en la nube**.

### 🎨 Fase 3: Interfaz Gráfica y Presencia Visual (GUI / Overlay)
1. **Minimalist HUD / Floating Widget:**
   * Crear una pequeña ventana flotante transparente (usando `PyQt` o `CustomTkinter`) que muestre el estado de A.D.A (Escuchando, Pensando, Hablando) mediante un orbe o animación sutil en pantalla.

---

## 5. Dictamen Final del Panel de Auditoría

El proyecto **A.D.A** cuenta con un diseño conceptual excelente, una personalidad bien lograda y una integración de voz limpia mediante Push-to-Talk. Aplicando las correcciones de estabilidad y portabilidad mencionadas en esta auditoría, el sistema quedará listo para recibir sus primeras herramientas de automatización.
