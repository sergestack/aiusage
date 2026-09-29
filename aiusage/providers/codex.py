"""OpenAI Codex provider.

Usage comes from the official local ``codex app-server`` JSON-RPC interface
(``account/read`` + ``account/rateLimits/read``), run once per ``CODEX_HOME``.

Multiple accounts: every Codex profile is a separate ``CODEX_HOME``
directory. aiusage discovers ``$CODEX_HOME``, ``~/.codex``, ``~/.codex-*`` and
any configured homes, asks each one which account it is logged into, and
de-duplicates homes that are logged into the same account. Profiles that
cannot be positively identified are not shown as accounts.

Windows are classified by their reported duration, never by slot position:
Codex has moved the 5-hour and weekly windows between ``primary`` and
``secondary`` before, and has temporarily removed the 5-hour window.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from aiusage import __version__
from aiusage.models import AccountUsage, UsageWindow
from aiusage.providers.base import AccountRef, Detection, Provider
from aiusage.util import (
    child_env,
    command_version,
    compact_duration,
    expand_path,
    pretty_path,
    to_float,
    to_int,
    valid_percent,
    which,
)

FIVE_HOUR_MINS = (4 * 60, 6 * 60)
WEEK_MINS = (6 * 24 * 60, 8 * 24 * 60)
FIVE_HOUR_MISSING = "Codex app-server did not expose a 5-hour window"
FIVE_HOUR_MISSING_DISPLAY = "temporarily unavailable — not currently exposed by Codex"


# ------------------------------------------------------------------ JSON-RPC
class AppServer:
    """Minimal line-delimited JSON-RPC client for ``codex app-server``.

    A reader thread feeds a queue so this works without ``select`` (which
    does not support pipes on Windows)."""

    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )
        self.messages: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self.messages.put(msg)
        self.messages.put(None)  # EOF

    def send(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def next_message(self, timeout: float) -> Optional[dict[str, Any]]:
        try:
            return self.messages.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def request(self, method: str, req_id: int, timeout: float, params: Any = None,
                on_notification: Any = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"method": method, "id": req_id}
        if params is not None:
            payload["params"] = params
        self.send(payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self.next_message(deadline - time.monotonic())
            if msg is None:
                if self.proc.poll() is not None:
                    raise RuntimeError("codex app-server exited unexpectedly")
                continue
            if msg.get("id") == req_id:
                if "error" in msg:
                    err = msg["error"]
                    text = err.get("message") if isinstance(err, dict) else str(err)
                    raise RuntimeError(" ".join(str(text).split())[:300])
                result = msg.get("result")
                return result if isinstance(result, dict) else {}
            if on_notification is not None:
                on_notification(msg)
        raise TimeoutError(f"timed out waiting for {method}")

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# ------------------------------------------------------------------ parsing
def window_duration(window: Any) -> Optional[int]:
    return to_int(window.get("windowDurationMins")) if isinstance(window, dict) else None


def is_five_hour(window: Any) -> bool:
    d = window_duration(window)
    return d is not None and FIVE_HOUR_MINS[0] <= d <= FIVE_HOUR_MINS[1]


def is_week(window: Any) -> bool:
    d = window_duration(window)
    return d is not None and WEEK_MINS[0] <= d <= WEEK_MINS[1]


def duration_label(minutes: int) -> tuple[str, str]:
    if minutes % 1440 == 0:
        return f"{minutes // 1440}-day", f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour", f"{minutes // 60}h"
    return f"{minutes}-minute", f"{minutes}m"


def select_rate_limits(result: dict[str, Any]) -> dict[str, Any]:
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
        return by_id["codex"]
    rate_limits = result.get("rateLimits")
    if isinstance(rate_limits, dict):
        return rate_limits
    raise ValueError("Codex returned no rate-limit data")


def reset_credits(result: dict[str, Any]) -> Optional[int]:
    credits = result.get("rateLimitResetCredits")
    if not isinstance(credits, dict):
        return None
    count = to_int(credits.get("availableCount"))
    return count if count is not None and count >= 0 else None


def tui_to_app_window(window: Any) -> Optional[dict[str, Any]]:
    if not isinstance(window, dict):
        return None
    return {
        "windowDurationMins": window.get("window_minutes"),
        "resetsAt": window.get("resets_at"),
        "usedPercent": window.get("used_percent"),
    }


def latest_session_log_windows(codex_home: Path, now: int, max_age_days: float) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """Rate-limit windows from the newest Codex TUI session log event.

    Only used to fill a window the app-server did not return. Slots whose
    reset already passed are dropped individually."""
    evidence: dict[str, Any] = {"checked": False, "latest": None}
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return [], evidence
    evidence["checked"] = True
    cutoff = time.time() - max_age_days * 86400
    latest: Optional[tuple[str, dict[str, Any]]] = None
    for path in sessions.glob("**/*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            with path.open(errors="replace") as fh:
                for line in fh:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = obj.get("payload") if isinstance(obj, dict) else None
                    rate_limits = payload.get("rate_limits") if isinstance(payload, dict) else None
                    stamp = str(obj.get("timestamp") or "")
                    if isinstance(rate_limits, dict) and (latest is None or stamp > latest[0]):
                        latest = (stamp, rate_limits)
        except OSError:
            continue
    if latest is None:
        return [], evidence
    stamp, rate_limits = latest
    evidence["latest"] = {"timestamp": stamp}
    windows: list[tuple[str, dict[str, Any]]] = []
    for slot in ("primary", "secondary"):
        window = tui_to_app_window(rate_limits.get(slot))
        if not window:
            continue
        resets = to_int(window.get("resetsAt"))
        if resets is not None and resets <= now:
            evidence["latest"].setdefault("droppedExpiredSlots", []).append(slot)
            continue
        windows.append((f"session log {stamp}".strip(), window))
    return windows, evidence


def build_windows(
    rate_limits: dict[str, Any],
    now: int,
    extra_candidates: Optional[list[tuple[str, dict[str, Any]]]] = None,
) -> tuple[list[UsageWindow], list[str], dict[str, Any]]:
    """Turn a Codex rateLimits object into display windows.

    Returns (windows, warnings, raw_fields). Never copies one window into
    another; a missing 5h window is reported as unavailable."""
    candidates: list[tuple[str, dict[str, Any]]] = []
    for slot in ("primary", "secondary"):
        if isinstance(rate_limits.get(slot), dict):
            candidates.append((slot, rate_limits[slot]))
    candidates.extend(extra_candidates or [])

    five_match = next(((s, w) for s, w in candidates if is_five_hour(w)), None)
    week_match = next(((s, w) for s, w in candidates if is_week(w)), None)
    warnings: list[str] = []

    five = UsageWindow("5-hour", "5h", unavailable_reason=FIVE_HOUR_MISSING,
                       unavailable_display=FIVE_HOUR_MISSING_DISPLAY)
    week = UsageWindow("Week", "Wk", unavailable_reason="not returned by Codex app-server")

    def fill(match: tuple[str, dict[str, Any]], target: UsageWindow, name: str) -> UsageWindow:
        source, raw = match
        used = to_float(raw.get("usedPercent"))
        resets = to_int(raw.get("resetsAt"))
        if not valid_percent(used) or (resets is not None and resets <= 0):
            warnings.append(f"Codex {name} window was incomplete or invalid")
            return UsageWindow(target.label, target.short_label, unavailable_reason=f"incomplete Codex {name} window")
        if resets is not None and resets <= now:
            return UsageWindow(target.label, target.short_label, unavailable_reason=f"Codex {name} reset already passed")
        return UsageWindow(target.label, target.short_label, used_percent=used, resets_at=resets, source=source)

    if five_match:
        five = fill(five_match, five, "5h")
        if five.resets_at is not None and five.resets_at - now > 6 * 3600:
            five = UsageWindow("5-hour", "5h", unavailable_reason="invalid reset from Codex app-server")
            warnings.append(f"Codex 5h reset failed sanity check ({five_match[0]})")
    if week_match:
        week = fill(week_match, week, "weekly")

    if (
        five.used_percent is not None
        and week.used_percent is not None
        and five.used_percent == week.used_percent
        and five.resets_at == week.resets_at
    ):
        five = UsageWindow("5-hour", "5h", unavailable_reason="duplicate of weekly window")
        warnings.append("Codex windows had identical reset and usage; 5h was not duplicated")

    windows = [five, week]
    # Any other window Codex exposes (unknown durations) is shown as-is.
    used_ids = {id(m[1]) for m in (five_match, week_match) if m}
    for slot, raw in candidates:
        if id(raw) in used_ids or slot not in ("primary", "secondary"):
            continue
        minutes = window_duration(raw)
        used = to_float(raw.get("usedPercent"))
        if minutes is None or not valid_percent(used):
            continue
        label, short = duration_label(minutes)
        resets = to_int(raw.get("resetsAt"))
        if resets is not None and resets <= now:
            continue
        windows.append(UsageWindow(label, short, used_percent=used, resets_at=resets, source=slot))

    if five.source and five.source.startswith("session log"):
        warnings.append("5h value from latest Codex session log (may lag live usage)")

    raw_fields = {
        "primaryDurationMins": window_duration(rate_limits.get("primary")),
        "secondaryDurationMins": window_duration(rate_limits.get("secondary")),
        "fiveHourSource": five_match[0] if five_match else None,
        "weeklySource": week_match[0] if week_match else None,
    }
    return windows, warnings, raw_fields


def windows_complete(rate_limits: dict[str, Any], now: int) -> bool:
    windows, _, _ = build_windows(rate_limits, now)
    return all(w.used_percent is not None for w in windows[:2])


def merge_sparse(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    for key, value in update.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge_sparse(base[key], value)
        else:
            base[key] = value
    return base


# ------------------------------------------------------------------ provider
class CodexProvider(Provider):
    id = "codex"
    display_name = "OpenAI Codex"
    vendor = "OpenAI"

    @property
    def command(self) -> str:
        return str(self.settings.get("command") or "codex")

    def detect(self) -> Detection:
        exe = which(self.command)
        return Detection(self.id, self.display_name, exe is not None, exe)

    def candidate_homes(self) -> list[Path]:
        homes: list[Path] = []
        if self.settings.get("auto_discover", True):
            env_home = os.environ.get("CODEX_HOME")
            if env_home:
                homes.append(expand_path(env_home))
            homes.append(Path.home() / ".codex")
            homes.extend(sorted(p for p in Path.home().glob(".codex-*") if p.is_dir()))
        homes.extend(expand_path(h) for h in self.settings.get("homes") or [])
        excluded = {str(expand_path(h).resolve()) for h in self.settings.get("exclude_homes") or []}
        seen: set[str] = set()
        result: list[Path] = []
        for home in homes:
            try:
                key = str(home.resolve())
            except OSError:
                continue
            if key in seen or key in excluded or not home.is_dir():
                continue
            seen.add(key)
            # A profile must look like a Codex home: login state or config.
            if (home / "auth.json").exists() or (home / "config.toml").exists():
                result.append(home)
        return result

    @staticmethod
    def default_name(home: Path) -> str:
        name = home.name
        if name == ".codex":
            return "Codex"
        if name.startswith(".codex-"):
            return f"Codex ({name[len('.codex-'):]})"
        return f"Codex ({name.lstrip('.')})"

    def discover_accounts(self) -> list[AccountRef]:
        if not self.detect().installed:
            return []
        return [
            AccountRef(self.id, self.default_name(home), pretty_path(home), {"home": str(home)})
            for home in self.candidate_homes()
        ]

    # --------------------------------------------------------------- query
    def _open(self, ref: AccountRef) -> tuple[Optional[AppServer], AccountUsage]:
        """Phase 1: start ``codex app-server`` for one home and identify the
        account it is logged into. The server is left running for phase 2."""
        started = int(time.time())
        exe = which(self.command) or self.command
        timeout = self.number("timeout_seconds", 12.0, 1.0, 120.0)
        account = AccountUsage(self.id, ref.name, profile=ref.profile, fetched_at=started,
                               source="codex app-server")
        account.diagnostics = {"home": ref.profile, "homePath": ref.details["home"], "identified": False}
        env = child_env(exe, CODEX_HOME=str(ref.details["home"]))
        server: Optional[AppServer] = None
        try:
            server = AppServer([exe, "app-server"], env)
            server.request(
                "initialize", 1, timeout,
                {"clientInfo": {"name": "aiusage", "title": "aiusage", "version": __version__}},
            )
            server.send({"method": "initialized", "params": {}})
            acct = server.request("account/read", 2, timeout, {"refreshToken": False})
            info = acct.get("account") if isinstance(acct.get("account"), dict) else None
            if not info:
                account.error = "not logged in"
                server.close()
                return None, account
            account.diagnostics["identified"] = True
            account.email = info.get("email") if isinstance(info.get("email"), str) else None
            account.plan = info.get("planType") if isinstance(info.get("planType"), str) else None
            account.diagnostics["accountType"] = info.get("type")
            return server, account
        except Exception as exc:
            account.error = " ".join(str(exc).split())[:300] or type(exc).__name__
            if server is not None:
                server.close()
            return None, account

    def _read_limits(self, server: AppServer, account: AccountUsage) -> AccountUsage:
        """Phase 2: read rate limits over an identified app-server session."""
        started = int(time.time())
        home = Path(account.diagnostics["homePath"])
        timeout = self.number("timeout_seconds", 12.0, 1.0, 120.0)
        wait_updates = self.number("notification_wait_seconds", 4.0, 0.0, 60.0)
        trace = account.diagnostics
        try:
            notifications: list[dict[str, Any]] = []

            def collect_update(msg: dict[str, Any]) -> None:
                if msg.get("method") == "account/rateLimits/updated" and isinstance(msg.get("params"), dict):
                    notifications.append(msg["params"])

            initial = server.request("account/rateLimits/read", 3, timeout, on_notification=collect_update)
            merged = json.loads(json.dumps(initial))
            try:
                complete = windows_complete(select_rate_limits(merged), started)
            except ValueError:
                complete = False
            observed: list[tuple[str, dict[str, Any]]] = []
            if not complete and wait_updates > 0:
                deadline = time.monotonic() + wait_updates
                while time.monotonic() < deadline:
                    msg = server.next_message(deadline - time.monotonic())
                    if msg is None:
                        break
                    collect_update(msg)
                for update in notifications:
                    try:
                        rl = select_rate_limits(update)
                    except ValueError:
                        continue
                    observed.extend(("account/rateLimits/updated", rl[s]) for s in ("primary", "secondary")
                                    if isinstance(rl.get(s), dict))
                    merge_sparse(merged, update)
                if notifications:
                    final = server.request("account/rateLimits/read", 4, timeout)
                    merge_sparse(merged, final)
            trace["updateNotifications"] = len(notifications)

            rate_limits = select_rate_limits(merged)
            if not account.plan and isinstance(rate_limits.get("planType"), str):
                account.plan = rate_limits["planType"]
            log_windows, log_evidence = latest_session_log_windows(
                home, started, self.number("session_log_max_age_days", 8, 0.0, 365.0)
            )
            trace["sessionLog"] = log_evidence
            windows, warnings, raw_fields = build_windows(rate_limits, started, observed + log_windows)
            trace["rawFields"] = raw_fields
            trace["limitIds"] = sorted(merged.get("rateLimitsByLimitId") or {}) if isinstance(
                merged.get("rateLimitsByLimitId"), dict) else []
            account.windows = windows
            account.warning = "; ".join(warnings) if warnings else None
            account.manual_resets = reset_credits(merged)
            if account.manual_resets:
                account.manual_reset_hint = "run /usage in Codex"
            account.fetched_at = int(time.time())
        except Exception as exc:
            account.error = " ".join(str(exc).split())[:300] or type(exc).__name__
        return account

    def query_usage(self, ref: AccountRef) -> AccountUsage:
        """Full query of a single home (used by diagnostics)."""
        server, account = self._open(ref)
        if server is None:
            return account
        try:
            return self._read_limits(server, account)
        finally:
            server.close()

    def query_all(self) -> list[AccountUsage]:
        """Identify every home in parallel, then read limits once per real
        account from its first home; duplicates are only used as fallbacks.
        A slow or broken duplicate profile therefore cannot delay the view."""
        refs = self.discover_accounts()
        if not refs:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(refs))) as pool:
            opened = list(pool.map(self._open, refs))
        groups: dict[str, list[tuple[Optional[AppServer], AccountUsage]]] = {}
        for server, account in opened:
            if account.diagnostics.get("identified"):
                key = (account.email or "").lower() or f"home:{account.profile}"
                groups.setdefault(key, []).append((server, account))

        def read_group(members: list[tuple[Optional[AppServer], AccountUsage]]) -> None:
            done = False
            for server, account in members:
                if server is None:
                    continue
                if done:
                    account.error = "skipped: duplicate profile of an account already read"
                    continue
                self._read_limits(server, account)
                done = not account.error and any(w.available for w in account.windows)

        try:
            if groups:
                with ThreadPoolExecutor(max_workers=min(8, len(groups))) as pool:
                    list(pool.map(read_group, groups.values()))
        finally:
            for server, _account in opened:
                if server is not None:
                    server.close()
        return self.dedupe([account for _server, account in opened])

    def dedupe(self, results: list[AccountUsage]) -> list[AccountUsage]:
        """One entry per real account. Homes logged into the same account are
        merged, preferring one that returned usage without error. Homes that
        could not be identified are hidden when any account was identified."""
        groups: dict[str, list[AccountUsage]] = {}
        order: list[str] = []
        unidentified: list[AccountUsage] = []
        for item in results:
            if not item.diagnostics.get("identified"):
                unidentified.append(item)
                continue
            key = (item.email or "").lower() or f"home:{item.profile}"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(item)

        accounts: list[AccountUsage] = []
        for key in order:
            members = groups[key]
            best = next((m for m in members if not m.error and any(w.available for w in m.windows)), None)
            best = best or next((m for m in members if not m.error), members[0])
            best.diagnostics["duplicateProfiles"] = [m.profile for m in members if m is not best]
            accounts.append(best)

        if not accounts:
            # Surface a failure instead of silently showing nothing, but do
            # not list profiles that are simply logged out.
            failed = [u for u in unidentified if u.error and u.error != "not logged in"]
            if failed:
                first = failed[0]
                first.account_name = "Codex"
                first.error = f"could not query codex app-server: {first.error}"
                return [first]
            return []

        for account in accounts:
            configured = self.config.display_name(self.id, account.account_name, account.email, account.profile)
            if configured != account.account_name:
                account.account_name = configured
            elif len(accounts) == 1:
                account.account_name = "Codex"
        return accounts

    # --------------------------------------------------------------- diagnose
    def diagnose(self) -> list[str]:
        det = self.detect()
        lines = [
            f"command: {self.command}",
            f"executable: {pretty_path(det.executable) if det.executable else 'not found'}",
            f"version: {command_version(det.executable) if det.executable else 'n/a'}",
        ]
        refs = self.discover_accounts()
        lines.append(f"candidate homes: {', '.join(r.profile or '' for r in refs) or 'none'}")
        if not refs:
            return lines
        with ThreadPoolExecutor(max_workers=min(8, len(refs))) as pool:
            results = list(pool.map(self.query_usage, refs))
        kept = {id(a) for a in self.dedupe(list(results))}
        for item in results:
            trace = item.diagnostics
            lines.append("")
            lines.append(f"home: {item.profile}")
            lines.append(f"  identified: {'yes' if trace.get('identified') else 'no'}")
            if item.email:
                lines.append(f"  account: {item.email} ({item.plan or 'plan unknown'})")
            lines.append(f"  shown on dashboard: {'yes' if id(item) in kept else 'no (duplicate or unidentified)'}")
            if item.error:
                lines.append(f"  error: {item.error}")
            raw = trace.get("rawFields") or {}
            if raw:
                lines.append(f"  primary windowDurationMins: {raw.get('primaryDurationMins')}")
                lines.append(f"  secondary windowDurationMins: {raw.get('secondaryDurationMins')}")
                lines.append(f"  5h source: {raw.get('fiveHourSource') or 'not found'}")
                lines.append(f"  weekly source: {raw.get('weeklySource') or 'not found'}")
                lines.append(f"  limit ids: {', '.join(trace.get('limitIds') or []) or 'n/a'}")
                lines.append(f"  rateLimits update notifications: {trace.get('updateNotifications', 0)}")
                latest = (trace.get("sessionLog") or {}).get("latest")
                lines.append(f"  latest session-log rate limits: {json.dumps(latest)}")
            if item.manual_resets is not None:
                lines.append(f"  manual reset credits: {item.manual_resets}")
            for w in item.windows:
                value = f"{w.used_percent:g}% used" if w.used_percent is not None else (w.unavailable_reason or "n/a")
                reset = f", resets in {compact_duration(w.resets_at - time.time())}" if w.resets_at else ""
                lines.append(f"  {w.display_label}: {value}{reset}")
        return lines
