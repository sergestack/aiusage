import time

from conftest import load_fixture

from aiusage import cache
from aiusage.config import Config
from aiusage.models import UsageWindow
from aiusage.providers.grok import GrokProvider, window_from_billing
from aiusage.util import HttpError


def setup(env):
    env.install("grok")
    env.write(".grok/auth.json", load_fixture("grok_auth.json"))


def query(p):
    refs = p.discover_accounts()
    assert refs
    return p.query_usage(refs[0])


def test_absolute_usage_without_denominator():
    w = window_from_billing(load_fixture("grok_credits_billing.json")["config"])
    assert w.label == "Week" and w.short_label == "Wk"
    assert w.used_percent is None and w.used_amount == 12 and w.unit == "credits"
    assert "quota not exposed" in w.unavailable_reason


def test_percent_exposed():
    w = window_from_billing(load_fixture("grok_credits_percent.json")["config"])
    assert w.used_percent == 9 and w.left_percent == 91


def test_legacy_billing_with_denominator():
    w = window_from_billing(load_fixture("grok_legacy_billing.json")["config"])
    assert w.label == "Month" and w.used_percent == 25 and w.remaining_amount == 75


def test_zero_monthly_limit_is_not_unlimited():
    w = window_from_billing({"monthlyLimit": {"val": 0}, "used": {"val": 3}})
    assert w.used_percent is None and "monthlyLimit=0" in w.unavailable_reason


def test_malformed():
    w = window_from_billing({})
    assert w.used_percent is None and w.unavailable_reason


def test_not_installed(env):
    env.write(".grok/auth.json", load_fixture("grok_auth.json"))
    assert GrokProvider(Config()).discover_accounts() == []


def test_installed_without_auth_not_listed(env):
    env.install("grok")
    assert GrokProvider(Config()).discover_accounts() == []


def test_live_query(env, monkeypatch):
    setup(env)
    monkeypatch.setattr(GrokProvider, "fetch", lambda self, url, rec, t, v: load_fixture("grok_credits_percent.json"))
    a = query(GrokProvider(Config()))
    assert a.email == "grok-user@example.com" and a.error is None and not a.stale
    assert a.windows[0].left_percent == 91
    assert cache.load("grok")["windows"][0]["used_percent"] == 9


def test_live_failure_uses_stale_cache(env, monkeypatch):
    setup(env)
    future = int(time.time()) + 86400
    cache.save("grok", {"fetched_at": int(time.time()) - 7200,
                        "windows": [UsageWindow("Week", "Wk", used_percent=30, resets_at=future).to_dict()]})

    def fail(self, url, rec, t, v):
        raise HttpError(503, "HTTP 503")

    monkeypatch.setattr(GrokProvider, "fetch", fail)
    a = query(GrokProvider(Config()))
    assert a.stale and a.error is None and a.windows[0].used_percent == 30
    assert "503" in a.warning


def test_stale_cache_past_reset_is_unavailable(env, monkeypatch):
    setup(env)
    cache.save("grok", {"fetched_at": int(time.time()) - 9 * 86400,
                        "windows": [UsageWindow("Week", "Wk", used_percent=30, resets_at=int(time.time()) - 60).to_dict()]})

    def fail(self, url, rec, t, v):
        raise HttpError(401, "HTTP 401")

    monkeypatch.setattr(GrokProvider, "fetch", fail)
    a = query(GrokProvider(Config(data={**Config().data, "grok": {"refresh_expired_auth": False}})))
    assert a.error and "unavailable" in a.error
    assert all(w.used_percent is None for w in a.windows)


def test_expired_live_reset_marked_stale(env, monkeypatch):
    setup(env)
    payload = {"config": {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": "2001-01-01T00:00:00Z"},
                          "creditUsagePercent": 50}}
    monkeypatch.setattr(GrokProvider, "fetch", lambda self, url, rec, t, v: payload)
    a = query(GrokProvider(Config()))
    assert a.stale and a.windows[0].expired and a.windows[0].used_percent is None
