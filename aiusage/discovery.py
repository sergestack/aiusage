"""Detect installed providers and collect usage from all of them."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from aiusage.config import Config
from aiusage.models import AccountUsage
from aiusage.providers import PROVIDER_CLASSES, Provider
from aiusage.providers.base import Detection


def build_providers(config: Config, only: Optional[str] = None) -> list[Provider]:
    providers = [cls(config) for cls in PROVIDER_CLASSES]
    if only:
        providers = [p for p in providers if p.id == only]
    return providers


def detect_all(config: Config) -> list[tuple[Provider, Detection]]:
    return [(p, p.detect()) for p in build_providers(config)]


def active_providers(config: Config) -> list[Provider]:
    """Enabled providers whose client is installed."""
    return [p for p in build_providers(config) if p.enabled and p.detect().installed]


def _safe_query(provider: Provider) -> list[AccountUsage]:
    try:
        return provider.query_all()
    except Exception as exc:  # one broken provider must not break the dashboard
        return [
            AccountUsage(
                provider=provider.id,
                account_name=provider.display_name,
                source=provider.display_name,
                error=f"unexpected error: {type(exc).__name__}: {exc}"[:300],
            )
        ]


def collect(config: Config, providers: Optional[list[Provider]] = None) -> list[AccountUsage]:
    """Query all active providers in parallel, keeping registry order."""
    providers = active_providers(config) if providers is None else providers
    if not providers:
        return []
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        results = list(pool.map(_safe_query, providers))
    return [account for group in results for account in group]
