"""Repository rules that tests alone do not catch. Run by scripts/ci.sh and tests/contracts.

1. Time comes from a Clock. No datetime.now(), utcnow(), date.today() or time.time() outside
   cortex_operator/contracts/clock.py.
2. The core never calls a vendor. Network and vendor SDK imports live only in
   cortex_operator/providers/.
3. No em or en dashes in code, docs, SQL or config.

LEGACY lists files from the original engine that break a rule today. The list may only
shrink: each entry is removed by the package that ports the file.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", ".git", "node_modules", ".next", "brag-output", "__pycache__", ".pytest_cache",
             ".ruff_cache"}
TEXT_SUFFIXES = {".py", ".md", ".sql", ".toml", ".yaml", ".yml", ".json", ".sh", ".ts", ".tsx", ".css", ".html"}

CLOCK_RE = re.compile(r"\b(datetime\.(now|utcnow)|date\.today|time\.time)\(")
VENDOR_RE = re.compile(
    r"^\s*(import|from)\s+(anthropic|openai|google|httpx|requests|urllib|aiohttp|notion_client|boto3|stripe|"
    r"twilio|smtplib|socket)\b", re.M)
DASH_RE = re.compile("[" + chr(0x2013) + chr(0x2014) + "]")

CLOCK_HOME = "cortex_operator/contracts/clock.py"
PROVIDERS = "cortex_operator/providers/"

LEGACY: dict[str, set[str]] = {
    "cortex_operator/run.py": {"clock"},  # ported by the worker package
    "cortex_operator/notion.py": {"clock", "vendor"},  # WP-17 calendar mirror
    "cortex_operator/postiz.py": {"clock", "vendor"},  # WP-08 publisher
    "cortex_operator/writer.py": {"vendor"},  # WP-06 text providers
}


def _files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS):
            out.append(p)
    return out


def violations() -> list[str]:
    found: list[str] = []
    for path in _files():
        rel = path.relative_to(ROOT).as_posix()
        if path.suffix not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        allowed = LEGACY.get(rel, set())
        if DASH_RE.search(text):
            found.append(f"{rel}: em or en dash")
        if path.suffix != ".py" or not rel.startswith("cortex_operator/"):
            continue  # clock and vendor rules bind product code; tests feed bad input on purpose
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        code = re.sub(r'"""[\s\S]*?"""', "", code)
        if rel != CLOCK_HOME and "clock" not in allowed and CLOCK_RE.search(code):
            found.append(f"{rel}: reads the system clock; take a Clock instead")
        if not rel.startswith(PROVIDERS) and "vendor" not in allowed and VENDOR_RE.search(code):
            found.append(f"{rel}: network or vendor import outside cortex_operator/providers/")
    for rel in LEGACY:
        if not (ROOT / rel).exists():
            found.append(f"{rel}: listed in LEGACY but gone; remove it from the list")
    return found


if __name__ == "__main__":
    problems = violations()
    for p in problems:
        print(p)
    sys.exit(1 if problems else 0)
