<p align="center">
  <img src="assets/brand/ada-banner.gif" alt="A.D.A Animated Banner" width="100%">
</p>

# A.D.A. (Advanced Desktop Assistant)

A.D.A is a personal conversational assistant designed to run on your computer. It combines local speech recognition, low-latency responses, and persistent long-term memory.

## 🌟 Features

- 🎙️ **Local Push-to-Talk:** Transcribe speech locally with `faster-whisper`, without sending microphone recordings to a cloud transcription service.
- 🧠 **Persistent Memory:** Remember personal information and preferences in a local SQLite database (`ada_memory.db`).
- 💬 **Minimal Terminal Interface:** A clean `Rich` interface with ANSI screen updates to reduce console clutter.
- ⚡ **Reduced Latency:** Hybrid command routing and concurrent text-to-speech (TTS) generation prepare audio while the assistant is speaking.

ADA starts in text mode without loading audio dependencies or opening audio devices. Speech recognition and conversations through local Ollama run on your machine. Optional Edge TTS speech synthesis uses an online service and sends the text to be spoken.

## Privacy and Data Transfers

**Local chat does not make enabled speech output offline.** Speech output is off by default. With `--speak`, Edge TTS is the default synthesis engine and sends response text to an online speech service, independently of the chat provider.

| Operation | Where it runs | Data sent to an external service |
|---|---|---|
| Microphone transcription | Local Whisper | No microphone recording is sent to a cloud transcription service. The resulting text follows the chat or memory-command route below. |
| Chat through local Ollama | Your computer | No conversation data is sent to a cloud chat provider. With `--speak`, Edge TTS receives the spoken response text. |
| Cloud chat | OpenAI by default | Your current message, recent conversation history, and the system prompt, which includes all currently loaded memory entries. |
| Save a memory in cloud mode | Cloud chat provider for extraction; SQLite locally for storage | Your save command and the information it contains. |
| Delete a memory in cloud mode | Cloud chat provider for key selection; SQLite locally for deletion | Your delete command and the names of existing memory keys, not their stored values in this parsing request. No parsing request is made when memory is empty. |
| Optional voice output through Edge TTS (`--speak`) | Online speech service | Response text after speech cleanup, which can contain personal information or recalled memories. |

When `OPENAI_API_KEY` is present, ADA offers a choice between local and cloud chat; local is the default. Memory-command extraction uses that same selected chat provider. Command text and confirmation messages also enter the recent conversation history and can be included in later cloud chat requests.

The SQLite database (`ada_memory.db`) is stored locally, but that does not mean its contents always stay on the device: memory entries are included in cloud chat context, and information repeated in a response can be sent to Edge TTS. Deleting a local memory does not retract information already sent to an external service. This section describes conversation data flows, not installation or model downloads.

See [the architecture's data-flow details](ARCHITECTURE.md#flujo-de-datos-y-servicios-externos) for the implementation paths.

## ⚙️ Requirements

- Python 3.10 or later.
- [Ollama](https://ollama.com/) running locally with the configured model.
- Windows, Linux, or macOS.

## 🚀 Installation

1. Clone this repository and open the project directory.
2. Install the required AI, audio, and terminal-interface dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Start the configured local model in Ollama (default: `qwen2.5:3b`):
   ```bash
   ollama run qwen2.5:3b
   ```

### HTTP 403 During Speech Generation

If Edge TTS returns `403, message='Invalid response status'`, update the dependency in the same Python environment that runs ADA, then restart the assistant:

```bash
python -m pip install --upgrade "edge-tts>=7.3.1"
```

The minimum required version includes the Sec-MS-GEC v2 protocol and a server-date parsing fix. Updating addresses that incompatibility; it does not guarantee that every HTTP 403 error will be resolved. See the [7.3.0 and 7.3.1 release notes](https://github.com/rany2/edge-tts/releases).

## 🎮 Usage

Start A.D.A by running the main script:

```bash
python ada.py
```

The default conversational prompt and explicit memory commands are in Spanish. To enable spoken responses independently of microphone input:

```bash
python ada.py --speak
```

Run `python ada.py --help` for the available startup options. Audio dependencies in `requirements.txt` are needed only when using their respective audio features; missing or failing audio components report an error without preventing text chat.

### Interaction Controls
- **Text:** Type next to the `🔴 > ` prompt and press `ENTER`.
- **Microphone:** Leave the input empty and press `ENTER`. On first use, ADA loads Whisper (`base`, CPU/int8); the model may need downloading if it is not cached. When "Grabando..." ("Recording...") appears, speak, then **press ENTER again** to stop recording. The transcription is submitted automatically. Later microphone uses reuse the loaded model.
- **Spoken responses:** Disabled by default; opt in with `--speak`. Playback initializes on the first response, separately from Whisper. Edge TTS falls back to local synthesis if it fails; if both fail, the written response remains available.

Voice fallback tracks completed fragments: if Edge generation or playback fails, local synthesis continues from the first unfinished fragment instead of repeating the whole response. A fragment interrupted during playback restarts from its beginning, so a few words may repeat; exact word-level recovery is not guaranteed. Empty or missing generated audio also triggers fallback. If both engines fail, ADA reports the problem and keeps text chat available.

If recording fails after it starts, press `ENTER` to finish that recording attempt and return to typing. The first microphone use now bears the Whisper loading cost instead of chat startup.

### Measured Text Startup

On Windows with Python 3.13.7, the median time from launching a fresh process to the first text prompt fell from **3.86 s to 1.94 s**, a **1.92 s (49.8%) reduction** compared with commit `353fa4b`, before optional audio.

The comparison used one discarded warmup and five measured runs per version, alternating their order. Dependencies and the Whisper model were already cached; model downloads were disabled. Both versions used isolated SQLite databases and local-chat configuration, with real imports and initialization, stopping at the first prompt before submitting a message. This is a cached-startup measurement on one machine, not a cold-cache, first-microphone, or response-latency benchmark, and it does not guarantee the same improvement elsewhere.

### Managing Memories
Use explicit commands to save or delete information. Start your message with a supported Spanish command word; the examples below retain those keywords so they work with the current command parser.

- **Save information:** Use *"Anota"*, *"Guarda"*, or *"Memoriza"*.
  - *"Anota que prefiero el té verde."* — Remember that I prefer green tea.
  - *"Por favor guarda que soy ingeniero de software."* — Please remember that I am a software engineer.
- **Delete information:** Use *"Borra"*, *"Olvida"*, or *"Elimina"*.
  - *"Borra mi edad."* — Delete my age.
  - *"Olvida mi edad."* — Forget my age.

Memory commands validate the model's JSON before applying changes. Saving requires a non-empty array of objects with non-empty string `key` and `value` fields; `category` defaults to `general` when omitted and otherwise must also be a non-empty string. Surrounding whitespace is trimmed and line breaks are replaced with spaces. Existing keys are updated as before.

Deletion accepts only an array of non-empty string keys selected from those offered to the model. An empty selection means no match. Invalid batches are rejected in full, and database failures roll back the batch. ADA confirms only committed saves or actual deletions, using the existing error/no-match responses without additional startup notices. Validation checks structure and allowed keys, not whether the model understood an ambiguous request correctly.

## Prompt tests

Run the offline regression tests without starting audio hardware or a language model:

```bash
python -B -m unittest discover -s tests -v
```

The tests use temporary SQLite databases, never your `ada_memory.db`. They check that the storage-disclosure rule has a single source in `system_prompt.txt`, memory-as-data/current-turn guidance, prompt composition, and cache/reload behavior. The memory context does not duplicate the disclosure rule. These checks do not prove that a language model follows the instructions.

Memory-operation tests also mock model responses and cover input validation, unchanged database contents after rejected batches, transaction rollback, and confirmations based on actual results. Run them alone with `python -B -m unittest discover -s tests -p "test_memory_validation.py" -v`.

Optional-audio tests block audio imports during text startup and mock microphone, model, playback, and synthesis failures. Run them alone with `python -B -m unittest discover -s tests -p "test_optional_audio.py" -v`. These checks do not validate physical audio hardware or the live speech service.

Voice-fallback regressions cover explicit outcomes, generation without usable audio, playback failures, fragment-level recovery, and temporary-file cleanup: `python -B -m unittest discover -s tests -p "test_voice_fallback.py" -v`.

For a bounded behavioral evaluation, start Ollama with the configured default model (`qwen2.5:3b`), then opt in from PowerShell:

```powershell
$env:ADA_RUN_MODEL_EVALS = "1"
python -B -m unittest discover -s tests -v
Remove-Item Env:ADA_RUN_MODEL_EVALS
```

This evaluation sends three synthetic examples only to `127.0.0.1:11434`: a current preference overriding a stored one, an instruction embedded in a memory, and a direct storage question.

The preference case asks about the same favorite-drink fact stored in memory and checks a labeled current value, ending at punctuation or a line break. An explanation may mention the old preference afterward on the same or another line. A missing label is a response-format failure, not proof of a precedence failure.

The evaluation uses no cloud credentials, personal memories, or audio. Each request has a 30-second timeout and no retries. A missing server/model or a mismatched response fails the evaluation. A successful run is evidence for these examples, not a general prompt-injection guarantee.

All three live cases passed locally with `qwen2.5:3b` (3.1B, `Q4_K_M`), using synthetic memories and isolated databases. The test run took 6.74 seconds; this is validation of those three examples, not a general model-quality or latency benchmark.

## Release Tags

Use annotated Semantic Versioning tags in `vMAJOR.MINOR.PATCH` format. Format each tag message as `Release vX.Y.Z: <concise change summary>`.

---
*See [ARCHITECTURE.md](ARCHITECTURE.md) for more information about the project.*
