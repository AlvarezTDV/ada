# Supervised interactive STT (WU-1)
Objective: isolate microphone and inference failures while preserving written chat. Branch: `fix/stt-supervised-worker`, baseline `fa96f50`.
Delivery: single atomic commit (explicit user boundary); forecast ~1,200 authored lines. Functional runner: `python -B scripts/run_tests.py --timeout 120`; 45-second metric waived by L10. Risk: items 3 (shared port/input contract), 4 (IPC/ordering).

## Specs

### S1 — Branch and delivery boundary
- Create or switch to branch: `fix/stt-supervised-worker` (from `main` at `fa96f50` or current `HEAD`).
- Keep this fix in one atomic commit:
  `fix(stt): isolate interactive speech capture in a supervised worker`
- Do not push, open a PR, modify the real `ada_memory.db`, or fold unrelated memory/navigation/provider changes into this work unit without separate authorization.

### S2 — Objective
Replace the current thread-based interactive STT execution with a supervised worker process (`stt_worker.py`). A hung microphone, audio-library call, model load, or transcription must not require restarting ADA to continue written chat. At the same time, distinguish silent recordings from real hardware/model failures and fix the module-identity eviction in `tests/test_optional_audio.py`.

Runtime acceptance correction (L10): “Lo de 45 segundos es algo que acepte, no es necesario que ajustes una métrica,”. Functional offline correctness remains required; 45 seconds is not a delivery criterion and no further performance tuning is authorized.

### S3 — Required architecture
1. **Single terminal-input owner**:
   - The parent session (`ada.py` / input controller) owns terminal input (`stdin`).
   - `stt_worker.py` must never read the terminal directly; its `stdin`/`stdout` are used exclusively for a structured line-oriented JSON IPC protocol with the parent supervisor.
2. **Supervised worker process (`stt_worker.py`)**:
   - Run microphone capture, audio cleanup, Whisper initialization, and inference inside `stt_worker.py`.
   - Reuse the proven process-tree isolation pattern from `browser_navigation.py:91-146` (Windows Job Object `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` + bounded `stop_worker` and `process.wait()`), without modifying `browser_navigation.py` or `browser_worker.py`.
   - Keep the worker alive across successful captures so `WhisperModel("base", device="cpu", compute_type="int8")` remains cached after first use. Restart it only when recovery requires it.
3. **Structured IPC and job identity**:
   - Use small JSON messages for readiness/phase transitions, start/stop capture, transcription results, and structured errors (`dependency_missing` vs `execution_failed`) or shutdown.
   - Associate every request/response with a `job_id`. Reject late/stale results from a cancelled, timed-out, or replaced worker.
4. **Silence vs. failure**:
   - When capture and transcription succeed but produce empty/whitespace text (user pressed `ENTER` then `ENTER` in silence), return an empty transcription cleanly (`SessionResult("", kind="input")`) without raising `EMPTY_RESPONSE`, without marking `stt.faster_whisper` as `DEGRADED`/`FAILED`, and without invoking reasoning.
   - Real dependency, device, stream, cleanup, timeout, or transcription errors must raise `CapabilityError` with `DEPENDENCY_MISSING`, `TIMEOUT`, or `EXECUTION_FAILED` and update `/estado` accurately.
5. **Preserve registry-based routing and port compatibility**:
   - Route interactive STT through `ExtensionRegistry`. If interactive control needs an additional protocol in `ada_core/ports.py`, add a focused protocol without breaking `SpeechInputPort.transcribe(audio: bytes) -> str`.

### S4 — Keyboard and timeout policy
- Preserve empty `ENTER` to begin microphone input and the following `ENTER` to stop recording.
- Maintain exactly one terminal-input owner across normal chat, recording, timeout, recovery, and exit.
- The parent must remain able to detect worker death/failure or initialization deadlines while waiting for the stop `ENTER` (do not replace the current problem with another indefinitely blocking `input()` when a worker dies mid-recording; keep compatibility with tests that patch `builtins.input`).
- Separate deadlines by phase:
  - `init / start recording` (bounded deadline, e.g. 30s; configurable in tests);
  - `active user recording` (user-controlled until second `ENTER` — must NOT fail or time out at the 30-second registry default);
  - `stop / audio cleanup` (bounded deadline, e.g. 5s);
  - `transcription` (bounded deadline, e.g. 30s).
- On timeout or worker failure, terminate and reap the worker using bounded supervision, report the limitation, and return to written chat.
- Do not start another microphone worker until termination of the previous one is confirmed (`poll() is not None` / `wait()`). If termination cannot be confirmed, quarantine microphone use (`FAILED` in `/estado` with explicit reason) while keeping text chat available.
- Preserve existing `Ctrl+C`/exit behavior. Do not promise that terminating a process repairs a malfunctioning OS driver.

### S5 — Allowed edit surface
- `ada.py`
- `voice.py`
- `stt_worker.py` (new)
- `ada_core/orchestrator.py`
- `ada_core/ports.py`
- `ada_core/__init__.py`
- `ada_adapters/voice_adapters.py`
- `tests/test_stt_worker.py` (new)
- `tests/test_architectural_resilience.py`
- `tests/test_optional_audio.py`
- `tests/test_adapter_conformance.py`
- `README.md`
- `ARCHITECTURE.md`

The harness-owned feature log is the only additional tracking artifact.

### S6 — RED → GREEN → REFACTOR
1. First reproduce the current permanent-hang limitation with a deterministic test (`RED`).
2. Fix `tests/test_optional_audio.py` module-loading helper so it restores only `"voice"` in `sys.modules` (like `tests/test_memory_prompt.py:19-32`) instead of evicting `ada_adapters` via `patch.dict("sys.modules", ...)`, and isolates `memory.DB_PATH`.
3. Implement `stt_worker.py`, adapter supervision, and orchestrator/input coordination (`GREEN`), adding coverage for:
   1. Normal start/stop/transcription and model reuse across turns.
   2. Healthy recording exceeding the former 30-second boundary without marking STT failed/degraded.
   3. Worker hangs during initialization, stopping/cleanup, and transcription.
   4. Worker crash, malformed IPC, unexpected EOF, and stale results (`job_id` mismatch).
   5. Confirmed worker termination and later successful microphone recovery (plus quarantine if termination cannot be confirmed).
   6. The next written message is processed exactly once after an STT failure.
   7. `/estado` and `salir`/`exit`/`quit` remain usable after failure.
   8. Exact history and SQLite preservation after rejected/failed/silent capture.
   9. No surviving child processes or temporary recording files after exit.
4. Use real subprocess supervision with deterministic fake workers for hang/crash tests. Keep test databases and scratch state outside the repository.
5. Run focused tests, then:
   - Offline suite functional verification; use `python -B scripts/run_tests.py --timeout 120` for bounded execution with adequate margin. The original `python -B scripts/run_tests.py --timeout 45` failures are retained as evidence, not blockers (L10).
   - `git diff --check`
   - Verify `ada_memory.db` SHA-256 is unchanged.
6. Update `README.md` and `ARCHITECTURE.md` and commit everything together as:
   `fix(stt): isolate interactive speech capture in a supervised worker`

## Tasks
- T1 | S2,S6 | inline (one behavior; no writer trigger) | complete | WU-1 commit locator: `fix/stt-supervised-worker` | RED: bounded child test killed the old indefinite wait after 2s; GREEN: module identity/spec/isolation checks pass.
- T2 | S2-S5 | inline (same dependent behavior) | complete | WU-1 commit locator: `fix/stt-supervised-worker` | Queued structured errors survive a fast worker exit; independent differential recheck passes.
- T3 | S1-S6 | inline plus independent verifier for risk items 3/4 | complete | WU-1 commit locator: `fix/stt-supervised-worker` | Final 171-test suite passes; independent recheck has no remaining blockers.

## Log
- L1 (2026-11-14): Original request:

````text
# Work unit (WU-1): isolate interactive STT failures from the chat session

    ## Branch and delivery boundary

    - Create or switch to branch: `fix/stt-supervised-worker` (from `main` at `fa96f50` or current `HEAD`).
    - Keep this fix in one atomic commit:
      `fix(stt): isolate interactive speech capture in a supervised worker`
    - Do not push, open a PR, modify the real `ada_memory.db`, or fold unrelated memory/navigation/provider changes into this work unit without separate
    authorization.

    ## Objective

    Replace the current thread-based interactive STT execution with a supervised worker process (`stt_worker.py`). A hung microphone, audio-library call,
    model load, or transcription must not require restarting ADA to continue written chat. At the same time, distinguish silent recordings from real
    hardware/model failures and fix the module-identity eviction in `tests/test_optional_audio.py` so the full offline suite runs reliably under the
    45-second budget.

    ## Verified problems to solve in this unit

    1. **Permanent hang and thread races (`JD-01`)**:
       - `voice.py:201-253` (`VoiceEngine.listen_manual_push()`) reads `ENTER` directly with `input()` and performs microphone initialization, cleanup,
       and Whisper inference in the same call.
       - `ada_core/registry.py:174-214` (`ExtensionRegistry._invoke()`) runs capability calls in daemon threads with bounded waiting (`30.0s`), which
       cannot terminate a hung thread or native call.
       - `ada_core/orchestrator.py:161-190` (`SessionOrchestrator.listen()`) catches `ErrorCode.TIMEOUT` and calls `finished.wait()` with no timeout,
       freezing the session indefinitely if `PyAudio`, Whisper load/inference, or `input()` hangs.
       - Furthermore, `transcribe(port)` calls `finished.set()` before `ExtensionRegistry._invoke.run()` resets `entry.running = False` under
       `self._lock`, creating a race where a subsequent `registry.enable("stt.faster_whisper")` raises `"Extension still has a running call."`; and a
       timeout before `started.set()` returns early while the daemon thread still reaches `input("Grabando...")`.

    2. **Silent recordings and hardware errors conflated as `EMPTY_RESPONSE` (`JD-02`)**:
       - `voice.py:227-229` and `250-252` catch all `Exception`s (including `ImportError`, `PyAudio` errors, and Whisper failures) and return `""`.
       - `ada_adapters/voice_adapters.py:60-73` (`WhisperPushToTalkAdapter.transcribe()`) turns `""` into `CapabilityError(ErrorCode.EMPTY_RESPONSE, "No
       speech was transcribed.")`.
       - Consequently, both a broken microphone/Whisper crash and a normal silent recording mark `stt.faster_whisper` as `DEGRADED` and print the false
       LLM retry message `"El proveedor devolvió una respuesta vacía o truncada tras el reintento."`, making `ada.py:397-405` unreachable.

    3. **Module identity eviction and 45s timeout in `tests/test_optional_audio.py` (`JD-05`)**:
       - `tests/test_optional_audio.py` (lines 57, 217, 229, 239) executes `load_module(..., "ada.py")` inside `with patch.dict("sys.modules", {"voice":
       self.voice}):`. Exiting that `with` block evicts `ada_adapters` from `sys.modules` while `ada.register_default_adapters` retains the old class
       references, causing subsequent `patch("ada_adapters.voice_adapters._available", ...)` and
       `patch("ada_adapters.memory_sqlite.SqliteMemoryAdapter.recall", ...)` calls to patch a second module copy, failing 2 tests when run standalone,
       touching the real `ada_memory.db`, and pushing `python -B scripts/run_tests.py --timeout 45` over 45 seconds.
       - Also, `MagicMock()` entries in `sys.modules` for `AUDIO_MODULES` lack a valid `__spec__`, causing `importlib.util.find_spec()` to raise
       `ValueError`.

    ## Required architecture

    1. **Single terminal-input owner**:
       - The parent session (`ada.py` / input controller) owns terminal input (`stdin`).
       - `stt_worker.py` must never read the terminal directly; its `stdin`/`stdout` are used exclusively for a structured line-oriented JSON IPC
       protocol with the parent supervisor.
    2. **Supervised worker process (`stt_worker.py`)**:
       - Run microphone capture, audio cleanup, Whisper initialization, and inference inside `stt_worker.py`.
       - Reuse the proven process-tree isolation pattern from `browser_navigation.py:91-146` (Windows Job Object `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` +
       bounded `stop_worker` and `process.wait()`), without modifying `browser_navigation.py` or `browser_worker.py`.
       - Keep the worker alive across successful captures so `WhisperModel("base", device="cpu", compute_type="int8")` remains cached after first use.
       Restart it only when recovery requires it.
    3. **Structured IPC and job identity**:
       - Use small JSON messages for readiness/phase transitions, start/stop capture, transcription results, and structured errors (`dependency_missing`
       vs `execution_failed`) or shutdown.
       - Associate every request/response with a `job_id`. Reject late/stale results from a cancelled, timed-out, or replaced worker.
    4. **Silence vs. failure**:
       - When capture and transcription succeed but produce empty/whitespace text (user pressed `ENTER` then `ENTER` in silence), return an empty
       transcription cleanly (`SessionResult("", kind="input")`) without raising `EMPTY_RESPONSE`, without marking `stt.faster_whisper` as
       `DEGRADED`/`FAILED`, and without invoking reasoning.
       - Real dependency, device, stream, cleanup, timeout, or transcription errors must raise `CapabilityError` with `DEPENDENCY_MISSING`, `TIMEOUT`, or
       `EXECUTION_FAILED` and update `/estado` accurately.
    5. **Preserve registry-based routing and port compatibility**:
       - Route interactive STT through `ExtensionRegistry`. If interactive control needs an additional protocol in `ada_core/ports.py`, add a focused
       protocol without breaking `SpeechInputPort.transcribe(audio: bytes) -> str`.

    ## Keyboard and timeout policy

    - Preserve empty `ENTER` to begin microphone input and the following `ENTER` to stop recording.
    - Maintain exactly one terminal-input owner across normal chat, recording, timeout, recovery, and exit.
    - The parent must remain able to detect worker death/failure or initialization deadlines while waiting for the stop `ENTER` (do not replace the
    current problem with another indefinitely blocking `input()` when a worker dies mid-recording; keep compatibility with tests that patch
    `builtins.input`).
    - Separate deadlines by phase:
      - `init / start recording` (bounded deadline, e.g. 30s; configurable in tests);
      - `active user recording` (user-controlled until second `ENTER` — must NOT fail or time out at the 30-second registry default);
      - `stop / audio cleanup` (bounded deadline, e.g. 5s);
      - `transcription` (bounded deadline, e.g. 30s).
    - On timeout or worker failure, terminate and reap the worker using bounded supervision, report the limitation, and return to written chat.
    - Do not start another microphone worker until termination of the previous one is confirmed (`poll() is not None` / `wait()`). If termination cannot
    be confirmed, quarantine microphone use (`FAILED` in `/estado` with explicit reason) while keeping text chat available.
    - Preserve existing `Ctrl+C`/exit behavior. Do not promise that terminating a process repairs a malfunctioning OS driver.

    ## Allowed edit surface

    - `ada.py`
    - `voice.py`
    - `stt_worker.py` (new)
    - `ada_core/orchestrator.py`
    - `ada_core/ports.py`
    - `ada_core/__init__.py`
    - `ada_adapters/voice_adapters.py`
    - `tests/test_stt_worker.py` (new)
    - `tests/test_architectural_resilience.py`
    - `tests/test_optional_audio.py`
    - `tests/test_adapter_conformance.py`
    - `README.md`
    - `ARCHITECTURE.md`

    ## RED → GREEN → REFACTOR

    1. First reproduce the current permanent-hang limitation with a deterministic test (`RED`).
    2. Fix `tests/test_optional_audio.py` module-loading helper so it restores only `"voice"` in `sys.modules` (like `tests/test_memory_prompt.py:19-32`)
    instead of evicting `ada_adapters` via `patch.dict("sys.modules", ...)`, and isolates `memory.DB_PATH`.
    3. Implement `stt_worker.py`, adapter supervision, and orchestrator/input coordination (`GREEN`), adding coverage for:
       1. Normal start/stop/transcription and model reuse across turns.
       2. Healthy recording exceeding the former 30-second boundary without marking STT failed/degraded.
       3. Worker hangs during initialization, stopping/cleanup, and transcription.
       4. Worker crash, malformed IPC, unexpected EOF, and stale results (`job_id` mismatch).
       5. Confirmed worker termination and later successful microphone recovery (plus quarantine if termination cannot be confirmed).
       6. The next written message is processed exactly once after an STT failure.
       7. `/estado` and `salir`/`exit`/`quit` remain usable after failure.
       8. Exact history and SQLite preservation after rejected/failed/silent capture.
       9. No surviving child processes or temporary recording files after exit.
    4. Use real subprocess supervision with deterministic fake workers for hang/crash tests. Keep test databases and scratch state outside the
    repository.
    5. Run focused tests, then:
       - `python -B scripts/run_tests.py --timeout 45`
       - `git diff --check`
       - Verify `ada_memory.db` SHA-256 is unchanged.
    6. Update `README.md` and `ARCHITECTURE.md` and commit everything together as:
       `fix(stt): isolate interactive speech capture in a supervised worker` El prompt está en ingles pero yo soy español, para entenderte.
````
- L2: Clean main at fa96f50255ffbf20cf81261b391a7f1d64217b5f. Created authorized branch. Real DB baseline SHA-256: `959DAD05DD7D4EA68279AB1A4A11DF9FEA2D6970B6657D3E1ADE6E4FC33F8BA9`. Review mode on (global). No remote operations authorized.
- L3: One read-only explorer maps cross-cutting input/registry/process boundaries because mapping exceeds inline evidence budget. Parent implements the single dependent behavior inline; no parallel writers.
- L4: Explorer proposed parent-owned capture; rejected because S3 explicitly requires capture/cleanup inside the worker. Parent owns terminal input only. Added `InteractiveSpeechInputPort` without changing `SpeechInputPort`. Reuse `browser_navigation.worker_job` as an imported helper, without editing navigation files.
- L5: RED observed `STT timeout still waits indefinitely for the abandoned call` in `test_registry_timeout_does_not_wait_for_abandoned_recording` (outer subprocess killed/reaped after 2s). No DB access in that reproduction.
- L6: Focused combined suite: 55 tests, 46.999s, OK. Full suite budget not yet proven; fresh read-only command worker measures full offline command and bottlenecks. Early combined shell invocation hit its own aggregate timeout; process inventory showed no surviving fake workers, DB hash unchanged. Standalone process tests subsequently passed (12 tests, 14.550s).
- L7: Fake worker imports must stay minimal: the resilience fixture deliberately clears the environment, and importing unittest.mock/asyncio in a Windows fake child with that environment raises WinError 10106. Moving test-only imports below the fake-worker dispatch restored healthy fake workers without changing application environment/provider behavior.
- L8: Full Engram mirror refresh pending (initial mirror was condensed). No source commit or remote operation yet. Independent verification for risk items 3/4 remains pending after own checks.
- L9 (2026-11-15): Full suite timed out at 45s. Focused profiler: 55/55 pass; imports 10.599s, execution 44.143s. Parent instrumented the conformance fixture: `patch.dict(sys.modules, {"ada": ada})` evicted 500 newly loaded modules, including 115 `openai.resources` modules. A new identity regression failed RED, then fixture restoration was restricted to `ada`; standalone conformance GREEN: 13 tests in 4.900s. Full offline budget recheck delegated on changed bytes.
- L10 (2026-11-15), user correction verbatim: “Lo de 45 segundos es algo que acepte, no es necesario que ajustes una métrica,”. Removed the runtime metric from S2/S6 acceptance; stop further performance/baseline profiling. Instructed the read-only command worker to finish functional verification with a 120-second bound only if already-running complete checks do not provide passing evidence. No runner defaults/configuration changes. T3 remains pending only for functional verification, independent verification and delivery; completed identity/STT work remains valid.
- L11: Full feature document mirror (including original request and L10) updated in Engram observation #746, topic `odd/stt-supervised-worker/tasks`, with repository-relative locator; native readback confirmed Specs, Tasks, Log, original request and corrected runtime acceptance.
- L12: `ADA_RUN_MODEL_EVALS=0 python -B scripts/run_tests.py --timeout 120`: exit 0, `Ran 166 tests in 34.050s … OK`. Quiet runner omits skip counts. Real DB SHA-256 unchanged; no surviving Python processes. No timing/baseline investigation continued after L10.
- L13: Independent read-only verifier launched for risk items 3 (terminal/port compatibility) and 4 (IPC/lifecycle ordering), using external baseline/candidate scratch copies, full S1-S6 and differential public-flow probes. No source changes while it checks. Source formatting: no project formatter config or active local pre-commit hook found; `git diff --check` passes.
- L14: Independent verifier: no candidate-caused blockers. In external copies, STT 12 tests and resilience 17 tests pass; full offline suite 166 tests in 31.931s, OK. CLI `--help` and ordinary written chat, `/estado`, exit have identical stdout/stderr/exit to baseline; SQLite unchanged. Independent real-open-pipe test detects failure/reap without stop ENTER and processes subsequent text once. Legacy `SpeechInputPort` receives exactly `b""`. Native audio/manual console not exercised; redirected cp1252 emoji failure is reproduced on baseline and candidate (out of scope).
- L15: Retained verifier's three independent regression probes inside authorized `tests/test_stt_worker.py`, rather than adding its proposed out-of-surface test file. Parent rerun: 15 STT tests in 9.340s, OK; `git diff --check` passes; real DB SHA unchanged. Final full suite delegated on these test bytes. Verifier scratch `stt-verify-c665fkha` is disposable external generated state; parent cleanup follows.
- L16: Final candidate: `ADA_RUN_MODEL_EVALS=0 python -B scripts/run_tests.py --timeout 120` exits 0, 169 tests in 30.955s, OK. No Python survivors; real DB SHA-256 unchanged. Verifier scratch removed. No manual microphone/model/download or native console checks; offline subprocess tests only. Quiet runner does not expose skip count.
- L17: One atomic commit is explicitly authorized with subject `fix(stt): isolate interactive speech capture in a supervised worker`; all tasks share that commit, resolved by branch tip from baseline `fa96f50`. App/tests/docs form one dependent rollback boundary. Staged forecast was 1,740 additions+deletions including this harness log, larger than the initial estimate because fault-matrix and public-pipe regressions belong with the worker; the user's single-commit boundary is preserved. No source-mutating formatter or commit hook applies.
- L18: Native assessment: high, process-boundary signal and named item 4 (IPC/cancellation/lifecycle ordering). Initial assessment required declaring untracked scope; staging the three authored new files plus tracking document resolved that repository-state limitation. RDD remains globally enabled; committed-range assessment and candidate consent are separate post-commit bookkeeping, not authorization to push/publish. Record final commit/review identities in the Engram mirror without creating a second source commit.
- L19: Native review of original commit `6d3e199` completed and exact acknowledgement burned its approved authority (`review-23bf914bd525c09b`, target `sha256:f1d1dbcbad9880cd34162d5f1965f19b33128f74f914863f4ec02c86377daa3a`). Two informational findings: duplicated compatibility lifecycle (deferred), and fast-exit diagnostic ordering. No native correction was offered; that completed review remains closed.
- L20: The diagnostic notice directly concerns existing S3. Parent reproduced it through a new deterministic real-subprocess regression: worker sends initializing + dependency_missing and exits, parent waits for confirmed exit before polling; RED observes EXECUTION_FAILED instead of DEPENDENCY_MISSING. Reopened only T2/S3 and T3 proof. Correction drains already-buffered worker messages before synthesizing a generic EOF/exit failure; no lifecycle/phase API changes. Preserve one atomic commit by updating the unpublished WU commit after verification; any changed target is distinct from the closed review, never a replay/reopen.
- L21: Correction checks: focused 16 tests, OK; full offline 170 tests in 38.981s, OK. Targeted independent recheck reproduces the exact RED on `6d3e199`, GREEN on corrected candidate. A separate probe through production `stt_worker.run` confirms exact dependency/execution codes and messages after error-and-exit, generic unexpected EOF, confirmed reap, later recovery and isolated SQLite hash preservation. No new broad sweep or timing work.
- L22: Integrated that production-worker probe into authorized `tests/test_stt_worker.py`. Parent focused 17 tests in 14.630s, OK; whitespace check passes; DB SHA unchanged. Targeted verifier scratch removed. Final suite on the integrated test bytes is pending; no other source correction or compatibility refactor is included.
- L23: Final integrated candidate: `ADA_RUN_MODEL_EVALS=0 python -B scripts/run_tests.py --timeout 120` exits 0; 171 tests in 41.518s, OK. DB SHA-256 unchanged, no Python survivors, no scratch or index changes. Amend only this unpublished WU commit to retain the user's one-commit boundary. The old approved/acknowledged target remains closed; any assessment of corrected bytes uses the old reviewed commit as its delta base and does not reuse its consent.
