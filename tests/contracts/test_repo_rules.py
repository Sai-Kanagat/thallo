"""Repository rules: clock injection, no vendor calls from the core, no dashes."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import check_rules  # noqa: E402


def test_repository_rules_hold():
    assert check_rules.violations() == []


def test_rules_catch_offenders(tmp_path, monkeypatch):
    bad = tmp_path / "thallo" / "copy"
    bad.mkdir(parents=True)
    (bad / "x.py").write_text("import anthropic\nfrom datetime import datetime\nnow = datetime.now()\n")
    (tmp_path / "note.md").write_text("a " + chr(0x2014) + " b\n")
    monkeypatch.setattr(check_rules, "ROOT", tmp_path)
    monkeypatch.setattr(check_rules, "LEGACY", {})
    found = "\n".join(check_rules.violations())
    assert "system clock" in found and "vendor import" in found and "dash" in found
