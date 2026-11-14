from datetime import datetime, timedelta

from thallo.contracts import BrandSettings, Item, Status
from thallo.notion import NotionCockpit, NotionMap
from thallo.publishing import MockPublisher
from thallo.truth import ClaimLedger
from thallo.workflow import Engine
from thallo import writer

MONDAY = datetime(2026, 11, 16, 9)


class FakeNotion:
    def __init__(self, status="Idea", draft=""):
        self.status, self.draft, self.comments, self.patches = status, draft, [], []

    def __call__(self, method, path, body=None):
        if path.endswith("/query"):
            text = [{"plain_text": self.draft}] if self.draft else []
            return {"has_more": False, "results": [{"id": "p1", "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Launch note"}]},
                "Channel": {"select": {"name": "LinkedIn"}},
                "Publish date": {"date": {"start": "2026-11-16T09:00:00"}},
                "Status": {"select": {"name": self.status}},
                "Draft": {"type": "rich_text", "rich_text": text}}}]}
        if method == "PATCH":
            self.patches.append(body["properties"])
            props = body["properties"]
            if "Status" in props:
                self.status = props["Status"]["select"]["name"]
            self.draft = "".join(c["text"]["content"] for c in props["Draft"]["rich_text"])
            return {}
        self.comments.append(body["rich_text"][0]["text"]["content"])
        return {}


class Source:
    def pull(self, item):
        return {}


def engine_for(fake, claims=None, complete=writer.mock):
    claims = claims or ClaimLedger()
    cockpit = NotionCockpit(NotionMap("db", "sai"), fake)
    return Engine(BrandSettings("sai", {"linkedin"}), cockpit,
                  writer.LlmWriter(complete, claims, lambda: MONDAY), MockPublisher(),
                  Source(), claims)


def test_notion_row_becomes_draft_in_review():
    fake = FakeNotion()
    engine_for(fake).tick(MONDAY - timedelta(days=7))
    assert fake.status == "Review" and "mock draft" in fake.draft
    assert fake.comments == ["Ready for review"]


def test_human_approval_in_notion_flows_to_published():
    fake = FakeNotion()
    engine = engine_for(fake)
    engine.tick(MONDAY - timedelta(days=7))
    fake.status = "Approved"
    engine.tick(MONDAY - timedelta(days=5))
    assert "p1" in engine.passports
    engine.tick(MONDAY - timedelta(days=3))
    assert fake.status == "Scheduled"
    engine.tick(MONDAY)
    assert fake.status == "Published"


def test_engine_never_writes_approved():
    fake = FakeNotion()
    engine = engine_for(fake)
    engine.tick(MONDAY - timedelta(days=7))
    fake.status = "Approved"
    fake.draft = "Edited by a person."
    engine.tick(MONDAY - timedelta(days=5))
    assert all(p.get("Status", {}).get("select", {}).get("name") != "Approved"
               for p in fake.patches)


def test_edit_after_approval_in_notion_returns_to_review():
    fake = FakeNotion()
    engine = engine_for(fake)
    engine.tick(MONDAY - timedelta(days=7))
    fake.status = "Approved"
    engine.tick(MONDAY - timedelta(days=5))
    fake.draft = "Sneaky change."
    engine.tick(MONDAY - timedelta(days=4))
    assert fake.status == "Review" and "p1" not in engine.passports


def test_unsupported_text_cannot_be_approved():
    fake = FakeNotion()
    engine = engine_for(fake)
    engine.tick(MONDAY - timedelta(days=7))
    fake.status, fake.draft = "Approved", "We grew 300%."
    engine.tick(MONDAY - timedelta(days=5))
    assert fake.status == "Review" and "Approval refused" in fake.comments[-1]


def test_model_inventing_a_number_is_stopped_by_the_ledger():
    fake = FakeNotion()
    engine_for(fake, complete=lambda s, p: "We doubled sales 2x.").tick(MONDAY - timedelta(days=7))
    assert fake.status == "Needs evidence"


def test_prompt_has_only_live_claims_and_rules():
    claims = ClaimLedger()
    good = claims.add("Good", "s", "e")
    claims.verify(good.id)
    claims.add("Unverified", "s", "e")
    prompt = writer.build_prompt(Item("a", "s", "linkedin", "b", MONDAY), ["Avoid x"], claims, MONDAY)
    assert "Good" in prompt and "Unverified" not in prompt and "Avoid x" in prompt


def test_provider_payloads():
    seen = {}

    def post(url, headers, body):
        seen.update(url=url, headers=headers, body=body)
        return {"content": [{"text": "hi"}]}

    assert writer.anthropic("k", post=post)("sys", "p") == "hi"
    assert seen["headers"]["x-api-key"] == "k" and seen["body"]["system"] == "sys"
    reply = {"candidates": [{"content": {"parts": [{"text": "yo"}]}}]}
    assert writer.gemini("k", post=lambda u, h, b: reply)("s", "p") == "yo"
