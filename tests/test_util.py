import os

from aiusage.util import compact_duration, parse_iso_epoch, parse_reset, pretty_path, redact, to_float


def test_parse_iso_epoch():
    assert parse_iso_epoch("2033-05-18T03:33:20Z") == 2000000000
    assert parse_iso_epoch("2033-05-18T03:33:20+00:00") == 2000000000
    assert parse_iso_epoch("2033-05-18T03:33:20.123+00:00") == 2000000000
    assert parse_iso_epoch("2033-05-18T03:33:20.123456789Z") == 2000000000
    assert parse_iso_epoch("2033-05-18T03:33:20") is None  # naive: never guessed
    assert parse_iso_epoch("garbage") is None
    assert parse_iso_epoch(None) is None


def test_parse_reset_units():
    assert parse_reset(2000000000) == 2000000000
    assert parse_reset(2000000000000) == 2000000000  # milliseconds
    assert parse_reset("2000000000") == 2000000000
    assert parse_reset("2033-05-18T03:33:20Z") == 2000000000
    assert parse_reset(0) is None and parse_reset(-5) is None and parse_reset(None) is None


def test_to_float_rejects_junk():
    assert to_float("12.5") == 12.5
    assert to_float(True) is None and to_float("nan") is None and to_float({}) is None


def test_compact_duration():
    assert compact_duration(30) == "<1m"
    assert compact_duration(3 * 3600 + 5 * 60) == "3h 5m"
    assert compact_duration(2 * 86400 + 3 * 3600 + 60) == "2d 3h"


def test_redact():
    data = {
        "access_token": "x",
        "nested": {"Authorization": "Bearer y", "ok": 1},  # secret-scan: allow
        "list": [{"refresh_token": "z"}],
        "note": "Bearer abc",
    }
    clean = redact(data)
    assert clean["access_token"] == "<redacted>"
    assert clean["nested"] == {"Authorization": "<redacted>", "ok": 1}  # secret-scan: allow
    assert clean["list"][0]["refresh_token"] == "<redacted>"
    assert clean["note"] == "Bearer <redacted>"


def test_pretty_path(env):
    assert pretty_path(os.path.join(str(env.home), ".codex-work")) == "~/.codex-work"
    assert pretty_path("/opt/x") == "/opt/x"
