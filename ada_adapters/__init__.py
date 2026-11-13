"""Replaceable capability implementations; construction performs no I/O."""

from .memory_sqlite import SqliteMemoryAdapter
from .reasoning_adapters import GroqChainReasoningAdapter, OllamaReasoningAdapter
from .voice_adapters import EdgeLocalTTSAdapter, WhisperPushToTalkAdapter
from .browser_navigation_adapter import BrowserUseNavigationAdapter

__all__ = [
    "GroqChainReasoningAdapter", "OllamaReasoningAdapter", "SqliteMemoryAdapter",
    "WhisperPushToTalkAdapter", "EdgeLocalTTSAdapter", "BrowserUseNavigationAdapter",
    "register_default_adapters",
]


def register_default_adapters(registry, *, provider="groq", voice_factory=None, browser_runner=None):
    """Register one reasoning provider and all current capabilities without I/O.

    ``provider`` is ``groq`` or ``local``. Injected voice factories run only on
    first speech use; browser runners run only for an explicit navigation call.
    STT and TTS own separate lazy engines so either may degrade independently.
    """
    reasoning_type = {"groq": GroqChainReasoningAdapter, "local": OllamaReasoningAdapter}[provider]
    registry.register(reasoning_type.manifest, reasoning_type)
    registry.register(SqliteMemoryAdapter.manifest, SqliteMemoryAdapter)
    for adapter in (
        WhisperPushToTalkAdapter(voice_factory=voice_factory),
        EdgeLocalTTSAdapter(voice_factory=voice_factory),
        BrowserUseNavigationAdapter(browser_runner=browser_runner),
    ):
        registry.register(adapter.manifest, lambda adapter=adapter: adapter, probe=adapter.probe)
