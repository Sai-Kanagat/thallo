"""Notion as the cockpit. Field and status names are mapped per brand."""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from cortex_operator.contracts import Item, Status

API = "https://api.notion.com/v1"
CHUNK = 2000
# The engine never writes these: approval is a human act in Notion.
HUMAN_ONLY = {Status.APPROVED}


@dataclass
class NotionMap:
    database_id: str
    brand: str
    title: str = "Name"
    channel: str = "Channel"
    date: str = "Publish date"
    status: str = "Status"
    draft: str = "Draft"
    url: str = "Published URL"
    statuses: dict[str, str] = field(default_factory=lambda: {
        "idea": "Idea", "needs_evidence": "Needs evidence", "review": "Review",
        "approved": "Approved", "on_hold": "On hold", "scheduled": "Scheduled",
        "published": "Published", "failed": "Failed"})


def http_transport(token: str) -> Callable[[str, str, dict | None], dict]:
    def call(method: str, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            API + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    return call


def _plain(prop: dict | None) -> str:
    if not prop:
        return ""
    return "".join(part["plain_text"] for part in prop.get(prop["type"]) or [])


def _chunks(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[i:i + CHUNK]}}
            for i in range(0, len(text), CHUNK)]


class NotionCockpit:
    def __init__(self, mapping: NotionMap, call: Callable) -> None:
        self.map, self.call = mapping, call
        self.reverse = {v: Status(k) for k, v in mapping.statuses.items()}
        self.cache: dict[str, Item] = {}
        self.written: dict[str, tuple] = {}
        self.restored: dict[str, tuple[int | None, set[int]]] = {}

    def progress(self) -> dict[str, tuple[int | None, set[int]]]:
        """Per-item forecast_seq and metrics_pulled, for persistence between runs."""
        state = dict(self.restored)
        state.update({i: (it.forecast_seq, set(it.metrics_pulled)) for i, it in self.cache.items()})
        return state

    def restore(self, state: dict[str, tuple[int | None, set[int]]]) -> None:
        self.restored = dict(state)
        for item_id, (seq, pulled) in state.items():
            if item_id in self.cache:
                self.cache[item_id].forecast_seq = seq
                self.cache[item_id].metrics_pulled = set(pulled)

    def _rows(self) -> list[dict]:
        rows, cursor = [], None
        while True:
            body = {"start_cursor": cursor} if cursor else {}
            page = self.call("POST", f"/databases/{self.map.database_id}/query", body)
            rows += page["results"]
            if not page.get("has_more"):
                return rows
            cursor = page["next_cursor"]

    def items(self) -> list[Item]:
        items = []
        for row in self._rows():
            props = row["properties"]
            date = (props.get(self.map.date) or {}).get("date")
            status = ((props.get(self.map.status) or {}).get("select")
                      or (props.get(self.map.status) or {}).get("status") or {}).get("name")
            channel = ((props.get(self.map.channel) or {}).get("select") or {}).get("name")
            if not date or status not in self.reverse or not channel:
                continue
            item = self.cache.get(row["id"]) or Item(
                row["id"], self.map.brand, channel.lower(), "", datetime.now())
            if row["id"] not in self.cache and row["id"] in self.restored:
                item.forecast_seq, pulled = self.restored[row["id"]]
                item.metrics_pulled = set(pulled)
            item.brief = _plain(props.get(self.map.title))
            item.channel = channel.lower()
            item.publish_at = datetime.fromisoformat(date["start"]).replace(tzinfo=None)
            item.status = self.reverse[status]
            item.text = _plain(props.get(self.map.draft))
            self.cache[row["id"]] = item
            self.written.setdefault(row["id"], self._state(item))
            items.append(item)
        return items

    @staticmethod
    def _state(item: Item) -> tuple:
        return item.status, item.text, item.url

    def save(self, item: Item) -> None:
        if self.written.get(item.id) == self._state(item):
            return
        props: dict = {self.map.draft: {"rich_text": _chunks(item.text)}}
        if item.status not in HUMAN_ONLY:
            kind = "select"
            props[self.map.status] = {kind: {"name": self.map.statuses[item.status.value]}}
        if item.url:
            props[self.map.url] = {"url": item.url}
        self.call("PATCH", f"/pages/{item.id}", {"properties": props})
        self.written[item.id] = self._state(item)

    def notify(self, item: Item, message: str) -> None:
        self.call("POST", "/comments", {"parent": {"page_id": item.id},
                                        "rich_text": _chunks(message)})
