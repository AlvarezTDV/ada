"""Lazy push-to-talk and speech output bridges; probes never initialize audio."""

from dataclasses import dataclass
from importlib import import_module, util

from ada_core import (
    CapabilityError, CapabilityKind, ErrorCode, ExtensionManifest, ExtensionState,
    HealthReport, SideEffectProfile, SpeechOutputReport,
)


@dataclass(frozen=True)
class _VoiceManifest(ExtensionManifest):
    optional_packages: tuple[str, ...] = ()


def _voice_factory():
    return import_module("voice").VoiceEngine()


def _available(package):
    try:
        return util.find_spec(package) is not None
    except (ImportError, ValueError):
        return False


class _VoiceAdapter:
    def __init__(self, *, voice_factory=None):
        self._voice_factory = voice_factory if voice_factory is not None else _voice_factory
        self._engine = None

    def _voice(self):
        if self._engine is None:
            self._engine = self._voice_factory()
        return self._engine

    def _error(self, code, message):
        return CapabilityError(code, self.manifest.capability, message,
                               extension_id=self.manifest.extension_id)


class WhisperPushToTalkAdapter(_VoiceAdapter):
    """Use an empty audio buffer to request legacy microphone push-to-talk."""

    manifest = _VoiceManifest(
        "stt.faster_whisper", CapabilityKind.STT, SideEffectProfile.PURE_READ,
        optional_packages=("faster_whisper", "pyaudio", "numpy"),
    )

    @staticmethod
    def probe() -> HealthReport:
        missing = [name for name in WhisperPushToTalkAdapter.manifest.optional_packages
                   if not _available(name)]
        if missing:
            return HealthReport(ExtensionState.UNAVAILABLE_DEPENDENCY,
                                "Missing speech input packages: " + ", ".join(missing))
        return HealthReport(ExtensionState.READY)

    def transcribe(self, audio: bytes = b"") -> str:
        """Record interactively; pre-recorded buffers are not supported by this engine."""
        if audio:
            raise self._error(ErrorCode.EXECUTION_FAILED,
                              "Push-to-talk requires an empty audio buffer.")
        try:
            text = self._voice().listen_manual_push()
        except ImportError as error:
            raise self._error(ErrorCode.DEPENDENCY_MISSING, "Speech input dependencies are unavailable.") from error
        except Exception as error:
            raise self._error(ErrorCode.EXECUTION_FAILED, "Speech input failed.") from error
        if not text or not text.strip():
            raise self._error(ErrorCode.EMPTY_RESPONSE, "No speech was transcribed.")
        return text


class EdgeLocalTTSAdapter(_VoiceAdapter):
    manifest = _VoiceManifest(
        "tts.edge_with_local_fallback", CapabilityKind.TTS,
        SideEffectProfile.NON_IDEMPOTENT_MUTATION,
        optional_packages=("edge_tts", "pygame", "pyttsx3"),
    )

    @staticmethod
    def probe() -> HealthReport:
        if _available("edge_tts") and _available("pygame"):
            return HealthReport(ExtensionState.READY)
        if _available("pyttsx3"):
            return HealthReport(ExtensionState.DEGRADED, "Only local speech output is available.")
        return HealthReport(ExtensionState.UNAVAILABLE_DEPENDENCY,
                            "Speech output requires edge_tts and pygame, or pyttsx3.")

    def speak(self, text: str) -> SpeechOutputReport:
        try:
            result = self._voice().speak(text)
        except ImportError as error:
            raise self._error(ErrorCode.DEPENDENCY_MISSING, "Speech output dependencies are unavailable.") from error
        except Exception as error:
            raise self._error(ErrorCode.EXECUTION_FAILED, "Speech output failed.") from error
        if not result.success:
            raise self._error(ErrorCode.EXECUTION_FAILED, "Speech output failed; continue using text.")
        return SpeechOutputReport(result.success, result.engine, result.completed_chunks,
                                  result.stage or "complete", str(result.error) if result.error else "")
