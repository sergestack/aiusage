"""Small shared helpers (no third-party dependencies)."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

SECRET_KEY_HINTS = (
    "token",
    "secret",
    "cookie",
    "authorization",
    "credential",
    "password",
    "apikey",
    "api_key",
    "key",
)


def to_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if result is not None and not math.isfinite(result):
        return None
    return result


def to_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def valid_percent(value: Optional[float]) -> bool:
    return value is not None and math.isfinite(value) and 0 <= value <= 100


def parse_iso_epoch(value: Any) -> Optional[int]:
    """Parse an ISO-8601 timestamp to epoch seconds. Naive values are rejected
    rather than guessed, so a reset time is never fabricated."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # Python < 3.11 cannot parse fractional seconds with != 6 digits.
        trimmed = re.sub(r"(\.\d{1,6})\d*", r"\1", value.replace("Z", "+00:00"))
        match = re.match(r"^(.*?\.)(\d{1,5})([+-]\d\d:\d\d)$", trimmed)
        if match:
            trimmed = f"{match.group(1)}{match.group(2).ljust(6, '0')}{match.group(3)}"
        try:
            dt = datetime.fromisoformat(trimmed)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp())


def parse_reset(value: Any) -> Optional[int]:
    """Accept epoch seconds, epoch milliseconds or ISO-8601 strings."""
    if isinstance(value, str) and not re.fullmatch(r"\d+(\.\d+)?", value.strip()):
        return parse_iso_epoch(value)
    number = to_float(value)
    if number is None or number <= 0:
        return None
    if number > 1e12:  # milliseconds
        number /= 1000.0
    return int(number)


def compact_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours and len(parts) < 2:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "<1m"


def read_json_file(path: Path) -> tuple[Optional[Any], Optional[str]]:
    try:
        return json.loads(path.read_text()), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as exc:  # malformed or unreadable
        return None, f"{type(exc).__name__}"


def write_json_secure(path: Path, payload: Any, preserve_mode: bool = False) -> None:
    """Atomic write with 0600 permissions (or the existing file's mode)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    if preserve_mode and path.exists():
        mode = path.stat().st_mode & 0o777
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def redact(value: Any) -> Any:
    """Recursively replace secret-looking fields before anything is cached or
    printed by diagnostics."""
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            lower = str(key).lower().replace("-", "_")
            if any(hint in lower for hint in SECRET_KEY_HINTS):
                clean[key] = "<redacted>"
            else:
                clean[key] = redact(item)
        return clean
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and re.match(r"(?i)^bearer\s+\S+", value):
        return "Bearer <redacted>"
    return value


def pretty_path(path: Any) -> str:
    """Display a path with the home directory collapsed to ``~``."""
    text = str(path)
    home = str(Path.home())
    if home and home != "/" and (text == home or text.startswith(home + os.sep)):
        return "~" + text[len(home):]
    return text


def expand_path(value: Any) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def which(command: str) -> Optional[str]:
    if not command:
        return None
    if os.sep in command:
        path = expand_path(command)
        return str(path) if path.exists() and os.access(path, os.X_OK) else None
    return shutil.which(command)


def command_version(executable: str, timeout: float = 5.0) -> Optional[str]:
    try:
        cp = subprocess.run(
            [executable, "--version"],
            text=True,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (cp.stdout or cp.stderr or "").strip()
    return out.splitlines()[0] if cp.returncode == 0 and out else None


class HttpError(Exception):
    def __init__(self, status: Optional[int], message: str) -> None:
        super().__init__(message)
        self.status = status


def http_request_json(
    url: str,
    headers: dict[str, str],
    timeout: float,
    data: Optional[bytes] = None,
) -> Any:
    """GET/POST returning parsed JSON. Raises HttpError with a message that
    never includes request headers (and therefore never includes tokens)."""
    request = urllib.request.Request(url, headers=headers, data=data)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code, f"HTTP {exc.code}") from None
    except Exception as exc:
        raise HttpError(None, f"{type(exc).__name__}") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise HttpError(None, "malformed JSON response") from None


def now_epoch() -> int:
    return int(time.time())
