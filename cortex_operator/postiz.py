"""Postiz scheduler adapter. Outside live mode, posts land only as Postiz drafts."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cortex_operator.contracts import Item

API = "https://api.postiz.com/public/v1"
PREFIX = "postiz://post/"


def http_transport(key: str, base: str = API) -> Callable[[str, str, dict | None], object]:
    def call(method: str, path: str, body: dict | None = None) -> object:
        request = urllib.request.Request(
            base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 429 or error.code >= 500:
                raise ConnectionError(f"postiz {error.code}") from error
            raise ValueError(f"postiz refused: {error.code}") from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TimeoutError("postiz timed out") from error
            raise ConnectionError("postiz unreachable") from error
    return call


class PostizPublisher:
    """Remembers sent keys on disk, because Postiz has no idempotency key."""

    def __init__(self, call: Callable, integrations: dict[str, dict], live: bool,
                 sent_path: Path) -> None:
        self.call, self.integrations, self.live, self.path = call, integrations, live, sent_path
        self.sent = json.loads(sent_path.read_text()) if sent_path.exists() else {}

    def publish(self, key: str, item: Item) -> str:
        if key in self.sent:
            return self.sent[key]
        target = self.integrations.get(item.channel)
        if not target:
            raise ValueError(f"no Postiz channel for {item.channel}")
        reply = self.call("POST", "/posts", {
            "type": "now" if self.live else "draft",
            "date": datetime.now(timezone.utc).isoformat(),
            "shortLink": False, "tags": [],
            "posts": [{"integration": {"id": target["id"]},
                       "value": [{"content": item.text, "image": []}],
                       "settings": {"__type": target["type"], **target.get("settings", {})}}]})
        first = reply[0] if isinstance(reply, list) and reply else reply
        post_id = first.get("postId") or first.get("id") if isinstance(first, dict) else None
        if not post_id:
            raise ValueError("postiz reply had no post id")
        self.sent[key] = PREFIX + str(post_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.sent))
        return self.sent[key]


class PostizMetrics:
    def __init__(self, call: Callable) -> None:
        self.call = call

    def pull(self, item: Item) -> dict[str, float]:
        if not (item.url or "").startswith(PREFIX):
            return {}
        rows = self.call("GET", f"/analytics/post/{item.url[len(PREFIX):]}?date=7", None)
        return {row["label"].lower(): float(row["data"][-1]["total"])
                for row in rows if row.get("data")}
