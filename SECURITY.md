# Security policy

## What aiusage touches

* **Reads** the credentials the official CLIs already stored locally
  (Claude Code OAuth credentials, Grok session), only to call the same usage
  endpoints those clients call. Codex credentials are never read directly —
  aiusage asks the official `codex app-server` for usage.
* **Writes** only its own cache (`$XDG_CACHE_HOME/aiusage`, files `0600`),
  and — only when the stored Grok session has expired and
  `grok.refresh_expired_auth` is enabled — Grok's own `auth.json`, atomically
  and preserving its permissions, so a rotated refresh token is not lost.
* Never modifies shell rc files or agent settings.
* Never prints, logs or caches tokens, cookies or authorization headers;
  diagnostics redact secret-looking fields.
* Never redeems Codex "manual reset" credits or otherwise changes account
  state.

## Reporting a vulnerability

Please report security issues privately through GitHub's
"Report a vulnerability" (Security Advisories) on this repository rather
than in a public issue. Include the version (`aiusage --version`) and steps
to reproduce. **Do not include real tokens, cookies or account data** in
reports — use `aiusage diagnose`, which redacts them.

## Before publishing / contributing

Run the secret scan; it checks for emails, bearer tokens, JWTs, API keys,
cookies, authorization headers, account IDs and personal paths:

```bash
python scripts/secret_scan.py
AIUSAGE_SCAN_EXTRA="me@example.com,myusername" python scripts/secret_scan.py
```

Test fixtures must be synthetic (`@example.com`, `FAKE` tokens).
