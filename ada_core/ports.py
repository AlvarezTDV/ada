"""Synchronous structural contracts implemented by adapters.

Capability calls raise CapabilityError on failure. They do not initialize or
dispose adapters; the runtime owns the independent lifecycle contract. Audio
input is an encoded byte payload whose format is agreed with the adapter.
"""

from typing import Protocol, runtime_checkable

from .domain import (
    ChatMessage, CompletionRequest, CompletionResponse, MemoryItem, SessionSummary, SpeechOutputReport,
    WebNavigationRequest, WebNavigationResponse,
)


@runtime_checkable
class ExtensionLifecyclePort(Protocol):
    def initialize(self) -> None: ...
    def shutdown(self) -> None: ...


@runtime_checkable
class ReasoningPort(Protocol):
    def complete(self, request: CompletionRequest) -> CompletionResponse: ...


@runtime_checkable
class MemoryPort(Protocol):
    def recall(self, query: str) -> tuple[MemoryItem, ...]: ...
    def save(self, item: MemoryItem) -> None: ...
    def delete(self, category: str, key: str) -> None: ...


@runtime_checkable
class MemoryCommandPort(MemoryPort, Protocol):
    """Optional command contract; retain the original single-item MemoryPort.

    Listing is complete and ordered, unlike bounded per-turn recall. Mutations
    validate the whole batch before writing and commit atomically. Return only
    committed normalized records or keys actually removed, never intended writes.
    """

    def list_all(self) -> tuple[MemoryItem, ...]: ...
    def save_batch(self, items: tuple[MemoryItem, ...]) -> tuple[MemoryItem, ...]: ...
    def delete_batch(self, keys: tuple[str, ...]) -> tuple[str, ...]: ...


@runtime_checkable
class ConversationStorePort(Protocol):
    """Persistent sessions and ordered, atomic conversation snapshots."""

    def create_session(self, summary: SessionSummary, messages: tuple[ChatMessage, ...]) -> None: ...
    def list_sessions(self) -> tuple[SessionSummary, ...]: ...
    def load_messages(self, session_id: str) -> tuple[ChatMessage, ...] | None:
        """Return None only if the session does not exist."""
        ...
    def save_messages(self, session_id: str, messages: tuple[ChatMessage, ...], updated_at: str) -> None: ...
    def delete_session(self, session_id: str) -> bool: ...


@runtime_checkable
class SpeechInputPort(Protocol):
    def transcribe(self, audio: bytes) -> str: ...


@runtime_checkable
class InteractiveSpeechInputPort(Protocol):
    """Short registry calls; terminal ownership and user waiting stay in the host."""

    def start_capture(self) -> None: ...
    def poll_capture(self) -> tuple[str, str]: ...
    def stop_capture(self) -> None: ...
    def cancel_capture(self) -> None: ...
    def shutdown(self) -> None: ...


@runtime_checkable
class SpeechOutputPort(Protocol):
    def speak(self, text: str) -> SpeechOutputReport: ...


@runtime_checkable
class WebNavigationPort(Protocol):
    def navigate(self, request: WebNavigationRequest) -> WebNavigationResponse: ...
