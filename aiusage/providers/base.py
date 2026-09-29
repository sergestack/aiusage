"""Provider interface.

A provider knows how to:

* ``detect()``            — is the client installed? (PATH / configured path)
* ``discover_accounts()`` — which authenticated profiles exist locally?
* ``query_usage()``       — fetch usage / limits for one discovered profile
* ``diagnose()``          — human-readable troubleshooting lines

Providers must never print or cache secrets, never fabricate missing values
and never present expired cached values as current.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from aiusage.config import Config
from aiusage.models import AccountUsage


@dataclass
class Detection:
    provider: str
    display_name: str
    installed: bool
    executable: Optional[str] = None
    version: Optional[str] = None
    note: Optional[str] = None


@dataclass
class AccountRef:
    """A locally discovered, not-yet-queried account/profile."""

    provider: str
    name: str
    profile: Optional[str] = None  # human path, e.g. "~/.codex-work"
    details: dict[str, Any] = field(default_factory=dict)


class Provider:
    id: str = ""
    display_name: str = ""
    vendor: str = ""

    def __init__(self, config: Config) -> None:
        self.config = config

    @property
    def settings(self) -> dict[str, Any]:
        return self.config.section(self.id)

    @property
    def enabled(self) -> bool:
        return self.config.provider_enabled(self.id)

    def detect(self) -> Detection:  # pragma: no cover - interface
        raise NotImplementedError

    def discover_accounts(self) -> list[AccountRef]:  # pragma: no cover - interface
        raise NotImplementedError

    def query_usage(self, ref: AccountRef) -> AccountUsage:  # pragma: no cover - interface
        raise NotImplementedError

    def query_all(self) -> list[AccountUsage]:
        """Query every discovered account. Providers may override this to
        de-duplicate profiles that turn out to be the same account."""
        return [self.query_usage(ref) for ref in self.discover_accounts()]

    def diagnose(self) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError
