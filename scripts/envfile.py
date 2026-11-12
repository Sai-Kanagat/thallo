"""Read and update .env without ever printing a value. Used by gen_keys and provision_db."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = Path(os.environ.get("THALLO_ENV_FILE", ROOT / ".env"))


def read() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV.exists():
        return values
    for raw in ENV.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def set_values(updates: dict[str, str]) -> None:
    """Replace or append KEY=value lines, keep everything else, and keep the file private."""
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    done: set[str] = set()
    out: list[str] = []
    for raw in lines:
        key = raw.partition("=")[0].strip()
        if key in updates and not raw.lstrip().startswith("#"):
            out.append(f"{key}={updates[key]}")
            done.add(key)
        else:
            out.append(raw)
    out += [f"{k}={v}" for k, v in updates.items() if k not in done]
    ENV.write_text("\n".join(out) + "\n")
    os.chmod(ENV, 0o600)
