"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Optional

from aiusage import __version__
from aiusage.config import EXAMPLE_CONFIG, Config, cache_dir, config_file, load_config
from aiusage.discovery import build_providers, collect, detect_all
from aiusage.providers.claude import ClaudeProvider
from aiusage.renderer import ansi, render_dashboard, render_json_payload, render_telegram, resolve_tz
from aiusage.util import command_version, pretty_path

PROVIDER_IDS = ("claude", "codex", "grok")

# Fallback glyphs for terminals/pipes that cannot encode Unicode output
# (e.g. PYTHONIOENCODING=ascii or legacy code pages). Never "?".
ASCII_GLYPHS = str.maketrans({
    "█": "#", "░": ".", "■": "#", "□": ".", "·": "-", "▸": ">", "↳": ">", "─": "-",
    "—": "-", "✗": "x", "⚠": "!", "\ufe0f": "", "…": "...",
})


class _AsciiStdout:
    """Wraps a text stream whose encoding cannot represent the dashboard."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._encoding = stream.encoding or "ascii"

    def write(self, text: str) -> int:
        safe = text.translate(ASCII_GLYPHS).encode(self._encoding, "replace").decode(self._encoding)
        return self._stream.write(safe)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def ensure_encodable_stdout() -> None:
    try:
        "█░▸·↳─—✗⚠■□".encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        sys.stdout = _AsciiStdout(sys.stdout)  # type: ignore[assignment]
SUBCOMMANDS = ("detect", "diagnose", "self-test", "capture-claude", "config")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aiusage",
        description="One dashboard for the usage / limits of your installed AI coding agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "commands:\n"
            "  aiusage detect              show which agents/profiles were found\n"
            "  aiusage diagnose [PROVIDER] troubleshooting details (claude|codex|grok)\n"
            "  aiusage self-test           offline checks of parsing and rendering\n"
            "  aiusage config [--init]     show (or create) the optional config file\n"
            "  aiusage capture-claude      store Claude statusLine JSON from stdin\n"
        ),
    )
    parser.add_argument("command", nargs="?", choices=SUBCOMMANDS, help=argparse.SUPPRESS)
    parser.add_argument("target", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    parser.add_argument("--telegram", action="store_true", help="compact Telegram-markdown output")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="refresh every SECONDS (min 5)")
    parser.add_argument("--provider", choices=PROVIDER_IDS, help="only query one provider")
    parser.add_argument("--init", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"aiusage {__version__}")
    # Backwards-compatible aliases from the original script.
    for pid in PROVIDER_IDS:
        parser.add_argument(f"--diagnose-{pid}", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test", dest="self_test_flag", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--capture-claude", dest="capture_flag", action="store_true", help=argparse.SUPPRESS)
    return parser


def cmd_detect(config: Config) -> int:
    print(f"aiusage {__version__} — provider detection")
    print(f"config: {pretty_path(config_file())} ({'loaded' if config.loaded else 'not present, using defaults'})")
    print(f"cache:  {pretty_path(cache_dir())}")
    print()
    for provider, det in detect_all(config):
        state = "installed" if det.installed else "not installed"
        if not provider.enabled:
            state += ", disabled in config"
        version = command_version(det.executable) if det.executable else None
        where = f" at {pretty_path(det.executable)}" if det.executable else ""
        print(f"{provider.display_name}: {state}{where}" + (f" ({version})" if version else ""))
        if not det.installed or not provider.enabled:
            continue
        refs = provider.discover_accounts()
        if not refs:
            print("  no authenticated profile found")
        for ref in refs:
            print(f"  profile: {ref.profile or '-'}  [{ref.name}]")
        if provider.id == "codex" and refs:
            accounts = provider.query_all()
            print(f"  distinct accounts: {len(accounts)}")
            for account in accounts:
                dupes = account.diagnostics.get("duplicateProfiles") or []
                extra = f" (same account as {', '.join(dupes)})" if dupes else ""
                status = f"error: {account.error}" if account.error else "ok"
                print(f"    {account.account_name}: {account.email or 'unknown'} via {account.profile}{extra} — {status}")
    return 0


def cmd_diagnose(config: Config, target: Optional[str]) -> int:
    if target and target not in PROVIDER_IDS:
        print(f"unknown provider {target!r}; choose one of: {', '.join(PROVIDER_IDS)}", file=sys.stderr)
        return 2
    print(f"aiusage {__version__} diagnostics (secrets are never printed)")
    print(f"python: {sys.version.split()[0]}  platform: {sys.platform}")
    print(f"config: {pretty_path(config_file())} ({'loaded' if config.loaded else 'not present'})")
    print(f"cache:  {pretty_path(cache_dir())}")
    for provider in build_providers(config, target):
        print()
        print(f"== {provider.display_name} ==")
        if not provider.enabled:
            print("disabled in config")
            continue
        try:
            for line in provider.diagnose():
                print(line)
        except Exception as exc:
            print(f"diagnose failed: {type(exc).__name__}: {exc}")
    return 0


def cmd_config(init: bool) -> int:
    path = config_file()
    if init:
        if path.exists():
            print(f"config already exists: {pretty_path(path)}")
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE_CONFIG)
        print(f"wrote example config: {pretty_path(path)}")
        return 0
    print(f"config file: {pretty_path(path)} ({'present' if path.exists() else 'not present'})")
    print()
    print(EXAMPLE_CONFIG)
    return 0


def cmd_capture_claude(config: Config) -> int:
    raw = sys.stdin.read()
    ok = ClaudeProvider(config).capture_statusline(raw)
    return 0 if ok else 1


def watch_footer(interval: int, tz_name: Optional[str], color: bool) -> str:
    """Tell the user watch mode keeps running and how to leave it."""
    upcoming = datetime.fromtimestamp(time.time() + interval, resolve_tz(tz_name))
    clock = upcoming.strftime("%H:%M:%S")
    text = f"watching · refresh every {interval}s · next update {clock} · Ctrl-C to quit"
    return ansi(text, "2", color)


def run_dashboard(args: argparse.Namespace, config: Config) -> int:
    providers = None
    if args.provider:
        providers = [p for p in build_providers(config, args.provider) if p.enabled and p.detect().installed]
    color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    interval = max(5, args.watch) if args.watch else 0
    while True:
        accounts = collect(config, providers)
        if args.json:
            output = json.dumps(render_json_payload(accounts, config.timezone), indent=2)
        elif args.telegram:
            output = render_telegram(accounts, config.timezone)
        else:
            output = render_dashboard(accounts, config.timezone, color)
            if args.watch:
                output += "\n" + watch_footer(interval, config.timezone, color)
                if sys.stdout.isatty():
                    output = "\033[2J\033[H" + output
        try:
            print(output, flush=True)
        except BrokenPipeError:
            return 0
        if not args.watch:
            return 0
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_encodable_stdout()
    if args.command == "self-test" or args.self_test_flag:
        from aiusage.selftest import run_self_test

        return run_self_test()
    if args.command == "config":
        return cmd_config(args.init)

    config = load_config()
    if args.command == "capture-claude" or args.capture_flag:
        return cmd_capture_claude(config)
    if args.command == "detect":
        return cmd_detect(config)
    legacy = next((pid for pid in PROVIDER_IDS if getattr(args, f"diagnose_{pid}")), None)
    if args.command == "diagnose" or legacy:
        return cmd_diagnose(config, args.target or legacy)
    try:
        return run_dashboard(args, config)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
