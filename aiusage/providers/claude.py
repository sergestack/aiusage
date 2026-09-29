"""Claude Code provider.

Sources, in order of preference:

1. Live: the Claude OAuth usage endpoint, called with the access token that
   Claude Code itself stores locally (the same request `/usage` makes).
2. Fallback: the newest valid snapshot of Claude Code's statusLine JSON
   (`rate_limits`) or of a previous live result, cached by aiusage.

Freshness rules:

* A fallback snapshot older than ``stale_after_seconds`` is marked STALE.
* A cached window whose reset time has passed is dropped (``expired``); it no
  longer describes the current window.
* If every cached window has expired the account is shown as unavailable.
* aiusage never launches a Claude session to "refresh" usage: a fresh
  session would report a brand-new window, not the user's real one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from aiusage import APP_NAME, __version__, cache
from aiusage.config import cache_dir
from aiusage.models import AccountUsage, UsageWindow
from aiusage.providers.base import AccountRef, Detection, Provider
from aiusage.util import (
    HttpError,
    command_version,
    compact_duration,
    expand_path,
    http_request_json,
    parse_reset,
    pretty_path,
    read_json_file,
    to_float,
    to_int,
    valid_percent,
    which,
)

OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

# (response key, long label, short label). Only windows the API actually
# returns (non-null) are shown.
KNOWN_WINDOWS = (
    ("five_hour", "5-hour", "5h"),
    ("seven_day", "Week", "Wk"),
    ("seven_day_opus", "Week (Opus)", "Opus"),
    ("seven_day_sonnet", "Week (Sonnet)", "Sonnet"),
)
REQUIRED_WINDOWS = ("five_hour", "seven_day")
STATUSLINE_CAPTURE_NAME = "claude-statusline.json"


class ClaudeProvider(Provider):
    id = "claude"
    display_name = "Claude Code"
    vendor = "Anthropic"

    # ----------------------------------------------------------- paths
    @property
    def command(self) -> str:
        return str(self.settings.get("command") or "claude")

    @property
    def claude_dir(self) -> Path:
        configured = self.settings.get("config_dir") or os.environ.get("CLAUDE_CONFIG_DIR")
        return expand_path(configured) if configured else Path.home() / ".claude"

    @property
    def credentials_file(self) -> Path:
        return self.claude_dir / ".credentials.json"

    def statusline_files(self) -> list[Path]:
        files = [expand_path(p) for p in self.settings.get("statusline_files") or []]
        files.append(cache_dir() / STATUSLINE_CAPTURE_NAME)
        # Compatibility with the pre-package "ai-usage" script layout.
        legacy = Path.home() / ".cache" / "ai-usage"
        files.append(legacy / "claude-statusline-latest.json")
        files.append(legacy / "claude-rate-limits.json")
        return files

    # ----------------------------------------------------------- detect
    def detect(self) -> Detection:
        exe = which(self.command)
        return Detection(self.id, self.display_name, exe is not None, exe)

    def _access_token(self) -> tuple[Optional[str], Optional[str]]:
        """Return (token, problem). The token is only ever used in memory for
        the usage request; it is never printed, logged or cached."""
        payload, error = read_json_file(self.credentials_file)
        if payload is None and sys.platform == "darwin" and self.settings.get("macos_keychain"):
            payload, error = self._keychain_credentials()
        if not isinstance(payload, dict):
            return None, "Claude OAuth credentials not found" if error == "missing" else "Claude OAuth credentials unreadable"
        oauth = payload.get("claudeAiOauth")
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        if not isinstance(token, str) or not token:
            return None, "Claude OAuth access token not found (API-key login has no subscription limits)"
        expires = parse_reset(oauth.get("expiresAt")) if isinstance(oauth, dict) else None
        if expires is not None and expires <= int(time.time()):
            return None, "Claude OAuth token expired; open Claude Code to refresh it"
        return token, None

    @staticmethod
    def _keychain_credentials() -> tuple[Optional[Any], Optional[str]]:  # pragma: no cover - macOS only
        try:
            cp = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if cp.returncode != 0:
                return None, "missing"
            return json.loads(cp.stdout), None
        except Exception as exc:
            return None, type(exc).__name__

    def has_credentials(self) -> bool:
        payload, _ = read_json_file(self.credentials_file)
        oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
        return isinstance(oauth, dict) and bool(oauth.get("accessToken"))

    def auth_status(self, timeout: float = 6.0) -> dict[str, Any]:
        exe = which(self.command)
        if not exe:
            return {}
        try:
            cp = subprocess.run(
                [exe, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
            data = json.loads(cp.stdout)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def discover_accounts(self) -> list[AccountRef]:
        if not self.detect().installed and not self.has_credentials():
            return []
        evidence = []
        if self.has_credentials():
            evidence.append("oauth credentials")
        if any(p.exists() for p in self.statusline_files()):
            evidence.append("statusLine snapshot")
        if not evidence:
            if sys.platform == "darwin" and self.settings.get("macos_keychain"):
                evidence.append("macOS keychain")
            elif self.auth_status().get("loggedIn"):
                evidence.append("claude auth status")
        if not evidence:
            return []
        name = self.config.display_name(self.id, "Claude Code", pretty_path(self.claude_dir))
        return [AccountRef(self.id, name, pretty_path(self.claude_dir), {"evidence": evidence})]

    # ----------------------------------------------------------- live
    def fetch_live(self, token: str, timeout: float) -> Any:
        return http_request_json(
            OAUTH_USAGE_URL,
            {
                "Authorization": f"Bearer {token}",
                "anthropic-beta": OAUTH_BETA,
                "Accept": "application/json",
                "User-Agent": f"{APP_NAME}/{__version__}",
            },
            timeout,
        )

    @staticmethod
    def parse_live(payload: Any) -> list[UsageWindow]:
        """Parse the OAuth usage response. Raises ValueError when the
        required 5-hour/weekly windows are missing or invalid."""
        if not isinstance(payload, dict):
            raise ValueError("Claude usage response malformed")
        windows: list[UsageWindow] = []
        for key, label, short in KNOWN_WINDOWS:
            raw = payload.get(key)
            if raw is None and key not in REQUIRED_WINDOWS:
                continue
            if not isinstance(raw, dict):
                raise ValueError(f"Claude usage response omitted {key}")
            used = to_float(raw.get("utilization"))
            if used is None and key not in REQUIRED_WINDOWS:
                continue
            if not valid_percent(used):
                raise ValueError(f"Claude usage response has invalid {key} utilization")
            resets = parse_reset(raw.get("resets_at"))
            # An unused rolling window has no reset until usage starts.
            if resets is None and used != 0:
                raise ValueError(f"Claude usage response omitted active {key} reset")
            windows.append(UsageWindow(label, short, used_percent=used, resets_at=resets, source="live"))
        return windows

    # ----------------------------------------------------------- snapshots
    @staticmethod
    def parse_statusline_rate_limits(rate_limits: Any) -> Optional[list[UsageWindow]]:
        """statusLine JSON ``rate_limits`` (or aiusage's legacy cache). Returns
        None unless both required windows are complete and valid."""
        if not isinstance(rate_limits, dict):
            return None
        windows: list[UsageWindow] = []
        for key, label, short in KNOWN_WINDOWS[:2]:
            raw = rate_limits.get(key)
            if not isinstance(raw, dict):
                return None
            used = to_float(raw.get("used_percentage"))
            resets = parse_reset(raw.get("resets_at"))
            if not valid_percent(used) or resets is None:
                return None
            windows.append(UsageWindow(label, short, used_percent=used, resets_at=resets))
        return windows

    def load_snapshots(self) -> list[dict[str, Any]]:
        """All valid cached snapshots, newest first. Each item has
        captured_at, windows, source, email, plan."""
        found: list[dict[str, Any]] = []
        cached = cache.load(self.id)
        if cached and isinstance(cached.get("windows"), list):
            windows = [UsageWindow.from_dict(w) for w in cached["windows"] if isinstance(w, dict)]
            captured = to_int(cached.get("captured_at"))
            if windows and captured:
                found.append(
                    {
                        "captured_at": captured,
                        "windows": windows,
                        "source": str(cached.get("source") or "aiusage cache"),
                        "email": cached.get("email"),
                        "plan": cached.get("plan"),
                    }
                )
        for path in self.statusline_files():
            payload, _error = read_json_file(path)
            if not isinstance(payload, dict):
                continue
            windows = self.parse_statusline_rate_limits(payload.get("rate_limits"))
            if not windows:
                continue
            captured = to_int(payload.get("captured_at"))
            if captured is None:
                try:
                    captured = int(path.stat().st_mtime)
                except OSError:
                    continue
            found.append(
                {
                    "captured_at": captured,
                    "windows": windows,
                    "source": payload["source"] if isinstance(payload.get("source"), str) else "Claude statusLine snapshot",
                    "path": pretty_path(path),
                    "email": payload.get("email") if isinstance(payload.get("email"), str) else None,
                    "plan": payload.get("plan") if isinstance(payload.get("plan"), str) else None,
                }
            )
        found.sort(key=lambda item: item["captured_at"], reverse=True)
        return found

    def save_snapshot(self, windows: list[UsageWindow], source: str, email: Any, plan: Any) -> None:
        try:
            cache.save(
                self.id,
                {
                    "captured_at": int(time.time()),
                    "source": source,
                    "email": email,
                    "plan": plan,
                    "windows": [w.to_dict() for w in windows],
                },
            )
        except OSError:
            pass

    # ----------------------------------------------------------- query
    def query_usage(self, ref: AccountRef) -> AccountUsage:
        now = int(time.time())
        timeout = max(1.0, min(30.0, float(self.settings.get("timeout_seconds") or 8.0)))
        stale_after = int(self.settings.get("stale_after_seconds") or 600)
        status = self.auth_status()
        email = status.get("email") or status.get("accountEmail")
        plan = status.get("subscriptionType") or status.get("plan")
        account = AccountUsage(
            provider=self.id,
            account_name=ref.name,
            email=email if isinstance(email, str) else None,
            plan=plan if isinstance(plan, str) else None,
            profile=ref.profile,
            fetched_at=now,
        )

        # The usage endpoint rate-limits aggressively; reuse a live result
        # captured moments ago instead of calling it again.
        min_interval = int(self.settings.get("min_refresh_seconds") or 60)
        snapshots = self.load_snapshots()
        if snapshots and "OAuth usage API" in str(snapshots[0]["source"]) and now - snapshots[0]["captured_at"] < min_interval:
            recent = self._from_snapshot(account, None, stale_after, now)
            if not recent.error:
                return recent

        live_problem: Optional[str] = None
        token, token_problem = self._access_token()
        if token:
            payload: Any = None
            for attempt in range(2):
                try:
                    payload = self.fetch_live(token, timeout)
                    live_problem = None
                    break
                except HttpError as exc:
                    live_problem = f"Claude live usage {exc}"
                    if exc.status != 429 or attempt:
                        break
                    time.sleep(0.75)
            if payload is not None:
                try:
                    account.windows = self.parse_live(payload)
                    account.source = "Claude OAuth usage API"
                    self.save_snapshot(account.windows, account.source, account.email, account.plan)
                    return account
                except ValueError as exc:
                    live_problem = str(exc)
        else:
            live_problem = token_problem

        return self._from_snapshot(account, live_problem, stale_after, now)

    def _from_snapshot(
        self, account: AccountUsage, live_problem: Optional[str], stale_after: int, now: int
    ) -> AccountUsage:
        snapshots = self.load_snapshots()
        if not snapshots:
            account.source = "Claude statusLine / cache"
            account.stale = True
            account.error = "no Claude usage available yet"
            account.warning = "; ".join(
                x for x in (live_problem, "see `aiusage diagnose claude` for statusLine capture setup") if x
            )
            return account

        snap = snapshots[0]
        captured = int(snap["captured_at"])
        age = max(0, now - captured)
        account.fetched_at = captured
        account.source = str(snap["source"])
        account.email = account.email or snap.get("email")
        account.plan = account.plan or snap.get("plan")
        account.stale = age > stale_after
        windows: list[UsageWindow] = []
        for window in snap["windows"]:
            if window.resets_at is not None and window.resets_at <= now:
                window.expired = True
                window.used_percent = None
                window.unavailable_reason = "cached window already reset"
            windows.append(window)
        account.windows = windows

        notes = []
        if live_problem:
            if "HTTP 429" in live_problem:
                notes.append("Claude live refresh throttled; showing last valid usage")
            else:
                notes.append(live_problem)
        if all(w.expired for w in windows):
            account.error = (
                f"Claude usage unavailable: cached rate limits expired "
                f"(snapshot {compact_duration(age)} old, all resets passed)"
            )
            notes.append("no fake refresh used; open Claude Code to update usage")
        elif any(w.expired for w in windows):
            notes.append("an expired cached window was hidden")
        account.warning = "; ".join(notes) if notes else None
        if account.stale:
            account.stale_reason = f"Claude usage snapshot is {compact_duration(age)} old"
            account.stale_action = "open Claude Code and complete one response to refresh"
        return account

    # ----------------------------------------------------------- capture
    def capture_statusline(self, raw: str) -> bool:
        """Store a statusLine payload's rate limits. Invalid or partial payloads
        never overwrite an existing valid snapshot."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict) or not self.parse_statusline_rate_limits(payload.get("rate_limits")):
            return False
        from aiusage.util import write_json_secure

        write_json_secure(
            cache_dir() / STATUSLINE_CAPTURE_NAME,
            {
                "captured_at": int(time.time()),
                "rate_limits": payload["rate_limits"],
                "version": payload.get("version") if isinstance(payload.get("version"), str) else None,
            },
        )
        return True

    # ----------------------------------------------------------- diagnose
    def diagnose(self) -> list[str]:
        det = self.detect()
        lines = [
            f"command: {self.command}",
            f"executable: {pretty_path(det.executable) if det.executable else 'not found'}",
            f"version: {command_version(det.executable) if det.executable else 'n/a'}",
            f"config dir: {pretty_path(self.claude_dir)}",
            f"oauth credentials: {'present' if self.has_credentials() else 'not found'} (token never printed)",
        ]
        status = self.auth_status()
        lines.append(f"auth status: {'logged in' if status.get('loggedIn') else 'not logged in / unavailable'}")
        if status.get("authMethod"):
            lines.append(f"auth method: {status.get('authMethod')}")
        token, problem = self._access_token()
        if token:
            try:
                windows = self.parse_live(self.fetch_live(token, 8.0))
                summary = ", ".join(f"{w.display_label} used={w.used_percent:g}%" for w in windows if w.used_percent is not None)
                lines.append(f"live usage API: ok ({summary})")
            except (HttpError, ValueError) as exc:
                lines.append(f"live usage API: {exc}")
        else:
            lines.append(f"live usage API: skipped ({problem})")
        now = int(time.time())
        for path in self.statusline_files():
            if path.exists():
                age = compact_duration(now - path.stat().st_mtime)
                lines.append(f"statusLine snapshot: {pretty_path(path)} ({age} old)")
        snaps = self.load_snapshots()
        if snaps:
            lines.append(f"newest valid snapshot: {snaps[0]['source']}, {compact_duration(now - snaps[0]['captured_at'])} old")
        else:
            lines.append("newest valid snapshot: none")
            lines.append(
                "tip: pipe Claude Code's statusLine JSON into `aiusage capture-claude` to keep a fallback snapshot"
            )
        return lines
