"""Provider registry. To add a provider, subclass ``Provider`` and append it
to ``PROVIDER_CLASSES`` (see CONTRIBUTING.md)."""

from __future__ import annotations

from aiusage.providers.base import AccountRef, Detection, Provider
from aiusage.providers.claude import ClaudeProvider
from aiusage.providers.codex import CodexProvider
from aiusage.providers.grok import GrokProvider

PROVIDER_CLASSES: list[type[Provider]] = [ClaudeProvider, CodexProvider, GrokProvider]

__all__ = [
    "AccountRef",
    "ClaudeProvider",
    "CodexProvider",
    "Detection",
    "GrokProvider",
    "PROVIDER_CLASSES",
    "Provider",
]
