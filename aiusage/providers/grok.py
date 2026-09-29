"""Grok (xAI) provider.

Usage comes from the same billing endpoint the Grok CLI's ``/usage`` view
calls, authenticated with the session the Grok CLI stored locally.

Grok exposes a billing *period* (e.g. weekly) plus credit amounts. When no
positive quota denominator is exposed, aiusage shows the absolute amount
used and says the quota is not exposed — it never invents a percentage.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from aiusage import APP_NAME, __version__, cache
from aiusage.models import AccountUsage, UsageWindow
from aiusage.providers.base import AccountRef, Detection, Provider
from aiusage.util import (
    HttpError,
    command_version,
    compact_duration,
    expand_path,
    http_request_json,
    parse_iso_epoch,
    pretty_path,
    read_json_file,
    redact,
    to_float,
    to_int,
    which,
    write_json_secure,
)

BILLING_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
BILLING_URL = f"{BILLING_BASE_URL}/billing?format=credits"
LEGACY_BILLING_URL = f"{BILLING_BASE_URL}/billing"
AUTO_TOPUP_URL = f"{BILLING_BASE_URL}/auto-topup-rule"
OIDC_TOKEN_URL = "https://auth.x.ai/oauth2/token"
LOG_TAIL_BYTES = 2 * 1024 * 1024

PERIOD_LABELS = {
    "daily": ("Day", "Day"),
    "weekly": ("Week", "Wk"),
    "monthly": ("Month", "Mo"),
    "rolling": ("Rolling", "Roll"),
}


def val(value: Any) -> Optional[float]:
    """Grok wraps amounts as {"val": n}."""
    if isinstance(value, dict):
        return to_float(value.get("val"))
    return to_float(value)


def period_labels(period_type: Any) -> tuple[str, str]:
    clean = str(period_type or "").replace("USAGE_PERIOD_TYPE_", "").strip().lower()
    return PERIOD_LABELS.get(clean, ("Usage", "Use"))


def window_from_billing(config: dict[str, Any], source: str = "live") -> UsageWindow:
    """Normalize a Grok billing ``config`` object into one window."""
    current = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else None
    if current is not None:
        label, short = period_labels(current.get("type"))
        reset = parse_iso_epoch(current.get("end"))
        used = val(config.get("onDemandUsed"))
        cap = val(config.get("onDemandCap"))
        used_percent = val(config.get("creditUsagePercent"))
        total = cap if cap and cap > 0 else None
        if used_percent is None and used is not None and total is not None:
            used_percent = max(0.0, min(100.0, 100.0 * used / total))
        if used_percent is not None and not 0 <= used_percent <= 100:
            used_percent = None
        reason = None
        if used_percent is None:
            reason = "total quota not exposed by Grok credits billing"
            if used is None:
                reason = "Grok credits billing omitted usage amount and total quota"
        remaining = total - used if total is not None and used is not None else None
        return UsageWindow(label, short, used_percent=used_percent, used_amount=used,
                           remaining_amount=remaining, total_amount=total, unit="credits",
                           resets_at=reset, unavailable_reason=reason, source=source)

    used = val(config.get("used"))
    total = val(config.get("monthlyLimit"))
    reset = parse_iso_epoch(config.get("billingPeriodEnd"))
    label, short = ("Month", "Mo") if "monthlyLimit" in config else ("Usage", "Use")
    used_percent: Optional[float] = None
    reason: Optional[str] = None
    if used is not None and total is not None and total > 0:
        used_percent = max(0.0, min(100.0, 100.0 * used / total))
    elif total == 0:
        reason = "Grok billing returned monthlyLimit=0; no percentage denominator exposed"
    elif used is None or total is None:
        reason = "Grok billing omitted used or total quota fields"
    else:
        reason = "Grok billing returned no usable positive quota denominator"
    remaining = total - used if used_percent is not None and total is not None and used is not None else None
    return UsageWindow(label, short, used_percent=used_percent, used_amount=used,
                       remaining_amount=remaining, total_amount=total, unit="credits",
                       resets_at=reset, unavailable_reason=reason, source=source)


def related_fields(source: str, payload: Any) -> list[dict[str, Any]]:
    """Inventory of quota-looking fields for diagnostics (secrets redacted)."""
    terms = ("usage", "used", "remaining", "quota", "credit", "allowance", "limit", "reset",
             "subscription", "plan", "period", "cap", "balance", "topup")
    rows: list[dict[str, Any]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{path}.{key}" if path else str(key)
                if any(term in str(key).lower() for term in terms):
                    rows.append({"source": source, "path": child, "value": redact(item)})
                walk(item, child)
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                walk(item, f"{path}[{idx}]")

    walk(payload, "")
    return rows


class GrokProvider(Provider):
    id = "grok"
    display_name = "Grok"
    vendor = "xAI"

    @property
    def command(self) -> str:
        return str(self.settings.get("command") or "grok")

    @property
    def home(self) -> Path:
        configured = self.settings.get("home") or os.environ.get("GROK_HOME")
        return expand_path(configured) if configured else Path.home() / ".grok"

    @property
    def auth_file(self) -> Path:
        return self.home / "auth.json"

    @property
    def log_file(self) -> Path:
        return self.home / "logs" / "unified.jsonl"

    def detect(self) -> Detection:
        exe = which(self.command)
        return Detection(self.id, self.display_name, exe is not None, exe)

    # --------------------------------------------------------------- auth
    def auth_record(self) -> tuple[Optional[str], Optional[dict[str, Any]], Optional[str]]:
        payload, error = read_json_file(self.auth_file)
        if error:
            return None, None, "Grok auth not found" if error == "missing" else "Grok auth unreadable"
        if not isinstance(payload, dict):
            return None, None, "Grok auth malformed"
        records = [(k, v) for k, v in payload.items() if isinstance(v, dict)]
        if not records:
            return None, None, "Grok auth has no account records"
        records.sort(key=lambda kv: str(kv[1].get("expires_at") or ""), reverse=True)
        key, record = records[0]
        if not isinstance(record.get("key"), str) or not record.get("key"):
            return key, record, "Grok auth record has no session token"
        return key, record, None

    @staticmethod
    def auth_expired(record: dict[str, Any], now: Optional[int] = None) -> bool:
        expires = parse_iso_epoch(record.get("expires_at"))
        return expires is not None and expires <= (now or int(time.time())) + 60

    def refresh_auth(self, record_key: str, record: dict[str, Any], timeout: float) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        if not self.settings.get("refresh_expired_auth", True):
            return None, "Grok session expired; run `grok` to refresh it"
        refresh_token = record.get("refresh_token")
        client_id = record.get("oidc_client_id")
        if not isinstance(refresh_token, str) or not refresh_token:
            return None, "Grok session expired and no refresh token is available"
        if not isinstance(client_id, str) or not client_id:
            return None, "Grok session expired and no OIDC client id is available"
        body = urllib.parse.urlencode(
            {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
        ).encode()
        try:
            payload = http_request_json(
                OIDC_TOKEN_URL,
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": f"{APP_NAME}/{__version__}",
                },
                timeout,
                data=body,
            )
        except HttpError as exc:
            return None, f"Grok auth refresh {exc}"
        if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
            return None, "Grok auth refresh response malformed"
        updated = dict(record)
        updated["key"] = payload["access_token"]
        if isinstance(payload.get("refresh_token"), str) and payload["refresh_token"]:
            updated["refresh_token"] = payload["refresh_token"]
        expires_in = to_int(payload.get("expires_in"))
        if expires_in:
            updated["expires_at"] = (datetime.now().astimezone() + timedelta(seconds=max(0, expires_in))).isoformat()
        # Write back so the (possibly rotated) refresh token is not lost.
        current, error = read_json_file(self.auth_file)
        if not error and isinstance(current, dict):
            current[record_key] = updated
            write_json_secure(self.auth_file, current, preserve_mode=True)
        return updated, None

    def client_version(self) -> str:
        exe = which(self.command)
        text = command_version(exe) if exe else None
        parts = (text or "").split()
        return parts[1] if len(parts) >= 2 and parts[0] == "grok" else "1.0.0"

    def fetch(self, url: str, record: dict[str, Any], timeout: float, version: str) -> Any:
        return http_request_json(
            url,
            {
                "Authorization": f"Bearer {record.get('key')}",
                "x-xai-token-auth": "xai-grok-cli",
                "x-grok-client-version": version,
                "Accept": "application/json",
                "User-Agent": f"grok/{version}",
            },
            timeout,
        )

    def latest_billing_log(self) -> Optional[dict[str, Any]]:
        try:
            with self.log_file.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - LOG_TAIL_BYTES))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            if "billing: fetched credits config" not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("ctx"), dict):
                return {"logged_at": parse_iso_epoch(str(obj.get("ts") or "")), "ctx": obj["ctx"]}
        return None

    def discover_accounts(self) -> list[AccountRef]:
        if not self.detect().installed:
            return []
        _key, record, error = self.auth_record()
        if record is None or (error and "no session token" not in error):
            return []
        name = self.config.display_name(self.id, "Grok", record.get("email") if isinstance(record.get("email"), str) else None,
                                        pretty_path(self.home))
        return [AccountRef(self.id, name, pretty_path(self.home))]

    # --------------------------------------------------------------- query
    def query_usage(self, ref: AccountRef) -> AccountUsage:
        started = int(time.time())
        timeout = float(self.settings.get("timeout_seconds") or 8.0)
        account = AccountUsage(self.id, ref.name, profile=ref.profile, fetched_at=started,
                               source="Grok credits billing API")
        record_key, record, auth_error = self.auth_record()
        if isinstance(record, dict) and isinstance(record.get("email"), str):
            account.email = record["email"]
        log = self.latest_billing_log()
        if log and isinstance(log["ctx"].get("subscriptionTier"), str):
            account.plan = log["ctx"]["subscriptionTier"]
        if auth_error or record is None or record_key is None:
            account.error = auth_error or "Grok auth unavailable"
            return account

        version = self.client_version()
        refresh_note: Optional[str] = None
        refreshed = False
        if self.auth_expired(record, started):
            new_record, refresh_note = self.refresh_auth(record_key, record, timeout)
            if new_record is not None:
                record, refreshed = new_record, True

        live: Any = None
        live_error: Optional[str] = None
        try:
            live = self.fetch(BILLING_URL, record, timeout, version)
        except HttpError as exc:
            live_error = f"Grok billing {exc}"
            if exc.status == 401 and not refreshed:
                new_record, refresh_note = self.refresh_auth(record_key, record, timeout)
                if new_record is not None:
                    record, refreshed = new_record, True
                    try:
                        live, live_error = self.fetch(BILLING_URL, record, timeout, version), None
                    except HttpError as exc2:
                        live_error = f"Grok billing {exc2}"
        if live_error and refresh_note:
            live_error = f"{live_error}: {refresh_note}"
        account.diagnostics = {"auth_refreshed": refreshed, "live_error": live_error, "version": version}

        config = live.get("config") if isinstance(live, dict) and isinstance(live.get("config"), dict) else None
        if config is None:
            return self._from_cache(account, live_error or "Grok billing response malformed", started)

        window = window_from_billing(config)
        if window.resets_at is not None and window.resets_at <= started:
            window.expired = True
            window.used_percent = None
            window.unavailable_reason = "Grok reset timestamp is expired"
            account.stale = True
        account.windows = [window]
        account.diagnostics["live_config"] = redact(config)
        account.diagnostics["prepaid_balance"] = val(config.get("prepaidBalance"))
        if not window.expired:
            try:
                cache.save(self.id, {"fetched_at": started, "email": account.email, "plan": account.plan,
                                     "windows": [window.to_dict()]})
            except OSError:
                pass
        return account

    def _from_cache(self, account: AccountUsage, reason: str, now: int) -> AccountUsage:
        cached = cache.load(self.id)
        windows = [UsageWindow.from_dict(w) for w in (cached or {}).get("windows") or [] if isinstance(w, dict)]
        if not cached or not windows:
            account.error = reason
            return account
        fetched = to_int(cached.get("fetched_at")) or now
        account.fetched_at = fetched
        account.email = account.email or cached.get("email")
        account.plan = account.plan or cached.get("plan")
        account.source = "Grok cached billing"
        account.stale = True
        account.stale_reason = f"Grok live refresh failed ({reason})"
        for window in windows:
            if window.resets_at is not None and window.resets_at <= now:
                window.expired = True
                window.used_percent = None
        account.windows = windows
        if all(w.expired for w in windows):
            account.error = f"Grok usage unavailable: {reason}; last successful update {compact_duration(now - fetched)} ago"
        else:
            account.warning = reason
        return account

    # --------------------------------------------------------------- diagnose
    def diagnose(self) -> list[str]:
        det = self.detect()
        lines = [
            f"command: {self.command}",
            f"executable: {pretty_path(det.executable) if det.executable else 'not found'}",
            f"version: {command_version(det.executable) if det.executable else 'n/a'}",
            f"home: {pretty_path(self.home)}",
            f"auth file: {pretty_path(self.auth_file)} ({'present' if self.auth_file.exists() else 'missing'}; secrets never printed)",
        ]
        key, record, error = self.auth_record()
        if record is not None:
            lines.append(f"session expired: {'yes' if self.auth_expired(record) else 'no'}")
            lines.append(f"auto refresh expired session: {bool(self.settings.get('refresh_expired_auth', True))}")
        if error:
            lines.append(f"auth problem: {error}")
        log = self.latest_billing_log()
        if log:
            age = compact_duration(time.time() - log["logged_at"]) if log.get("logged_at") else "unknown"
            lines.append(f"latest /usage billing log: {age} ago (plan: {log['ctx'].get('subscriptionTier')})")
        else:
            lines.append("latest /usage billing log: none")
        if det.installed and record is not None and not error:
            account = self.query_usage(AccountRef(self.id, "Grok", pretty_path(self.home)))
            lines.append(f"account: {account.email or 'unknown'} ({account.plan or 'plan unknown'})")
            lines.append(f"live billing: {account.diagnostics.get('live_error') or 'ok'}")
            lines.append(f"auth refreshed this run: {account.diagnostics.get('auth_refreshed')}")
            if account.error:
                lines.append(f"error: {account.error}")
            for w in account.windows:
                lines.append(f"window: {w.label}")
                lines.append(f"  used_percent: {w.used_percent}")
                lines.append(f"  used_amount: {w.used_amount} {w.unit or ''}".rstrip())
                lines.append(f"  total_amount: {w.total_amount}")
                lines.append(f"  resets_at: {w.resets_at}")
                lines.append(f"  unavailable_reason: {w.unavailable_reason or 'none'}")
            fresh_record = self.auth_record()[1] or record
            version = self.client_version()
            for label, url in (("credits billing", BILLING_URL), ("legacy billing", LEGACY_BILLING_URL),
                               ("auto top-up", AUTO_TOPUP_URL)):
                try:
                    payload = self.fetch(url, fresh_record, 8.0, version)
                    fields = related_fields(label, payload)
                    lines.append(f"{label} endpoint: ok, {len(fields)} quota-related fields")
                    for row in fields:
                        value = row["value"]
                        text = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else repr(value)
                        lines.append(f"  {row['path']}: {text}")
                except HttpError as exc:
                    lines.append(f"{label} endpoint: {exc}")
        return lines
