"""Synchronous structural contracts implemented by adapters.

Capability calls raise CapabilityError on failure. They do not initialize or
dispose adapters; the runtime owns the independent lifecycle contract. Audio
input is an encoded byte payload whose format is agreed with the adapter.
"""

from typing import Protocol, runtime_checkable

from .domain import (
    CompletionRequest, CompletionResponse, MemoryItem, SpeechOutputReport,
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
class SpeechInputPort(Protocol):
    def transcribe(self, audio: bytes) -> str: ...


@runtime_checkable
class SpeechOutputPort(Protocol):
    def speak(self, text: str) -> SpeechOutputReport: ...


@runtime_checkable
class WebNavigationPort(Protocol):
    def navigate(self, request: WebNavigationRequest) -> WebNavigationResponse: ...
