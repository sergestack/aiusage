"""Terminal dashboard, Telegram and JSON renderers.

Alignment rule: every field is padded as plain text FIRST and only then
wrapped in ANSI codes, so colors never shift columns.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Optional

from aiusage import __version__
from aiusage.models import (
    AccountUsage,
    UsageWindow,
    classify_left,
    urgency_candidates,
)
from aiusage.util import compact_duration

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment,misc]

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SEVERITY_COLOR = {"ok": "32", "low": "33", "critical": "31;1", "unknown": "2"}
PCT_W = 4  # "100%", " 34%", " N/A"
VENDOR = {"claude": "Anthropic", "codex": "OpenAI", "grok": "xAI"}


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def ansi(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled and code and text else text


def resolve_tz(name: Optional[str]) -> tzinfo:
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    local = datetime.now().astimezone().tzinfo
    assert local is not None
    return local


def local_zone_name() -> str:
    """IANA name of the system timezone (e.g. "America/New_York"), falling
    back to the current abbreviation when it cannot be determined."""
    tz_env = os.environ.get("TZ", "").lstrip(":")
    if tz_env and "/" in tz_env and not tz_env.startswith("/"):
        return tz_env
    try:
        name = Path("/etc/timezone").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return datetime.now().astimezone().strftime("%Z") or "local"


def fmt_clock(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    zone = dt.strftime("%Z")
    return f"{hour}:{dt.minute:02d} {suffix}" + (f" {zone}" if zone else "")


def fmt_reset(epoch: Optional[int], tz: tzinfo, now: Optional[float] = None) -> tuple[str, str]:
    """(reset text, countdown text) for a reset epoch."""
    if not epoch:
        return "reset unavailable", ""
    now = time.time() if now is None else now
    dt = datetime.fromtimestamp(epoch, tz)
    if epoch <= now:
        return f"reset passed ({dt.strftime('%b')} {dt.day}, {fmt_clock(dt)})", ""
    today = datetime.fromtimestamp(now, tz).date()
    if dt.date() == today:
        when = fmt_clock(dt)
    elif (dt.date() - today).days < 7:
        when = f"{dt.strftime('%a')} {fmt_clock(dt)}"
    else:
        when = f"{dt.strftime('%a %b')} {dt.day}, {fmt_clock(dt)}"
    return f"resets {when}", f"in {compact_duration(epoch - now)}"


def window_reset(window: UsageWindow, tz: tzinfo) -> tuple[str, str]:
    if window.resets_at is None and window.used_percent == 0:
        return "unused window", "no active reset"
    return fmt_reset(window.resets_at, tz)


def format_amount(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value)):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def amount_text(window: UsageWindow) -> Optional[str]:
    """Absolute usage for windows without a percentage, e.g. Grok credits."""
    if window.used_amount is None:
        return None
    unit = window.unit or "units"
    if window.total_amount is not None and window.total_amount > 0:
        return f"{format_amount(window.used_amount)} / {format_amount(window.total_amount)} {unit} used"
    return f"{format_amount(window.used_amount)} {unit} used · quota not exposed"


def unavailable_text(window: UsageWindow) -> str:
    if window.unavailable_display:
        return window.unavailable_display
    return f"unavailable ({window.unavailable_reason or 'not exposed'})"


def meter(left: Optional[float], width: int) -> tuple[str, str]:
    if left is None:
        return "", "░" * width
    filled = int(round(width * max(0.0, min(100.0, left)) / 100))
    return "█" * filled, "░" * (width - filled)


def pct_text(left: Optional[float]) -> str:
    return " N/A" if left is None else f"{left:>3.0f}%"


# ------------------------------------------------------------------ urgent
def most_urgent(accounts: list[AccountUsage], tz: tzinfo, color: bool) -> str:
    rows = urgency_candidates(accounts)
    if not rows:
        return ansi("NO URGENT LIMITS", "2", color)
    left, account, window = rows[0]
    severity = classify_left(left)
    if severity == "ok":
        return "\n".join(
            [
                ansi("NO URGENT LIMITS", "32", color),
                ansi(f"Lowest available: {account.account_name} · {window.label} · {left:.0f}% left", "2", color),
            ]
        )
    _, countdown = fmt_reset(window.resets_at, tz)
    text = (
        f"MOST URGENT: {account.account_name} · {window.label} · {left:.0f}% LEFT · "
        f"{severity.upper()} · resets {countdown or 'unknown'}"
    )
    if account.manual_resets:
        text += " · MANUAL RESET AVAILABLE"
    return ansi(text, SEVERITY_COLOR[severity], color)


# ------------------------------------------------------------------ dashboard
def _widths(accounts: list[AccountUsage], tz: tzinfo) -> dict[str, int]:
    labels = [2]
    resets = [12]
    countdowns = [0]
    for account in accounts:
        if account.error:
            continue
        for window in account.visible_windows():
            labels.append(len(window.display_label))
            reset, countdown = window_reset(window, tz)
            if window.available or window.used_amount is not None or window.resets_at:
                resets.append(len(reset))
                countdowns.append(len(countdown))
    cols = shutil.get_terminal_size((96, 24)).columns
    return {
        "label": max(labels),
        "bar": 10 if cols < 70 else 20,
        "reset": max(resets),
        "countdown": max(countdowns),
    }


def _window_lines(window: UsageWindow, account: AccountUsage, w: dict[str, int], tz: tzinfo, color: bool) -> list[str]:
    stale = account.stale
    left = window.left_percent if window.available else None
    severity = "unknown" if stale or left is None else classify_left(left)
    code = SEVERITY_COLOR[severity]
    label = window.display_label.ljust(w["label"])
    filled, empty = meter(left, w["bar"])
    bar_field = ansi(filled, code, color) + ansi(empty, "2" if left is None else "", color)
    pct_field = ansi(pct_text(left).rjust(PCT_W), code, color)
    prefix = f"  {label}  {bar_field}  {pct_field}  "

    if left is None:
        detail = amount_text(window)
        has_reset = window.resets_at is not None
        lines: list[str] = []
        if detail:
            reset, countdown = window_reset(window, tz)
            first = f"{reset.ljust(w['reset'])}  {countdown.ljust(w['countdown'])}".rstrip() if has_reset else ""
            lines.append((prefix + first).rstrip() + (f"  {ansi('STALE', '33', color)}" if stale else ""))
            indent = " " * (2 + w["label"] + 2 + w["bar"] + 2 + PCT_W + 2)
            lines.append(ansi(indent + detail, "2", color))
            return lines
        return [prefix + ansi(unavailable_text(window), "2", color)]

    reset, countdown = window_reset(window, tz)
    line = f"{prefix}{reset.ljust(w['reset'])}  {countdown.ljust(w['countdown'])}"
    if stale:
        line += "  " + ansi("STALE", "33", color)
    elif severity in ("low", "critical"):
        line += "  " + ansi(severity.upper(), code, color)
    return [line.rstrip()]


def render_dashboard(accounts: list[AccountUsage], tz_name: Optional[str] = None, color: bool = False,
                     now: Optional[float] = None) -> str:
    tz = resolve_tz(tz_name)
    now = time.time() if now is None else now
    stamp = datetime.fromtimestamp(now, tz)
    cols = shutil.get_terminal_size((96, 24)).columns
    sep = "─" * min(60, cols)
    heading = f"AI USAGE  ·  {stamp.strftime('%a %b')} {stamp.day}, {stamp.year} · {fmt_clock(stamp)}"
    blocks = [ansi(heading, "1;37", color), sep, ""]

    if not accounts:
        blocks.append("No supported AI coding agents with usage data were found.")
        blocks.append(ansi("Run `aiusage detect` to see what was checked.", "2", color))
        blocks.append(sep)
        return "\n".join(blocks)

    blocks.append(most_urgent(accounts, tz, color))
    blocks.append("")
    widths = _widths(accounts, tz)

    for idx, account in enumerate(accounts):
        if idx:
            blocks.append("")
        vendor = VENDOR.get(account.provider, account.provider)
        identity = f" · {account.email or 'account identity unavailable'}"
        if account.plan:
            identity += f" · {account.plan.upper()}"
        blocks.append(ansi(f"▸ {account.account_name} — {vendor}", "1;36", color) + ansi(identity, "2", color))
        age = compact_duration(now - account.fetched_at)

        if account.error:
            blocks.append(ansi(f"  ✗ {account.error}", "31", color))
            if account.warning:
                blocks.append(ansi(f"  ⚠ {account.warning}", "33", color))
            if account.source:
                blocks.append(ansi(f"  ↳ {account.source}", "2", color))
            continue

        if account.stale:
            blocks.append(ansi("  stale snapshot — live refresh unavailable", "33", color))
            if account.stale_reason:
                blocks.append(ansi(f"  reason: {account.stale_reason}", "2", color))

        for window in account.visible_windows():
            blocks.extend(_window_lines(window, account, widths, tz, color))

        if account.manual_resets:
            noun = "reset" if account.manual_resets == 1 else "resets"
            hint = f" · {account.manual_reset_hint}" if account.manual_reset_hint else ""
            blocks.append(ansi(f"  {account.manual_resets} manual {noun} available{hint}", "36", color))
        if account.stale and account.stale_action:
            blocks.append(ansi(f"  action: {account.stale_action}", "2", color))
        suffix = " · STALE" if account.stale else ""
        blocks.append(ansi(f"  ↳ {account.source} · {age} ago{suffix}", "2", color))
        if account.warning:
            blocks.append(ansi(f"  ⚠ {account.warning}", "33", color))

    blocks.append(sep)
    return "\n".join(blocks)


# ------------------------------------------------------------------ telegram
def _short_bar(left: Optional[float], width: int = 12) -> str:
    if left is None:
        return "□" * width
    filled = int(round(width * left / 100))
    return "■" * filled + "□" * (width - filled)


def _compact_reset(window: UsageWindow, tz: tzinfo) -> str:
    if window.resets_at is None:
        return "unused" if window.used_percent == 0 else ""
    now = time.time()
    if window.resets_at <= now:
        return ""
    dt = datetime.fromtimestamp(window.resets_at, tz)
    remaining = compact_duration(window.resets_at - now)
    days = (dt.date() - datetime.fromtimestamp(now, tz).date()).days
    if days == 0:
        return f"in {remaining}"
    if days < 7:
        return f"{dt.strftime('%a')} {remaining}"
    return f"{dt.strftime('%a %b')} {dt.day} {remaining}"


def render_telegram(accounts: list[AccountUsage], tz_name: Optional[str] = None) -> str:
    tz = resolve_tz(tz_name)
    stamp = datetime.now(tz)
    lines = [f"AI USAGE · {stamp.strftime('%a %b')} {stamp.day} · {fmt_clock(stamp)}", ""]
    if not accounts:
        lines.append("No supported AI coding agents found.")
    for account in accounts:
        header = f"*{account.account_name}*"
        if account.plan:
            header += f" · {account.plan.upper()}"
        if account.error:
            lines.append(f"{header} ✗ {account.error}")
            continue
        lines.append(header)
        if account.stale:
            lines.append(f"stale snapshot · last update {compact_duration(time.time() - account.fetched_at)} ago")
        for window in account.visible_windows():
            label = f"`{window.display_label}`"
            left = window.left_percent if window.available else None
            if left is None:
                detail = amount_text(window) or unavailable_text(window)
                when = _compact_reset(window, tz)
                lines.append(f"{label} {detail}" + (f" · {when}" if when else ""))
                continue
            pct = f"{left:.0f}%"
            if not account.stale:
                severity = classify_left(left)
                if severity == "critical":
                    pct = f"*⚠️ {pct}*"
                elif severity == "low":
                    pct = f"_{pct}_"
            tail = "  stale" if account.stale else ""
            lines.append(f"{label} {_short_bar(left)}  {pct}  {_compact_reset(window, tz)}{tail}".rstrip())
        if account.manual_resets:
            noun = "reset" if account.manual_resets == 1 else "resets"
            lines.append(f"{account.manual_resets} manual {noun} available")
    return "\n".join(lines)


# ------------------------------------------------------------------ json
def render_json_payload(accounts: list[AccountUsage], tz_name: Optional[str] = None) -> dict[str, Any]:
    return {
        "aiusage_version": __version__,
        "generated_at": int(time.time()),
        "timezone": tz_name or local_zone_name(),
        "accounts": [a.to_dict() for a in accounts],
    }
