"""Provider-neutral data model for usage / limits."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# Severity thresholds, expressed as percent LEFT.
CRITICAL_MAX_LEFT = 20.0
LOW_MAX_LEFT = 40.0


@dataclass
class UsageWindow:
    """One limit a provider exposes: a rolling window, a billing period, a
    credit pool, ...

    Percent fields are only set when the provider exposed them (or exposed a
    positive denominator we can divide by). Missing values stay ``None`` —
    they are never defaulted to 0% or 100%.
    """

    label: str  # long label, e.g. "5-hour", "Week", "Month"
    short_label: Optional[str] = None  # compact label, e.g. "5h", "Wk"
    used_percent: Optional[float] = None
    used_amount: Optional[float] = None
    remaining_amount: Optional[float] = None
    total_amount: Optional[float] = None
    unit: Optional[str] = None  # "credits", "tokens", ...
    resets_at: Optional[int] = None  # epoch seconds
    unavailable_reason: Optional[str] = None
    expired: bool = False  # cached value whose reset time already passed
    source: Optional[str] = None
    # Optional presentation hint for known-intermittent windows.
    unavailable_display: Optional[str] = None

    @property
    def left_percent(self) -> Optional[float]:
        if self.used_percent is None:
            return None
        return max(0.0, min(100.0, 100.0 - self.used_percent))

    @property
    def available(self) -> bool:
        return not self.expired and self.used_percent is not None

    @property
    def display_label(self) -> str:
        return self.short_label or self.label

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("unavailable_display", None)
        data["left_percent"] = self.left_percent
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UsageWindow":
        from aiusage.util import to_float, to_int

        def opt_str(key: str) -> Optional[str]:
            value = data.get(key)
            return value if isinstance(value, str) else None

        return cls(
            label=str(data.get("label") or "Usage"),
            short_label=opt_str("short_label"),
            used_percent=to_float(data.get("used_percent")),
            used_amount=to_float(data.get("used_amount")),
            remaining_amount=to_float(data.get("remaining_amount")),
            total_amount=to_float(data.get("total_amount")),
            unit=opt_str("unit"),
            resets_at=to_int(data.get("resets_at")),
            unavailable_reason=opt_str("unavailable_reason"),
            expired=bool(data.get("expired")),
            source=opt_str("source"),
        )


@dataclass
class AccountUsage:
    provider: str  # provider id: "claude", "codex", "grok"
    account_name: str
    email: Optional[str] = None
    plan: Optional[str] = None
    windows: list[UsageWindow] = field(default_factory=list)
    source: str = ""
    fetched_at: int = field(default_factory=lambda: int(time.time()))
    stale: bool = False
    warning: Optional[str] = None
    error: Optional[str] = None
    profile: Optional[str] = None  # e.g. "~/.codex-work"
    manual_resets: Optional[int] = None  # e.g. Codex "usage limit reset" credits
    manual_reset_hint: Optional[str] = None
    stale_reason: Optional[str] = None
    stale_action: Optional[str] = None
    # Internal details for `diagnose`; never included in normal output.
    diagnostics: dict[str, Any] = field(default_factory=dict, repr=False)

    def visible_windows(self) -> list[UsageWindow]:
        return [w for w in self.windows if not w.expired]

    def all_windows_expired(self, now: Optional[int] = None) -> bool:
        now = int(time.time()) if now is None else now
        if not self.windows:
            return False
        return all(w.expired or (w.resets_at is not None and w.resets_at <= now) for w in self.windows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "account_name": self.account_name,
            "email": self.email,
            "plan": self.plan,
            "profile": self.profile,
            "windows": [w.to_dict() for w in self.windows],
            "source": self.source,
            "fetched_at": self.fetched_at,
            "stale": self.stale,
            "warning": self.warning,
            "error": self.error,
            "manual_resets": self.manual_resets,
        }


def classify_left(left: Optional[float]) -> str:
    """Severity for percent-left: ok (>40), low (21-40), critical (<=20), unknown."""
    if left is None:
        return "unknown"
    if left > LOW_MAX_LEFT:
        return "ok"
    if left > CRITICAL_MAX_LEFT:
        return "low"
    return "critical"


def urgency_candidates(accounts: list[AccountUsage]) -> list[tuple[float, AccountUsage, UsageWindow]]:
    """Available, current windows eligible for MOST URGENT, lowest left first.

    Errored, stale, expired and unavailable windows are excluded: a stale
    snapshot must never be presented as the most urgent live limit.
    """
    rows: list[tuple[float, AccountUsage, UsageWindow]] = []
    for account in accounts:
        if account.error or account.stale:
            continue
        for window in account.windows:
            if window.available and window.left_percent is not None:
                rows.append((window.left_percent, account, window))
    rows.sort(key=lambda r: r[0])
    return rows
