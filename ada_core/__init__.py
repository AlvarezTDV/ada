"""Dependency-free domain contracts; adapters implement ports, not vice versa."""

CORE_CONTRACT_VERSION = 1

from .domain import (
    ChatMessage, CompletionRequest, CompletionResponse, MemoryItem,
    SessionMode, SessionSummary, SpeechOutputReport, TurnMode, WebNavigationRequest, WebNavigationResponse,
)
from .errors import CapabilityError, CapabilityKind, ErrorCode, SideEffectProfile, redact_secrets
from .manifest import ExecutionModel, ExtensionManifest, PermissionSet
from .ports import (
    ConversationStorePort, ExtensionLifecyclePort, InteractiveSpeechInputPort, MemoryCommandPort, MemoryPort,
    ReasoningPort, SpeechInputPort,
    SpeechOutputPort, WebNavigationPort,
)
from .registry import ExtensionRegistry, ExtensionState, HealthReport
from .orchestrator import SessionOrchestrator, SessionResult
from .session_manager import SessionManager

__all__ = [
    "CORE_CONTRACT_VERSION", "ChatMessage", "CompletionRequest", "CompletionResponse",
    "MemoryItem", "SpeechOutputReport", "TurnMode", "WebNavigationRequest",
    "WebNavigationResponse", "CapabilityError", "CapabilityKind", "ErrorCode",
    "SideEffectProfile", "redact_secrets", "ExecutionModel", "ExtensionManifest",
    "PermissionSet", "ExtensionLifecyclePort", "MemoryCommandPort", "MemoryPort", "ReasoningPort",
    "SpeechInputPort", "InteractiveSpeechInputPort", "SpeechOutputPort", "WebNavigationPort",
    "ExtensionRegistry", "ExtensionState", "HealthReport",
    "ConversationStorePort", "SessionManager", "SessionMode", "SessionOrchestrator", "SessionResult", "SessionSummary",
]
