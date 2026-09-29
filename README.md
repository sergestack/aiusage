# aiusage

**One terminal dashboard for the usage / limits of your AI coding agents.**

`aiusage` finds the AI coding CLIs installed on your machine — Claude Code,
OpenAI Codex (any number of accounts) and Grok — asks each one for the usage
and limits it exposes, and shows everything in one aligned, color-coded view.

```text
AI USAGE  ·  Mon Sep 28, 2026 · 8:49 PM EDT
────────────────────────────────────────────────────────────

MOST URGENT: Claude Code · Week · 32% LEFT · LOW · resets in 2h 10m

▸ Claude Code — Anthropic · you@example.com · PRO
  5h  ███████████████████░   93%  resets 11:30 PM EDT     in 2h 40m
  Wk  ██████░░░░░░░░░░░░░░   32%  resets 11:00 PM EDT     in 2h 10m  LOW
  ↳ Claude OAuth usage API · <1m ago

▸ Codex — OpenAI · you@example.com · PLUS
  5h  █████████████░░░░░░░   63%  resets 11:44 PM EDT     in 2h 55m
  Wk  █████████░░░░░░░░░░░   43%  resets Sat 1:44 PM EDT  in 4d 16h
  1 manual reset available · run /usage in Codex
  ↳ codex app-server · <1m ago

▸ Codex (work) — OpenAI · work@example.com · PLUS
  5h  ████████████████████  100%  resets Tue 1:08 AM EDT  in 4h 19m
  Wk  █████████████░░░░░░░   63%  resets Sat 3:07 PM EDT  in 4d 18h
  ↳ codex app-server · <1m ago

▸ Grok — xAI · you@example.com · SUPERGROK
  Wk  ░░░░░░░░░░░░░░░░░░░░   N/A  resets Wed 3:27 PM EDT  in 1d 18h
                                  0 credits used · quota not exposed
  ↳ Grok credits billing API · <1m ago
────────────────────────────────────────────────────────────
```

Colors: **>40% left** green · **21–40%** yellow `LOW` · **≤20%** bold red
`CRITICAL` · stale/unavailable neutral.

> **aiusage does not bypass, raise or reset any provider limit.** It only
> displays usage information that your already-authenticated local clients
> and their providers expose.

## Supported providers

| Provider | Detected via | What is shown | Source |
|---|---|---|---|
| Claude Code | `claude` on `PATH` + local login | 5-hour and weekly windows (+ per-model weekly windows when exposed) | Claude OAuth usage endpoint used by `/usage`; falls back to Claude Code's statusLine `rate_limits` snapshot |
| OpenAI Codex | `codex` on `PATH` + each logged-in `CODEX_HOME` | 5-hour and weekly windows, any other window Codex reports, manual "usage limit reset" credits | official `codex app-server` JSON-RPC (`account/rateLimits/read`) |
| Grok (xAI) | `grok` on `PATH` + `~/.grok/auth.json` | current billing period, credits used, percentage when a denominator is exposed | the billing endpoint used by Grok's `/usage` |

Each provider reports what it actually exposes — percentages, rolling
windows, billing periods, credits, reset times. Values a provider does not
expose are shown as `N/A` / unavailable; they are never guessed.

## Installation

```bash
pipx install aiusage-dashboard
```

The PyPI package is named `aiusage-dashboard` (the name `aiusage` belongs to
an unrelated project); the command it installs is `aiusage`.

Requires Python 3.9+ and no other runtime dependencies (plus `tomli` on
Python < 3.11 for the optional config file).

From a clone of this repository:

```bash
pipx install .                # recommended
pip install --user .          # plain pip
./install.sh                  # wrapper around pipx/pip; never edits shell rc files
```

## Quick start

```bash
aiusage                 # dashboard
aiusage detect          # what was found, and which profiles map to which account
aiusage --json          # machine-readable
aiusage --watch 60      # refresh every 60 s (Ctrl-C to stop)
aiusage --no-color      # plain text (NO_COLOR is honored too)
aiusage --telegram      # compact Markdown for chat bots
aiusage --provider codex
aiusage --version
```

No configuration is needed when the CLIs are installed normally.

## Auto-detection

On every run aiusage:

1. resolves `claude`, `codex` and `grok` on `PATH` (or configured paths);
2. finds local, authenticated profiles for installed clients (never reading
   anything it does not need, never printing secrets);
3. queries all providers in parallel and skips clients that are not installed.

CLIs are found on `PATH` first, then in the standard installer locations
(`~/.local/bin`, `~/.claude/local`, `~/.grok/bin`, `~/.npm-global/bin`,
`~/.bun/bin`, `~/.cargo/bin`, `~/bin`, `/usr/local/bin`, `/opt/homebrew/bin`),
so `aiusage` also works from cron, systemd or launchd with a minimal `PATH`.

Terminals or pipes that cannot display Unicode get an ASCII rendering
(`#`/`.` bars) instead of an error.

## Multiple Codex accounts

Codex keeps each login in a `CODEX_HOME` directory. aiusage looks at:

* `$CODEX_HOME` (if set)
* `~/.codex`
* every `~/.codex-*` directory
* any `homes` listed in the config

A directory counts as a profile only if it contains Codex login state or
config. Each profile is asked which account it is logged into (via
`codex app-server`), and **profiles logged into the same account are merged**
so each account appears once. Every profile is identified in parallel, but
usage is read only once per account (from its first profile, falling back to
duplicates on error), so a slow or broken duplicate profile cannot slow the
dashboard down. Profiles that are logged out or cannot be
identified are not shown. Default names come from the directory
(`~/.codex-work` → `Codex (work)`); rename them in the config.

## JSON mode

`aiusage --json` prints:

```json
{
  "aiusage_version": "0.1.0",
  "generated_at": 1790643126,
  "timezone": "America/New_York",
  "accounts": [
    {
      "provider": "codex",
      "account_name": "Codex (work)",
      "email": "work@example.com",
      "plan": "plus",
      "profile": "~/.codex-work",
      "windows": [
        {"label": "5-hour", "short_label": "5h", "used_percent": 37.0, "left_percent": 63.0,
         "used_amount": null, "remaining_amount": null, "total_amount": null, "unit": null,
         "resets_at": 1790653478, "unavailable_reason": null, "expired": false, "source": "primary"}
      ],
      "source": "codex app-server",
      "fetched_at": 1790643126,
      "stale": false,
      "warning": null,
      "error": null,
      "manual_resets": 1
    }
  ]
}
```

## Configuration (optional)

`$XDG_CONFIG_HOME/aiusage/config.toml` (default `~/.config/aiusage/config.toml`).
`aiusage config` prints an example; `aiusage config --init` writes it.

```toml
timezone = "Europe/Berlin"          # default: system timezone

[claude]
stale_after_seconds = 600
statusline_files = ["~/.cache/my-statusline/latest.json"]

[codex]
homes = ["~/work/codex-home"]
exclude_homes = ["~/.codex-old"]

[grok]
enabled = false

[names]                     # key: profile path, email or default name
"~/.codex-work" = "Codex Work"

[names.codex]               # per-provider table; wins over [names]
"you@example.com" = "Codex Personal"
```

Environment overrides: `AIUSAGE_CONFIG`, `AIUSAGE_CACHE_DIR`, `AIUSAGE_TZ`,
`CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `GROK_HOME`.

## Freshness rules

* Live values are shown as current.
* A cached fallback older than its provider's threshold is labelled **STALE**
  and is never chosen as MOST URGENT.
* A cached window whose reset time has passed is hidden — it no longer
  describes the current window. If all windows have expired, the account is
  shown as unavailable.
* Missing fields never become 0% or 100%; weekly values are never copied into
  5-hour windows; reset times are never invented.
* aiusage never starts an agent session to "refresh" usage.

## Claude statusLine fallback (optional)

If the Claude usage endpoint is unreachable or throttled, aiusage falls back
to the latest `rate_limits` snapshot from Claude Code's statusLine JSON. To
keep one, pipe the statusLine input into aiusage from your own statusLine
script:

```bash
#!/bin/sh
input=$(cat)
printf '%s' "$input" | aiusage capture-claude
# ... render your status line from "$input" as before ...
```

Partial or invalid payloads never overwrite a valid snapshot.

## Diagnostics

```bash
aiusage diagnose            # all providers
aiusage diagnose codex      # per-CODEX_HOME details, window durations, duplicates
aiusage diagnose claude
aiusage diagnose grok       # also lists quota-related billing fields
aiusage self-test           # offline parser/renderer checks
```

Diagnostics never print tokens, cookies or authorization headers.

## Known provider limitations

* **Claude:** the usage endpoint is undocumented and rate-limited; aiusage
  reuses a live result for 60 s and falls back to cached snapshots. On macOS,
  Claude Code stores credentials in the keychain: set
  `claude.macos_keychain = true` to let aiusage read it (may prompt).
* **Codex:** OpenAI has temporarily removed the 5-hour window before; it is
  then shown as "temporarily unavailable" and returns automatically.
  "Manual reset" credits are only reported — aiusage never redeems them.
* **Grok:** many plans expose credits used but no total; the percentage is
  then `N/A`. Expired Grok sessions are refreshed with the stored refresh
  token like the Grok CLI does (`grok.refresh_expired_auth = false` disables
  this).
* Windows is not tested.

## Privacy & security model

* Runs entirely locally; the only network calls are the same usage endpoints
  the official clients call, authenticated with the credentials those clients
  already stored.
* Tokens are read in memory only; they are never printed, logged or cached.
* The cache (`$XDG_CACHE_HOME/aiusage`, mode `0600`) holds normalized usage
  values plus account email/plan — no tokens or raw responses.
* No telemetry.

See [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Try |
|---|---|
| A provider is missing | `aiusage detect` — is the CLI on `PATH` and logged in? |
| Codex account missing | `aiusage diagnose codex`; check the profile is logged in (`CODEX_HOME=... codex login status`) |
| Claude shows STALE | live endpoint throttled/unreachable; open Claude Code once, or set up the statusLine fallback |
| Grok shows `N/A` | Grok did not expose a quota denominator; the credits used are still shown |
| Wrong timezone | set `timezone` in the config or `AIUSAGE_TZ` |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run `python -m pytest`,
`ruff check .` and `python scripts/secret_scan.py` before opening a PR.

## License

MIT — see [LICENSE](LICENSE).
