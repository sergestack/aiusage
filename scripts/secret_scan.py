#!/usr/bin/env python3
"""Scan the repository for secrets and personal data before publishing.

Usage:
    python scripts/secret_scan.py [ROOT]

Extra forbidden literals (e.g. your own email addresses or username) can be
supplied without committing them, via a comma-separated environment variable:

    AIUSAGE_SCAN_EXTRA="me@mydomain.com,myusername" python scripts/secret_scan.py

Exit status is 1 when anything suspicious is found. A line can be exempted
with the marker ``secret-scan: allow``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "build", "dist", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
SKIP_SUFFIXES = {".pyc", ".png", ".jpg", ".gif", ".whl"}
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "users.noreply.github.com"}

PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}"),
    "bearer token": re.compile(r"(?i)bearer\s+(?!<redacted>|\{|\$|token\b)[A-Za-z0-9._~+/=-]{16,}"),
    "JWT": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "OpenAI/Anthropic key": re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{16,}"),
    "xAI key": re.compile(r"\bxai-[A-Za-z0-9]{20,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "AWS key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "cookie header": re.compile(r"(?i)\b(set-)?cookie\s*:\s*\S+=\S+"),
    "authorization header value": re.compile(r"(?i)[\"']authorization[\"']\s*:\s*[\"'](?!Bearer \{|Bearer <redacted>)[^\"']{8,}"),
    "credential assignment": re.compile(
        r"(?i)[\"']?(access_token|refresh_token|id_token|api_key|apikey|client_secret|password)[\"']?\s*[:=]\s*[\"'](?!FAKE|<redacted>|example|\{)[A-Za-z0-9._~+/=-]{12,}"
    ),
    "UUID / account id": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    "personal path": re.compile(r"(/home/[a-z][a-z0-9_-]*|/Users/[A-Za-z][A-Za-z0-9_-]*|C:\\\\Users\\\\)"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def iter_files(root: Path):
    """Files that would be published: in a git checkout, tracked plus
    untracked-but-not-ignored files; otherwise every file under root."""
    if (root / ".git").exists():
        try:
            listed = subprocess.run(
                ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                capture_output=True, check=True,
            ).stdout.decode().split("\0")
            for rel in sorted(filter(None, listed)):
                path = root / rel
                if path.is_file() and path.suffix not in SKIP_SUFFIXES:
                    yield path
            return
        except (OSError, subprocess.CalledProcessError):
            pass
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.suffix not in SKIP_SUFFIXES:
            yield path


def scan(root: Path, extra: list[str]) -> list[str]:
    findings: list[str] = []
    self_path = Path(__file__).resolve()
    for path in iter_files(root):
        if path.resolve() == self_path:
            continue  # this file necessarily contains the patterns
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        rel = path.relative_to(root)
        for lineno, line in enumerate(text.splitlines(), 1):
            if "secret-scan: allow" in line:
                continue
            for name, pattern in PATTERNS.items():
                for match in pattern.finditer(line):
                    if name == "email" and match.group(0).split("@", 1)[1].lower() in ALLOWED_EMAIL_DOMAINS:
                        continue
                    findings.append(f"{rel}:{lineno}: {name}: {match.group(0)[:60]}")
            for literal in extra:
                if literal and literal.lower() in line.lower():
                    findings.append(f"{rel}:{lineno}: forbidden literal (AIUSAGE_SCAN_EXTRA)")
    # Files that must never be committed, whatever their content.
    for path in iter_files(root):
        name = path.name.lower()
        if name in {"auth.json", ".credentials.json", ".env"} or name.endswith((".pem", ".key")):
            findings.append(f"{path.relative_to(root)}: forbidden file name")
    return findings


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
    extra = [x.strip() for x in os.environ.get("AIUSAGE_SCAN_EXTRA", "").split(",") if x.strip()]
    findings = scan(root, extra)
    for item in findings:
        print(item)
    files = sum(1 for _ in iter_files(root))
    print(f"secret scan: {files} files, {len(findings)} finding(s)" + (f", {len(extra)} extra literal(s)" if extra else ""))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
