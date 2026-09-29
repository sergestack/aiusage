import json
import os
import time

import pytest
from conftest import load_fixture

from aiusage.config import Config
from aiusage.providers.base import AccountRef
from aiusage.providers.claude import ClaudeProvider
from aiusage.util import HttpError


def write_credentials(env, expires_ms=4_000_000_000_000):
    env.write(".claude/.credentials.json", {"claudeAiOauth": {"accessToken": "FAKE-TOKEN", "expiresAt": expires_ms}})


def query(p):
    refs = p.discover_accounts()
    assert refs, "Claude account should be discovered"
    return p.query_usage(refs[0])


def test_not_installed_no_credentials(env):
    assert ClaudeProvider(Config()).discover_accounts() == []


def test_valid_live_usage(env, monkeypatch):
    env.install("claude")
    write_credentials(env)
    monkeypatch.setattr(ClaudeProvider, "fetch_live", lambda self, token, timeout: load_fixture("claude_oauth_usage.json"))
    a = query(ClaudeProvider(Config()))
    assert a.error is None and not a.stale
    assert a.email == "claude-user@example.com" and a.plan == "pro"
    assert [w.short_label for w in a.windows] == ["5h", "Wk"]  # null opus window skipped
    assert a.windows[0].left_percent == 88 and a.windows[1].left_percent == 34
    assert a.source == "Claude OAuth usage API"


def test_live_result_reused_within_min_interval(env, monkeypatch):
    env.install("claude")
    write_credentials(env)
    calls = []

    def fetch(self, token, timeout):
        calls.append(1)
        return load_fixture("claude_oauth_usage.json")

    monkeypatch.setattr(ClaudeProvider, "fetch_live", fetch)
    query(ClaudeProvider(Config()))
    a = query(ClaudeProvider(Config()))
    assert len(calls) == 1 and not a.stale and a.windows[0].used_percent == 12


def test_429_falls_back_to_statusline_snapshot(env, monkeypatch):
    env.install("claude")
    write_credentials(env)
    env.write(".cache/aiusage/claude-statusline.json", load_fixture("claude_statusline.json"))

    def fail(self, token, timeout):
        raise HttpError(429, "HTTP 429")

    monkeypatch.setattr(ClaudeProvider, "fetch_live", fail)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    a = query(ClaudeProvider(Config()))
    assert a.error is None
    assert a.windows[0].used_percent == 20
    assert "throttled" in a.warning


def test_stale_snapshot_marked_stale(env, monkeypatch):
    env.install("claude")
    path = env.write(".cache/aiusage/claude-statusline.json", load_fixture("claude_statusline.json"))
    old = time.time() - 3600
    os.utime(path, (old, old))
    a = query(ClaudeProvider(Config()))
    assert a.stale and a.error is None and "old" in a.stale_reason


def test_expired_snapshot_is_not_current(env):
    env.install("claude")
    data = load_fixture("claude_statusline.json")
    data["rate_limits"]["five_hour"]["resets_at"] = int(time.time()) - 10
    data["rate_limits"]["seven_day"]["resets_at"] = int(time.time()) - 5
    env.write(".cache/aiusage/claude-statusline.json", data)
    a = query(ClaudeProvider(Config()))
    assert a.error and "expired" in a.error
    assert all(w.used_percent is None for w in a.windows)


def test_partially_expired_snapshot_hides_expired_window(env):
    env.install("claude")
    data = load_fixture("claude_statusline.json")
    data["rate_limits"]["five_hour"]["resets_at"] = int(time.time()) - 10
    env.write(".cache/aiusage/claude-statusline.json", data)
    a = query(ClaudeProvider(Config()))
    assert a.windows[0].expired and a.windows[1].used_percent == 50
    assert [w.short_label for w in a.visible_windows()] == ["Wk"]


def test_missing_usage_is_error_not_fabricated(env):
    env.install("claude")
    write_credentials(env, expires_ms=1000)  # expired token: no live call
    a = query(ClaudeProvider(Config()))
    assert a.error and a.windows == []


def test_malformed_live_response_falls_back(env, monkeypatch):
    env.install("claude")
    write_credentials(env)
    monkeypatch.setattr(ClaudeProvider, "fetch_live", lambda self, t, to: {"five_hour": "nope"})
    a = query(ClaudeProvider(Config()))
    assert a.error and a.windows == []


@pytest.mark.parametrize("payload", [None, {}, {"five_hour": {"utilization": 50}, "seven_day": {"utilization": 1, "resets_at": "2033-01-01T00:00:00Z"}}])
def test_parse_live_rejects_incomplete(payload):
    with pytest.raises(ValueError):
        ClaudeProvider.parse_live(payload)


def test_capture_statusline_never_overwrites_with_partial(env):
    p = ClaudeProvider(Config())
    assert p.capture_statusline(json.dumps(load_fixture("claude_statusline.json")))
    target = env.home / ".cache/aiusage/claude-statusline.json"
    before = target.read_text()
    assert not p.capture_statusline(json.dumps({"model": {}, "rate_limits": {"five_hour": {"used_percentage": 1}}}))
    assert not p.capture_statusline("not json")
    assert target.read_text() == before
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_account_ref_name_config(env):
    env.install("claude")
    write_credentials(env)
    cfg = Config()
    cfg.data["names"] = {"~/.claude": "Claude Personal"}
    assert ClaudeProvider(cfg).discover_accounts()[0].name == "Claude Personal"
    assert isinstance(ClaudeProvider(cfg).discover_accounts()[0], AccountRef)
