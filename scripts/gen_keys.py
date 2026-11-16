"""Generate Thallo's local keys straight into .env. Prints names, never values.

    .venv/bin/python scripts/gen_keys.py

THALLO_VAULT_KEY         base64 of 32 random bytes; encrypts stored tokens (WP-02)
THALLO_GATE_SIGNING_KEY  hex of 32 random bytes; signs approvals (WP-04)
THALLO_PROXY_STAMP_KEY   hex of 32 random bytes; the web server signs the client address (WP-09)

A key that already exists is left alone, because replacing it would make stored secrets
unreadable or invalidate every approval. Back them up in your password manager.
"""
from __future__ import annotations

import base64
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402

GENERATORS = {
    "THALLO_VAULT_KEY": lambda: base64.b64encode(secrets.token_bytes(32)).decode(),
    "THALLO_GATE_SIGNING_KEY": lambda: secrets.token_bytes(32).hex(),
    "THALLO_PROXY_STAMP_KEY": lambda: secrets.token_bytes(32).hex(),
}


def main() -> int:
    current = envfile.read()
    new = {k: make() for k, make in GENERATORS.items() if not current.get(k)}
    if new:
        envfile.set_values(new)
    for key in GENERATORS:
        print(f"{key}: {'generated and saved' if key in new else 'already set, left unchanged'}")
    if new:
        print("Now copy the new lines from .env into your password manager as a backup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
