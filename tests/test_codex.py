import json
import time

from conftest import load_fixture

from aiusage.config import DEFAULTS, Config, deep_merge
from aiusage.providers.codex import CodexProvider, build_windows, reset_credits


def scenario(name, now=None):
    now = int(time.time()) if now is None else now
    text = json.dumps(load_fixture(name))
    text = text.replace('"__NOW_PLUS_2H__"', str(now + 2 * 3600)).replace('"__NOW_PLUS_5D__"', str(now + 5 * 86400))
    return json.loads(text)


def provider(extra=None):
    data = deep_merge(DEFAULTS, {"codex": {"notification_wait_seconds": 0.2, "timeout_seconds": 10}})
    if extra:
        data = deep_merge(data, extra)
    return CodexProvider(Config(data=data))


def test_not_installed_means_no_accounts(env):
    env.codex_home(".codex", scenario("codex_5h_week.json"))
    assert provider().discover_accounts() == []
    assert provider().query_all() == []


def test_single_profile_5h_and_weekly(env):
    env.install("codex")
    env.codex_home(".codex", scenario("codex_5h_week.json"))
    accounts = provider().query_all()
    assert len(accounts) == 1
    a = accounts[0]
    assert a.account_name == "Codex"
    assert a.email == "codex-a@example.com" and a.plan == "plus"
    assert [w.short_label for w in a.windows] == ["5h", "Wk"]
    assert a.windows[0].left_percent == 70 and a.windows[1].left_percent == 55
    assert a.manual_resets == 2 and a.error is None


def test_missing_5h_is_unavailable_not_copied(env):
    env.install("codex")
    env.codex_home(".codex", scenario("codex_week_only.json"))
    a = provider().query_all()[0]
    assert a.windows[0].used_percent is None
    assert a.windows[0].unavailable_reason
    assert a.windows[1].used_percent == 15


def test_multiple_profiles_discovered_without_fixed_names(env):
    env.install("codex")
    env.codex_home(".codex", scenario("codex_5h_week.json"))
    env.codex_home(".codex-work", scenario("codex_week_only.json"))
    env.codex_home(".codex-empty-dir-without-auth", {})
    (env.home / ".codex-empty-dir-without-auth" / "auth.json").unlink()
    (env.home / ".codex-empty-dir-without-auth" / "fake-scenario.json").unlink()
    accounts = provider().query_all()
    assert [a.account_name for a in accounts] == ["Codex", "Codex (work)"]
    assert {a.email for a in accounts} == {"codex-a@example.com", "codex-b@example.com"}


def test_same_account_in_two_homes_is_deduplicated(env):
    env.install("codex")
    env.codex_home(".codex-broken", scenario("codex_error.json"))
    env.codex_home(".codex-ok", scenario("codex_5h_week.json"))
    accounts = provider().query_all()
    assert len(accounts) == 1
    assert accounts[0].error is None
    assert accounts[0].profile == "~/.codex-ok"


def test_logged_out_profile_excluded(env):
    env.install("codex")
    env.codex_home(".codex", scenario("codex_logged_out.json"))
    env.codex_home(".codex-work", scenario("codex_week_only.json"))
    accounts = provider().query_all()
    assert [a.email for a in accounts] == ["codex-b@example.com"]


def test_codex_home_env_and_config_homes_and_rename(env, monkeypatch):
    env.install("codex")
    custom = env.codex_home("elsewhere/codexhome", scenario("codex_5h_week.json"))
    extra = env.codex_home("more/second", scenario("codex_week_only.json"))
    monkeypatch.setenv("CODEX_HOME", str(custom))
    p = provider({"codex": {"homes": [str(extra)]}, "names": {"codex-b@example.com": "Work"}})
    names = sorted(a.account_name for a in p.query_all())
    assert names == ["Codex (codexhome)", "Work"]


def test_exclude_homes(env):
    env.install("codex")
    env.codex_home(".codex", scenario("codex_5h_week.json"))
    env.codex_home(".codex-old", scenario("codex_week_only.json"))
    p = provider({"codex": {"exclude_homes": ["~/.codex-old"]}})
    assert [r.profile for r in p.discover_accounts()] == ["~/.codex"]


def test_error_only_profile_surfaces_error(env):
    env.install("codex")
    env.codex_home(".codex", scenario("codex_error.json"))
    accounts = provider().query_all()
    assert len(accounts) == 1 and "401" in accounts[0].error


def test_update_notification_fills_missing_5h(env):
    env.install("codex")
    now = int(time.time())
    sc = scenario("codex_week_only.json", now)
    sc["updates"] = [{"rateLimits": {"limitId": "codex",
                                     "primary": {"usedPercent": 40, "windowDurationMins": 300, "resetsAt": now + 3600}}}]
    env.codex_home(".codex", sc)
    a = provider({"codex": {"notification_wait_seconds": 1.0}}).query_all()[0]
    assert a.windows[0].used_percent == 40
    assert a.windows[1].used_percent == 15


def test_session_log_only_fills_gaps(env):
    env.install("codex")
    now = int(time.time())
    home = env.codex_home(".codex", scenario("codex_5h_week.json", now))
    log = {"timestamp": "2033-01-01T00:00:00Z", "payload": {"rate_limits": {
        "primary": {"used_percent": 1, "window_minutes": 300, "resets_at": now + 3600}}}}
    env.write(".codex/sessions/2033/rollout.jsonl", json.dumps(log) + "\n")
    assert home.exists()
    a = provider().query_all()[0]
    assert a.windows[0].used_percent == 30  # live value wins over session log


def test_build_windows_rules():
    now = 1_000_000
    week = {"usedPercent": 10, "resetsAt": now + 5 * 86400, "windowDurationMins": 10080}
    windows, _, raw = build_windows({"primary": week, "secondary": None}, now)
    assert windows[0].used_percent is None and windows[1].used_percent == 10
    assert raw["weeklySource"] == "primary"
    # swapped slots are classified by duration, not position
    five = {"usedPercent": 5, "resetsAt": now + 3600, "windowDurationMins": 300}
    windows, _, _ = build_windows({"primary": week, "secondary": five}, now)
    assert windows[0].used_percent == 5 and windows[1].used_percent == 10
    # identical values are not duplicated
    same = {"usedPercent": 10, "resetsAt": now + 3600, "windowDurationMins": 300}
    windows, warnings, _ = build_windows({"primary": same, "secondary": dict(same, windowDurationMins=10080)}, now)
    assert windows[0].used_percent is None and windows[0].unavailable_reason == "duplicate of weekly window"
    assert windows[1].used_percent == 10 and warnings
    # expired reset is not current
    windows, _, _ = build_windows({"primary": dict(week, resetsAt=now - 1)}, now)
    assert windows[1].used_percent is None
    # malformed
    windows, warnings, _ = build_windows({"primary": {"usedPercent": "x", "windowDurationMins": 10080}}, now)
    assert windows[1].used_percent is None and warnings
    # unknown durations are shown generically
    windows, _, _ = build_windows({"primary": {"usedPercent": 3, "resetsAt": now + 86400, "windowDurationMins": 1440}}, now)
    assert windows[2].short_label == "1d"


def test_reset_credits():
    assert reset_credits({"rateLimitResetCredits": {"availableCount": 3}}) == 3
    assert reset_credits({"rateLimitResetCredits": {"availableCount": -1}}) is None
    assert reset_credits({}) is None


def test_provider_scoped_names_do_not_leak_across_providers(env):
    cfg = Config(data=deep_merge(DEFAULTS, {"names": {"codex": {"shared@example.com": "Codex Main"},
                                                       "~/.grok": "Grok Personal"}}))
    assert cfg.display_name("codex", "Codex", "shared@example.com") == "Codex Main"
    assert cfg.display_name("grok", "Grok", "shared@example.com", "~/.grok") == "Grok Personal"
    assert cfg.display_name("claude", "Claude Code", "shared@example.com") == "Claude Code"


def test_numeric_settings_honor_zero_and_reject_junk(env):
    p = provider({"codex": {"notification_wait_seconds": 0, "timeout_seconds": "abc", "session_log_max_age_days": -5}})
    assert p.number("notification_wait_seconds", 4.0, 0.0, 60.0) == 0.0
    assert p.number("timeout_seconds", 12.0, 1.0, 120.0) == 12.0
    assert p.number("session_log_max_age_days", 8, 0.0, 365.0) == 0.0
    assert p.number("missing", 3.0) == 3.0


def test_zero_notification_wait_does_not_wait(env):
    env.install("codex")
    env.codex_home(".codex", scenario("codex_week_only.json"))
    started = time.monotonic()
    accounts = provider({"codex": {"notification_wait_seconds": 0}}).query_all()
    assert accounts[0].windows[1].used_percent == 15
    assert time.monotonic() - started < 3


def test_slow_duplicate_profile_does_not_delay_dashboard(env):
    env.install("codex")
    env.codex_home(".codex", scenario("codex_5h_week.json"))
    slow = scenario("codex_error.json")
    slow["limits_delay"] = 6  # read by the fake only if rate limits are requested
    env.codex_home(".codex-zz-slow-duplicate", slow)
    started = time.monotonic()
    accounts = provider().query_all()
    assert time.monotonic() - started < 4
    assert len(accounts) == 1 and accounts[0].error is None
