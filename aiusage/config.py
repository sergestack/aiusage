"""Configuration: optional TOML file + environment overrides.

aiusage works with no config file at all. The file only exists to rename
accounts, disable providers, add Codex homes, override paths and tune
freshness thresholds.
"""

from __future__ import annotations

import copy
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from aiusage import APP_NAME
from aiusage.util import expand_path

try:  # Python 3.11+
    import tomllib as _toml  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.9/3.10
    try:
        import tomli as _toml  # type: ignore[import-not-found,no-redef]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]


DEFAULTS: dict[str, Any] = {
    "timezone": None,  # None = system local timezone
    "claude": {
        "enabled": True,
        "command": "claude",
        "config_dir": None,  # default: $CLAUDE_CONFIG_DIR or ~/.claude
        "stale_after_seconds": 600,
        "timeout_seconds": 8.0,
        "min_refresh_seconds": 60,  # reuse a live result younger than this
        # Extra statusLine JSON snapshots to read (see README).
        "statusline_files": [],
        # macOS stores Claude Code credentials in the login keychain. Reading
        # it may show a system prompt, so it is opt-in.
        "macos_keychain": False,
    },
    "codex": {
        "enabled": True,
        "command": "codex",
        "auto_discover": True,  # scan $CODEX_HOME, ~/.codex and ~/.codex-*
        "homes": [],  # additional CODEX_HOME directories
        "exclude_homes": [],
        "timeout_seconds": 12.0,
        "notification_wait_seconds": 4.0,
        "session_log_max_age_days": 8,
    },
    "grok": {
        "enabled": True,
        "command": "grok",
        "home": None,  # default: $GROK_HOME or ~/.grok
        # Grok freshness: a live result is current; a cached fallback (live
        # call failed) is always shown STALE and hidden once its reset passes.
        "timeout_seconds": 8.0,
        # When the stored Grok session token is expired, refresh it with the
        # stored OIDC refresh token (the same thing the Grok CLI does) and
        # write it back atomically. Set false to never touch Grok's auth file.
        "refresh_expired_auth": True,
    },
    # Display names. Keys may be an email, a profile path (e.g.
    # "~/.codex-work"), or a default account name ("Codex (work)").
    "names": {},
}


def xdg_dir(env_var: str, fallback: str) -> Path:
    value = os.environ.get(env_var)
    base = Path(value) if value and Path(value).is_absolute() else Path.home() / fallback
    return base / APP_NAME


def config_dir() -> Path:
    return xdg_dir("XDG_CONFIG_HOME", ".config")


def cache_dir() -> Path:
    override = os.environ.get("AIUSAGE_CACHE_DIR")
    if override:
        return expand_path(override)
    return xdg_dir("XDG_CACHE_HOME", ".cache")


def config_file() -> Path:
    override = os.environ.get("AIUSAGE_CONFIG")
    if override:
        return expand_path(override)
    return config_dir() / "config.toml"


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    path: Optional[Path] = None
    loaded: bool = False
    warnings: list[str] = field(default_factory=list)

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name)
        return value if isinstance(value, dict) else {}

    def provider_enabled(self, name: str) -> bool:
        return bool(self.section(name).get("enabled", True))

    @property
    def timezone(self) -> Optional[str]:
        value = self.data.get("timezone")
        return str(value) if value else None

    def display_name(self, provider: str, default: str, *keys: Optional[str]) -> str:
        """Configured display name. A provider table (``[names.codex]``) wins
        over global ``[names]`` keys, so one email used with several
        providers can be named per provider."""
        names = self.data.get("names")
        if not isinstance(names, dict):
            return default
        scoped = names.get(provider)
        for table in (scoped if isinstance(scoped, dict) else {}, names):
            normalized = {self._norm(k): v for k, v in table.items() if isinstance(v, str) and v}
            for key in (*keys, default):
                if key and self._norm(key) in normalized:
                    return normalized[self._norm(key)]
        return default

    @staticmethod
    def _norm(key: str) -> str:
        text = str(key).strip()
        if text.startswith("~") or text.startswith("/") or text.startswith("$"):
            return str(expand_path(text)).rstrip("/")
        return text.lower()


def load_config(path: Optional[Path] = None) -> Config:
    path = path or config_file()
    cfg = Config(path=path)
    if path.exists():
        if _toml is None:
            cfg.warnings.append(f"cannot read {path}: install 'tomli' on Python < 3.11")
        else:
            try:
                with path.open("rb") as fh:
                    user = _toml.load(fh)
                cfg.data = deep_merge(DEFAULTS, user)
                cfg.loaded = True
            except Exception as exc:
                cfg.warnings.append(f"ignoring invalid config {path}: {exc}")

    tz = os.environ.get("AIUSAGE_TZ")
    if tz:
        cfg.data["timezone"] = tz
    for warning in cfg.warnings:
        print(f"aiusage: warning: {warning}", file=sys.stderr)
    return cfg


EXAMPLE_CONFIG = """\
# aiusage configuration (optional). Location:
#   $XDG_CONFIG_HOME/aiusage/config.toml  or  ~/.config/aiusage/config.toml

# timezone = "Europe/Berlin"     # default: system local timezone

[claude]
# enabled = true
# command = "claude"
# config_dir = "~/.claude"
# stale_after_seconds = 600
# statusline_files = ["~/.cache/my-statusline/latest.json"]

[codex]
# enabled = true
# command = "codex"
# auto_discover = true          # $CODEX_HOME, ~/.codex, ~/.codex-*
# homes = ["~/work/codex-home"]
# exclude_homes = ["~/.codex-old"]

[grok]
# enabled = true
# home = "~/.grok"
# refresh_expired_auth = true

[names]
# "~/.codex-work" = "Codex Work"   # key: profile path, email or default name
# [names.codex]                    # per-provider table wins over [names]
# "you@example.com" = "Codex Personal"
"""
