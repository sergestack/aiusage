# Changelog

## 0.1.2

- Published on PyPI as `aiusage-dashboard` (the `aiusage` name is taken by an
  unrelated project). The command is still `aiusage`.
- Watch mode shows a footer: refresh interval, next update time and
  "Ctrl-C to quit", so it no longer looks frozen between refreshes.
- `--json` reports the IANA timezone name (e.g. `America/New_York`) instead
  of an abbreviation such as `EDT`.

## 0.1.1

- Codex: identify all profiles in parallel, then read limits once per account;
  a slow or broken duplicate profile no longer delays the dashboard.
- Find agent CLIs in standard install locations when they are not on `PATH`
  (cron/systemd/launchd), and give child processes the CLI's directory on `PATH`.
- ASCII fallback output when stdout cannot encode Unicode (no crash, no `?`).
- Numeric settings: explicit `0` is honored (e.g. `notification_wait_seconds = 0`),
  invalid values fall back to defaults instead of breaking a provider.
- Removed the unused `grok.stale_after_seconds` setting (Grok: live = current,
  cached fallback = always STALE).
- Secret scan only scans files git would publish.

## 0.1.0

- Initial release: Claude Code, OpenAI Codex (multi-account) and Grok.
