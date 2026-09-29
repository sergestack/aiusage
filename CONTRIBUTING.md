# Contributing

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
ruff check .
python scripts/secret_scan.py
```

## Adding a provider

1. Create `aiusage/providers/<name>.py` with a `Provider` subclass
   (`aiusage/providers/base.py`) implementing:
   * `detect()` — is the client installed (PATH / configured path)?
   * `discover_accounts()` — authenticated local profiles; return nothing for
     profiles that cannot be positively identified.
   * `query_usage(ref)` — return an `AccountUsage` with one `UsageWindow`
     per limit the provider exposes (percentages, amounts, units, resets).
   * `diagnose()` — troubleshooting lines; never include secrets.
   * Override `query_all()` if several profiles can map to one account.
2. Register it in `PROVIDER_CLASSES` (`aiusage/providers/__init__.py`) and
   add defaults to `DEFAULTS` in `aiusage/config.py`.
3. Add sanitized fixtures under `tests/fixtures/` and tests covering valid,
   missing, malformed, stale and expired data.

## Rules

* Never fabricate values: missing stays `None` → `N/A` / unavailable.
* Never present expired or stale data as current; stale accounts must set
  `stale=True`.
* Never launch an agent session to refresh usage.
* Never print, log or cache secrets.
* Keep runtime dependencies at zero (stdlib only, `tomli` on old Pythons).
