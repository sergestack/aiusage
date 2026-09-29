"""Local cache of the last valid usage snapshot per account.

Caches only hold normalized usage values plus account identity (email/plan),
written 0600. They never contain tokens, cookies or raw provider responses.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from aiusage.config import cache_dir
from aiusage.util import read_json_file, redact, write_json_secure


def slug(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip().lower()).strip("-")
    return value or "default"


def cache_path(provider: str, key: str = "usage") -> Path:
    return cache_dir() / provider / f"{slug(key)}.json"


def load(provider: str, key: str = "usage") -> Optional[dict[str, Any]]:
    payload, _error = read_json_file(cache_path(provider, key))
    return payload if isinstance(payload, dict) else None


def save(provider: str, payload: dict[str, Any], key: str = "usage") -> Path:
    path = cache_path(provider, key)
    write_json_secure(path, redact(payload))
    return path
