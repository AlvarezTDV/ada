"""ReasoningPort bridges using the existing provider chain and request policy."""

from dataclasses import dataclass
from importlib import import_module
import os

from ada_core import (
    CapabilityError, CapabilityKind, CompletionRequest, CompletionResponse,
    ErrorCode, ExtensionManifest, PermissionSet, SideEffectProfile, TurnMode,
)


@dataclass(frozen=True)
class _ReasoningManifest(ExtensionManifest):
    """Add provider requirements without changing the shared core manifest."""

    required_env: tuple[str, ...] = ()


class _ReasoningAdapter:
    def __init__(self, *, client=None, model: str = ""):
        self._client = client
        self._model = model

    def _error(self, code, message):
        return CapabilityError(
            code, self.manifest.capability, message,
            extension_id=self.manifest.extension_id,
            retryable=code in (ErrorCode.TIMEOUT, ErrorCode.NETWORK_UNAVAILABLE,
                               ErrorCode.QUOTA_EXCEEDED),
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        try:
            helpers = import_module("ada")
        except ImportError as error:
            raise self._error(ErrorCode.DEPENDENCY_MISSING,
                              "Reasoning dependencies are unavailable.") from error
        try:
            if self._client is None:
                self._client, configured_model = helpers.get_client(self.provider)
                self._model = self._model or configured_model
            model = request.model or self._model
            if not model:
                model = (helpers.get_groq_model_chain()[0] if self.provider == "groq"
                         else (os.getenv("ADA_LOCAL_MODEL") or helpers.DEFAULT_LOCAL_MODEL).strip())
            response, selected_model = helpers.create_chat_completion_with_fallback(
                self._client, model,
                [{"role": message.role, "content": message.content}
                 for message in request.messages],
                temperature=request.temperature, max_tokens=request.max_tokens,
                think=request.mode == TurnMode.REASONING, provider=self.provider,
            )
        except helpers.ChatConfigError as error:
            raise self._error(ErrorCode.AUTH_INVALID,
                              "The provider credentials are missing or invalid.") from error
        except helpers.openai.APIError as error:
            code = ErrorCode.EXECUTION_FAILED
            for exception_type, candidate in (
                (helpers.openai.AuthenticationError, ErrorCode.AUTH_INVALID),
                (helpers.openai.PermissionDeniedError, ErrorCode.PERMISSION_DENIED),
                (helpers.openai.RateLimitError, ErrorCode.QUOTA_EXCEEDED),
                (helpers.openai.APITimeoutError, ErrorCode.TIMEOUT),
                (helpers.openai.APIConnectionError, ErrorCode.NETWORK_UNAVAILABLE),
            ):
                if isinstance(error, exception_type):
                    code = candidate
                    break
            raise self._error(code, "The reasoning provider could not complete the request.") from error
        if response.choices and response.choices[0].finish_reason == "length":
            raise self._error(ErrorCode.INCOMPLETE_RESPONSE,
                              "The provider returned an incomplete response.")
        text = helpers._completion_text(response)
        if not text:
            raise self._error(ErrorCode.EMPTY_RESPONSE,
                              "The provider returned an empty response.")
        return CompletionResponse(text, selected_model, response.choices[0].finish_reason)


class GroqChainReasoningAdapter(_ReasoningAdapter):
    """Rotate the configured Groq chain only on quota exhaustion (HTTP 429)."""

    provider = "groq"
    manifest = _ReasoningManifest(
        "reasoning.groq", CapabilityKind.REASONING, SideEffectProfile.PURE_READ,
        permissions=PermissionSet(network_hosts=("api.groq.com",)),
        required_env=("GROQ_API_KEY",),
    )


class OllamaReasoningAdapter(_ReasoningAdapter):
    """Use the local OpenAI-compatible endpoint with Ollama's options payload."""

    provider = "local"
    manifest = _ReasoningManifest(
        "reasoning.ollama_local", CapabilityKind.REASONING, SideEffectProfile.PURE_READ,
        permissions=PermissionSet(network_hosts=("localhost",)),
    )
