import json
import time

from conftest import load_fixture

from aiusage import cli
from aiusage.config import Config, load_config
from aiusage.discovery import active_providers, collect, detect_all
from aiusage.providers.claude import ClaudeProvider
from aiusage.providers.grok import GrokProvider


def codex_scenario():
    now = int(time.time())
    text = json.dumps(load_fixture("codex_5h_week.json"))
    return json.loads(text.replace('"__NOW_PLUS_2H__"', str(now + 7200)).replace('"__NOW_PLUS_5D__"', str(now + 432000)))


def test_no_providers_installed(env, capsys):
    assert active_providers(Config()) == []
    assert collect(Config()) == []
    assert cli.main(["--no-color"]) == 0
    assert "No supported AI coding agents" in capsys.readouterr().out


def test_one_provider_installed(env):
    env.install("codex")
    env.codex_home(".codex", codex_scenario())
    assert [p.id for p in active_providers(Config())] == ["codex"]
    detected = {p.id: d.installed for p, d in detect_all(Config())}
    assert detected == {"claude": False, "codex": True, "grok": False}
    accounts = collect(Config())
    assert len(accounts) == 1 and accounts[0].provider == "codex"


def test_all_providers_installed(env, monkeypatch, capsys):
    for name in ("claude", "codex", "grok"):
        env.install(name)
    env.codex_home(".codex", codex_scenario())
    env.write(".claude/.credentials.json", {"claudeAiOauth": {"accessToken": "FAKE", "expiresAt": 4_000_000_000_000}})
    env.write(".grok/auth.json", load_fixture("grok_auth.json"))
    monkeypatch.setattr(ClaudeProvider, "fetch_live", lambda self, t, to: load_fixture("claude_oauth_usage.json"))
    monkeypatch.setattr(GrokProvider, "fetch", lambda self, u, r, t, v: load_fixture("grok_credits_billing.json"))
    accounts = collect(Config())
    assert [a.provider for a in accounts] == ["claude", "codex", "grok"]

    assert cli.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [a["provider"] for a in payload["accounts"]] == ["claude", "codex", "grok"]

    assert cli.main(["--no-color"]) == 0
    out = capsys.readouterr().out
    assert "Claude Code" in out and "Codex" in out and "Grok" in out and "N/A" in out

    assert cli.main(["--telegram"]) == 0
    assert "*Grok*" in capsys.readouterr().out

    assert cli.main(["detect"]) == 0
    out = capsys.readouterr().out
    assert "distinct accounts: 1" in out and "FAKE" not in out

    assert cli.main(["diagnose", "claude"]) == 0
    out = capsys.readouterr().out
    assert "FAKE" not in out and "present" in out


def test_disabled_provider(env, tmp_path, monkeypatch):
    env.install("codex")
    env.codex_home(".codex", codex_scenario())
    cfg_path = env.write(".config/aiusage/config.toml", "[codex]\nenabled = false\n")
    assert cfg_path.exists()
    cfg = load_config()
    assert cfg.loaded and active_providers(cfg) == []


def test_invalid_config_is_ignored_with_warning(env, capsys):
    env.write(".config/aiusage/config.toml", "this is = = not toml")
    cfg = load_config()
    assert not cfg.loaded and cfg.provider_enabled("codex")
    assert "ignoring invalid config" in capsys.readouterr().err


def test_version_and_self_test(env, capsys):
    try:
        cli.main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "aiusage" in capsys.readouterr().out
    assert cli.main(["self-test"]) == 0
    assert cli.main(["--self-test"]) == 0


def test_legacy_diagnose_flag(env, capsys):
    assert cli.main(["--diagnose-grok"]) == 0
    assert "== Grok ==" in capsys.readouterr().out


def test_config_init(env, capsys):
    assert cli.main(["config", "--init"]) == 0
    assert (env.home / ".config/aiusage/config.toml").exists()
    assert load_config().loaded
