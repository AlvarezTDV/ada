"""Structured failures and conservative retry policy for capability adapters."""

from __future__ import annotations

from enum import Enum
import os
import re
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .manifest import ExtensionManifest


class CapabilityKind(str, Enum):
    REASONING = "reasoning"
    MEMORY = "memory"
    STT = "stt"
    TTS = "tts"
    NAVIGATION = "navigation"
    AGENT = "agent"


class ErrorCode(str, Enum):
    AUTH_INVALID = "auth_invalid"
    QUOTA_EXCEEDED = "quota_exceeded"
    NETWORK_UNAVAILABLE = "network_unavailable"
    TIMEOUT = "timeout"
    EMPTY_RESPONSE = "empty_response"
    INCOMPLETE_RESPONSE = "incomplete_response"
    DEPENDENCY_MISSING = "dependency_missing"
    PERMISSION_DENIED = "permission_denied"
    INCOMPATIBLE_CONTRACT = "incompatible_contract"
    EXECUTION_FAILED = "execution_failed"


class SideEffectProfile(str, Enum):
    PURE_READ = "pure_read"
    IDEMPOTENT_WRITE = "idempotent_write"
    NON_IDEMPOTENT_MUTATION = "non_idempotent_mutation"


def redact_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Mask configured keys, literal extras and provider/auth token patterns.

    Merge all matching spans before substitution so overlapping secrets cannot
    expose a suffix or cause a later replacement to corrupt an earlier mask.
    This is redaction, not permission to log arbitrary provider payloads.
    """
    secrets = {
        value for value in (
            os.environ.get("GROQ_API_KEY", ""),
            os.environ.get("OPENAI_API_KEY", ""), *extra_secrets,
        ) if value
    }
    # Auth schemes are case-insensitive; bearer tokens may include base64 symbols.
    patterns = [r"(?i:Bearer) [A-Za-z0-9._~+/-]+=*", r"gsk_[A-Za-z0-9]+"]
    patterns.extend(re.escape(value) for value in secrets)
    # Lookahead also discovers nested token prefixes inside an earlier match.
    spans = sorted(
        match.span(1) for pattern in patterns
        for match in re.finditer(f"(?=({pattern}))", text)
    )
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    parts, cursor = [], 0
    for start, end in merged:
        parts.extend((text[cursor:start], "[REDACTED]"))
        cursor = end
    return "".join([*parts, text[cursor:]])


class CapabilityError(Exception):
    """Safe public message plus machine-readable routing and retry metadata."""

    def __init__(
        self, code: ErrorCode, capability: CapabilityKind, user_message: str, *,
        extension_id: str = "", retryable: bool = False,
        side_effect_uncertain: bool = False,
    ) -> None:
        self.code = code
        self.capability = capability
        self.user_message = redact_secrets(user_message)
        self.extension_id = extension_id
        self.retryable = retryable
        self.side_effect_uncertain = side_effect_uncertain
        super().__init__(self.user_message)

    @classmethod
    def from_timeout(cls, manifest: ExtensionManifest, user_message: str) -> CapabilityError:
        """Only pure reads may be retried automatically after an unknown outcome."""
        return cls(
            ErrorCode.TIMEOUT, manifest.capability, user_message,
            extension_id=manifest.extension_id,
            side_effect_uncertain=(
                manifest.side_effect_profile == SideEffectProfile.NON_IDEMPOTENT_MUTATION
            ),
            retryable=(manifest.side_effect_profile == SideEffectProfile.PURE_READ),
        )
