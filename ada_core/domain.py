"""Immutable values crossing capability boundaries, independent of SDK types."""

from dataclasses import dataclass
from enum import Enum


class TurnMode(str, Enum):
    CASUAL = "casual"
    TASK = "task"
    REASONING = "reasoning"


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class CompletionRequest:
    messages: tuple[ChatMessage, ...]
    mode: TurnMode = TurnMode.TASK
    model: str = ""
    temperature: float = 0.7
    max_tokens: int = 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))


@dataclass(frozen=True)
class CompletionResponse:
    text: str
    model: str
    finish_reason: str = "stop"


@dataclass(frozen=True)
class MemoryItem:
    category: str
    key: str
    value: str


@dataclass(frozen=True)
class SpeechOutputReport:
    success: bool
    engine: str
    completed_chunks: int
    stage: str = "complete"
    error: str = ""


@dataclass(frozen=True)
class WebNavigationRequest:
    url: str
    task: str


@dataclass(frozen=True)
class WebNavigationResponse:
    text: str
    url: str
