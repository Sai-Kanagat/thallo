"""One pass over a brand's calendar: python -m thallo.run brand.json [state.db]

Nothing leaves from here: the engine schedules and measures, and sending goes through
thallo.publish (the publisher), which alone calls the gate's check_publish.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from thallo import writer
from thallo.crew import CrewWriter
from thallo.contracts import BrandSettings, Item
from thallo.notion import NotionCockpit, NotionMap, http_transport
from thallo.store import Store
from thallo.truth import ClaimLedger
from thallo.workflow import Engine


class NoMetrics:
    def pull(self, item: Item) -> dict[str, float]:
        return {}


def load_env(path: Path = Path(".env")) -> None:
    for line in path.read_text().splitlines() if path.exists() else []:
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def completer():
    provider = os.environ.get("LLM_PROVIDER", "mock")
    if provider == "anthropic":
        return writer.anthropic(os.environ["ANTHROPIC_API_KEY"])
    if provider == "gemini":
        return writer.gemini(os.environ["GEMINI_API_KEY"])
    return writer.mock


def main() -> None:
    load_env()
    if len(sys.argv) not in (2, 3) or not os.environ.get("NOTION_TOKEN"):
        sys.exit(__doc__)
    config = json.loads(Path(sys.argv[1]).read_text())
    claims = ClaimLedger()
    cockpit = NotionCockpit(NotionMap(**config["notion"]), http_transport(os.environ["NOTION_TOKEN"]))
    brand = BrandSettings(config["notion"]["brand"], set(config["channels"]))
    engine = Engine(brand, cockpit, CrewWriter(completer(), claims),
                    NoMetrics(), claims)
    store = Store(sys.argv[2]) if len(sys.argv) == 3 else Store.for_brand(brand.name)
    store.load(engine)
    minutes = float(os.environ.get("CORTEX_LOOP_MINUTES", "0"))
    while True:
        try:
            engine.tick(datetime.now())
        except OSError as error:
            print(f"Pass failed, will retry: {type(error).__name__}")
            if not minutes:
                raise
        finally:
            store.save(engine)
        if not minutes:
            break
        time.sleep(minutes * 60)
    print("Pass complete.")


if __name__ == "__main__":
    main()
