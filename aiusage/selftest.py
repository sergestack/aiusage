"""`aiusage self-test`: offline checks that need no network, no accounts and
no test framework. The full suite lives in tests/ (pytest)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone

from aiusage.config import Config
from aiusage.models import AccountUsage, UsageWindow
from aiusage.providers.claude import ClaudeProvider
from aiusage.providers.codex import build_windows
from aiusage.providers.grok import window_from_billing
from aiusage.renderer import most_urgent, render_dashboard, render_json_payload, render_telegram, resolve_tz, strip_ansi


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def run_self_test() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            failures.append(name)

    now = int(time.time())
    tz = resolve_tz("UTC")
    old_cache = os.environ.get("AIUSAGE_CACHE_DIR")
    with tempfile.TemporaryDirectory(prefix="aiusage-selftest.") as tmp:
        os.environ["AIUSAGE_CACHE_DIR"] = tmp
        try:
            # Claude
            windows = ClaudeProvider.parse_live(
                {"five_hour": {"utilization": 0.0, "resets_at": None},
                 "seven_day": {"utilization": 34.0, "resets_at": _iso(now + 86400)}}
            )
            check("Claude live: unused 5h without reset accepted",
                  windows[0].used_percent == 0 and windows[0].resets_at is None and windows[1].resets_at == now + 86400)
            try:
                ClaudeProvider.parse_live({"five_hour": {"utilization": 101, "resets_at": _iso(now)}, "seven_day": {}})
                check("Claude live: invalid utilization rejected", False)
            except ValueError:
                check("Claude live: invalid utilization rejected", True)
            check("Claude statusLine: partial rate_limits rejected",
                  ClaudeProvider.parse_statusline_rate_limits({"five_hour": {"used_percentage": 5}}) is None)

            # Codex
            ws, _warn, raw = build_windows(
                {"primary": {"usedPercent": 15, "resetsAt": now + 6 * 86400, "windowDurationMins": 10080}}, now)
            check("Codex weekly never copied into 5h",
                  ws[0].used_percent is None and ws[1].used_percent == 15 and raw["weeklySource"] == "primary")
            ws, _warn, _raw = build_windows(
                {"primary": {"usedPercent": 25, "resetsAt": now + 7 * 3600, "windowDurationMins": 300},
                 "secondary": {"usedPercent": 15, "resetsAt": now + 6 * 86400, "windowDurationMins": 10080}}, now)
            check("Codex 5h reset sanity check", ws[0].used_percent is None and ws[1].used_percent == 15)
            ws, _warn, _raw = build_windows(
                {"primary": {"usedPercent": 101, "resetsAt": now + 86400, "windowDurationMins": 10080}}, now)
            check("Codex invalid usedPercent unavailable", ws[1].used_percent is None)

            # Grok
            w = window_from_billing({"monthlyLimit": {"val": 100}, "used": {"val": 25},
                                     "billingPeriodEnd": _iso(now + 86400)})
            check("Grok absolute usage with denominator", w.left_percent == 75.0 and w.label == "Month")
            w = window_from_billing({"currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": _iso(now + 86400)},
                                     "onDemandUsed": {"val": 12}, "onDemandCap": {"val": 0}})
            check("Grok missing denominator stays N/A", w.used_percent is None and w.used_amount == 12)
            check("Grok malformed response unavailable", window_from_billing({}).unavailable_reason is not None)

            # Rendering / urgency
            def acct(name: str, left: float, stale: bool = False) -> AccountUsage:
                return AccountUsage("codex", name, windows=[
                    UsageWindow("5-hour", "5h", unavailable_reason="missing"),
                    UsageWindow("Week", "Wk", used_percent=100 - left, resets_at=now + 86400)], stale=stale)

            check("75% left => NO URGENT LIMITS", "NO URGENT LIMITS" in most_urgent([acct("A", 75)], tz, False))
            check("40% left => LOW", "LOW" in most_urgent([acct("A", 40)], tz, False))
            check("20% left => CRITICAL", "CRITICAL" in most_urgent([acct("A", 20)], tz, False))
            check("stale never MOST URGENT",
                  "B" in most_urgent([acct("A", 1, stale=True), acct("B", 30)], tz, False))
            dash = render_dashboard([acct("A", 75)], "UTC", color=False)
            check("unknown percent renders N/A, never ???", "N/A" in dash and "?" not in dash)
            colored = render_dashboard([acct("A", 75), acct("B", 10)], "UTC", color=True)
            rows = [ln for ln in strip_ansi(colored).splitlines() if ln.startswith("  Wk")]
            check("ANSI colors do not shift columns",
                  len(rows) == 2 and rows[0].index("%") == rows[1].index("%") and rows[0].index("resets") == rows[1].index("resets"))
            payload = json.loads(json.dumps(render_json_payload([acct("A", 75)])))
            check("JSON serializable with left_percent", payload["accounts"][0]["windows"][1]["left_percent"] == 75)
            check("telegram renders", "*A*" in render_telegram([acct("A", 75)], "UTC"))
            check("no accounts renders helpful message", "No supported" in render_dashboard([], "UTC"))
            check("config works without file", Config().provider_enabled("codex"))
        finally:
            if old_cache is None:
                os.environ.pop("AIUSAGE_CACHE_DIR", None)
            else:
                os.environ["AIUSAGE_CACHE_DIR"] = old_cache

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} failing check(s)")
        return 1
    print("SELF-TEST OK")
    return 0
