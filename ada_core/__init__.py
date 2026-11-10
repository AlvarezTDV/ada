"""Dependency-free domain contracts; adapters implement ports, not vice versa."""

CORE_CONTRACT_VERSION = 1

from .domain import (
    ChatMessage, CompletionRequest, CompletionResponse, MemoryItem,
    SpeechOutputReport, TurnMode, WebNavigationRequest, WebNavigationResponse,
)
from .errors import CapabilityError, CapabilityKind, ErrorCode, SideEffectProfile, redact_secrets
from .manifest import ExecutionModel, ExtensionManifest, PermissionSet
from .ports import (
    ExtensionLifecyclePort, MemoryPort, ReasoningPort, SpeechInputPort,
    SpeechOutputPort, WebNavigationPort,
)

__all__ = [
    "CORE_CONTRACT_VERSION", "ChatMessage", "CompletionRequest", "CompletionResponse",
    "MemoryItem", "SpeechOutputReport", "TurnMode", "WebNavigationRequest",
    "WebNavigationResponse", "CapabilityError", "CapabilityKind", "ErrorCode",
    "SideEffectProfile", "redact_secrets", "ExecutionModel", "ExtensionManifest",
    "PermissionSet", "ExtensionLifecyclePort", "MemoryPort", "ReasoningPort",
    "SpeechInputPort", "SpeechOutputPort", "WebNavigationPort",
]
