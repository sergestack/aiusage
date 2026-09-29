import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_scanner():
    spec = importlib.util.spec_from_file_location("secret_scan", ROOT / "scripts" / "secret_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_has_no_secrets_or_personal_data():
    findings = load_scanner().scan(ROOT, [])
    assert findings == [], "\n".join(findings)


def test_scanner_detects_planted_secrets(tmp_path):
    fake_jwt = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    (tmp_path / "leak.txt").write_text(
        "contact: someone@realmail.test\n"  # secret-scan: allow
        f"token: {fake_jwt}\n"
        "path: /home/alice/.codex\n"  # secret-scan: allow
    )
    (tmp_path / "auth.json").write_text("{}")
    findings = load_scanner().scan(tmp_path, ["alice"])
    kinds = " ".join(findings)
    for expected in ("email", "JWT", "personal path", "forbidden literal", "forbidden file name"):
        assert expected in kinds
