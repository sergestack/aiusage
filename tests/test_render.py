import json
import time

from aiusage.models import AccountUsage, UsageWindow, classify_left, urgency_candidates
from aiusage.renderer import (
    fmt_reset,
    most_urgent,
    render_dashboard,
    render_json_payload,
    render_telegram,
    resolve_tz,
    strip_ansi,
)

NOW = int(time.time())
TZ = resolve_tz("UTC")


def account(name, *windows, provider="codex", **kw):
    return AccountUsage(provider, name, email=f"{name.lower().replace(' ', '')}@example.com",
                        windows=list(windows), **kw)


def week(left, **kw):
    return UsageWindow("Week", "Wk", used_percent=100 - left, resets_at=NOW + 3 * 86400, **kw)


def five(left):
    return UsageWindow("5-hour", "5h", used_percent=100 - left, resets_at=NOW + 3600)


def test_percent_used_vs_left():
    w = UsageWindow("Week", used_percent=30)
    assert w.left_percent == 70
    assert UsageWindow("x", used_percent=120).left_percent == 0
    assert UsageWindow("x").left_percent is None


def test_thresholds():
    assert classify_left(41) == "ok"
    assert classify_left(40) == "low" and classify_left(21) == "low"
    assert classify_left(20) == "critical" and classify_left(0) == "critical"
    assert classify_left(None) == "unknown"


def test_most_urgent_picks_lowest_available():
    accts = [account("A", five(80), week(35)), account("B", five(15), week(90))]
    text = most_urgent(accts, TZ, False)
    assert text.startswith("MOST URGENT: B · 5-hour · 15% LEFT · CRITICAL")


def test_most_urgent_low_and_manual_reset():
    text = most_urgent([account("A", week(40), manual_resets=1)], TZ, False)
    assert "LOW" in text and "MANUAL RESET AVAILABLE" in text


def test_no_urgent_limits_with_lowest_available():
    text = most_urgent([account("A", week(75))], TZ, False)
    assert "NO URGENT LIMITS" in text and "Lowest available: A · Week · 75% left" in text


def test_unavailable_stale_errored_excluded_from_urgent():
    accts = [
        account("Stale", week(1), stale=True),
        account("Err", week(1), error="boom"),
        account("Unavail", UsageWindow("5-hour", "5h", unavailable_reason="missing"), week(60)),
        account("Expired", UsageWindow("Week", "Wk", used_percent=99, resets_at=NOW + 10, expired=True)),
    ]
    assert [a.account_name for _, a, _ in urgency_candidates(accts)] == ["Unavail"]
    assert "NO URGENT LIMITS" in most_urgent(accts, TZ, False)
    assert most_urgent([], TZ, False).endswith("NO URGENT LIMITS")


def test_ansi_does_not_break_alignment():
    accts = [
        account("Alpha", five(95), week(35)),
        account("Longer Name", UsageWindow("Month", "Mo", used_percent=90, resets_at=NOW + 20 * 86400), week(5)),
        account("NoPct", UsageWindow("Week", "Wk", used_amount=3, unit="credits", resets_at=NOW + 86400)),
    ]
    plain = render_dashboard(accts, "UTC", color=False, now=NOW)
    colored = render_dashboard(accts, "UTC", color=True, now=NOW)
    assert "\x1b[" in colored
    assert strip_ansi(colored) == plain
    rows = [line for line in plain.splitlines() if line.startswith("  ") and ("█" in line or "░" in line)]
    assert len(rows) == 5
    assert len({r.index("░") if "█" not in r else r.index("█") for r in rows}) == 1  # bars start together
    assert len({r.index("%") if "%" in r else r.index("N/A") + 2 for r in rows}) == 1  # percents end together
    reset_cols = {r.index("resets") for r in rows if "resets" in r}
    assert len(reset_cols) == 1


def test_unknown_percentage_is_na_never_question_marks():
    acct = account("A", UsageWindow("Week", "Wk", used_amount=0, unit="credits", resets_at=NOW + 86400,
                                    unavailable_reason="total quota not exposed"))
    out = render_dashboard([acct], "UTC", now=NOW)
    assert "░" * 20 + "   N/A" in out
    assert "?" not in out
    assert "0 credits used · quota not exposed" in out


def test_stale_rendering_is_neutral_and_labeled():
    out = render_dashboard([account("A", week(5), stale=True, stale_reason="snapshot is 2h old")], "UTC", color=True)
    assert "STALE" in out and "stale snapshot" in out
    assert "\x1b[31;1m" not in out  # stale values never shown as critical red


def test_error_account_rendering_and_other_providers_unaffected():
    accts = [account("Good", week(80)), account("Bad", error="Grok billing HTTP 401", provider="grok")]
    out = render_dashboard(accts, "UTC")
    assert "✗ Grok billing HTTP 401" in out and "Good" in out


def test_no_providers_installed_message():
    assert "No supported AI coding agents" in render_dashboard([], "UTC")


def test_json_serialization_multiple_accounts():
    accts = [account("A", five(50), week(75), manual_resets=2), account("B", week(10), provider="grok")]
    payload = json.loads(json.dumps(render_json_payload(accts, "UTC")))
    assert len(payload["accounts"]) == 2
    a = payload["accounts"][0]
    assert a["windows"][0]["left_percent"] == 50 and a["windows"][0]["used_percent"] == 50
    assert a["manual_resets"] == 2
    assert "diagnostics" not in a


def test_telegram():
    out = render_telegram([account("A", five(10), week(35)), account("B", error="x")], "UTC")
    assert "*A*" in out and "*⚠️ 10%*" in out and "_35%_" in out and "*B* ✗ x" in out


def test_reset_text():
    assert fmt_reset(None, TZ) == ("reset unavailable", "")
    text, countdown = fmt_reset(NOW + 3 * 86400, TZ, now=NOW)
    assert text.startswith("resets ") and countdown == "in 3d"
    assert fmt_reset(NOW - 5, TZ, now=NOW)[0].startswith("reset passed")
