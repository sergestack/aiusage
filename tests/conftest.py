"""Shared fixtures. Every test runs against an isolated fake HOME and PATH, so
no real credentials, caches or CLIs on the developer's machine are touched."""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

FAKE_CODEX = textwrap.dedent(
    """\
    #!{python}
    # Fake `codex app-server`: answers JSON-RPC from $CODEX_HOME/fake-scenario.json
    import json, os, sys
    if sys.argv[1:2] == ["--version"]:
        print("codex-cli 0.0.0-test"); sys.exit(0)
    scenario = json.load(open(os.path.join(os.environ["CODEX_HOME"], "fake-scenario.json")))
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            send({{"id": mid, "result": {{"codexHome": os.environ["CODEX_HOME"]}}}})
        elif method == "account/read":
            send({{"id": mid, "result": {{"account": scenario.get("account")}}}})
        elif method == "account/rateLimits/read":
            if scenario.get("error"):
                send({{"id": mid, "error": {{"code": -32603, "message": scenario["error"]}}}})
            else:
                send({{"id": mid, "result": scenario["limits"]}})
                for update in scenario.get("updates", []):
                    send({{"method": "account/rateLimits/updated", "params": update}})
    """
)

FAKE_SIMPLE = textwrap.dedent(
    """\
    #!{python}
    import json, sys
    if sys.argv[1:2] == ["--version"]:
        print("{name} 1.0.0-test"); sys.exit(0)
    if sys.argv[1:3] == ["auth", "status"]:
        print(json.dumps({{"loggedIn": True, "email": "claude-user@example.com", "subscriptionType": "pro"}}))
    """
)


class Env:
    def __init__(self, home: Path, bin_dir: Path) -> None:
        self.home = home
        self.bin = bin_dir

    def install(self, name: str) -> Path:
        path = self.bin / name
        if name == "codex":
            body = FAKE_CODEX.format(python=sys.executable)
        else:
            body = FAKE_SIMPLE.format(python=sys.executable, name=name)
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def codex_home(self, dirname: str, scenario: dict) -> Path:
        home = self.home / dirname
        home.mkdir(parents=True, exist_ok=True)
        (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "FAKE"}}))
        (home / "fake-scenario.json").write_text(json.dumps(scenario))
        return home

    def write(self, rel: str, data) -> Path:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    for var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "GROK_HOME", "AIUSAGE_CONFIG", "AIUSAGE_CACHE_DIR", "AIUSAGE_TZ"):
        monkeypatch.delenv(var, raising=False)
    os.environ.pop("NO_COLOR", None)
    return Env(home, bin_dir)


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())
