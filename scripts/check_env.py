"""Say which settings in .env are present, never what they are.

    .venv/bin/python scripts/check_env.py

Prints SET or MISSING per key, and the wave that needs it. Values are never printed,
logged or sent anywhere. Secrets live only in .env, which git ignores.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

NEEDED: list[tuple[str, str, str]] = [
    ("DATABASE_ADMIN_URL", "1", "Supabase owner connection string, for migrations"),
    ("DATABASE_URL", "1", "Supabase connection as the cortex_app role, for the app"),
    ("THALLO_VAULT_KEY", "1", "32 random bytes, base64; encrypts stored tokens"),
    ("ANTHROPIC_API_KEY", "2", "Claude: drafts, briefs, plans"),
    ("GEMINI_API_KEY", "2", "Gemini: second reader and background images"),
    ("POSTIZ_API_KEY", "2", "Postiz hosted: publishing"),
]


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> int:
    env = read_env(ROOT / ".env")
    missing = 0
    for key, wave, why in NEEDED:
        ok = bool(env.get(key))
        missing += not ok
        print(f"{'SET    ' if ok else 'MISSING'}  wave {wave}  {key:<20} {why}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
