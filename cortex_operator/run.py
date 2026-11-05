"""One pass over a brand's calendar: python -m cortex_operator.run brand.json

Real publishing does not exist yet, so approved items fail safely at publish time.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from cortex_operator import writer
from cortex_operator.contracts import BrandSettings, Item
from cortex_operator.notion import NotionCockpit, NotionMap, http_transport
from cortex_operator.truth import ClaimLedger
from cortex_operator.workflow import Engine


class NoPublisher:
    def publish(self, key: str, item: Item) -> str:
        raise ConnectionError("no real publisher connected")


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
    if len(sys.argv) != 2 or not os.environ.get("NOTION_TOKEN"):
        sys.exit(__doc__)
    config = json.loads(Path(sys.argv[1]).read_text())
    claims = ClaimLedger()
    cockpit = NotionCockpit(NotionMap(**config["notion"]), http_transport(os.environ["NOTION_TOKEN"]))
    brand = BrandSettings(config["notion"]["brand"], set(config["channels"]))
    engine = Engine(brand, cockpit, writer.LlmWriter(completer(), claims),
                    NoPublisher(), NoMetrics(), claims)
    engine.tick(datetime.now())
    print("Pass complete.")


if __name__ == "__main__":
    main()
