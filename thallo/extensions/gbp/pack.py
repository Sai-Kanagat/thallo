"""The manual pack: everything a person does in the Business Profile dashboard, with no API access.

Three parts:
- posts and photo updates: the copy packs the publisher's manual connection already produced
  (`PublishResult.copy_pack`, exact approved text and media hashes); passed in as they are;
- replies and answers: only drafts whose latest decision on their exact text hash is "approved"
  and not yet done; the text is re-hashed here and a draft whose text no longer matches is left out;
- profile changes: only approved proposals not yet done, re-hashed the same way.

The pack is data. Showing it sends nothing; a person pastes each item and marks it done.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from thallo.extensions.gbp.inbox import InboxItem
from thallo.extensions.gbp.profile import Proposal, value_hash
from thallo.extensions.gbp.replies import Decision, ReplyDraft, sha256_text, standing

DASHBOARD = "https://business.google.com/"


@dataclass(frozen=True)
class Pack:
    posts: tuple[dict[str, Any], ...]
    replies: tuple[dict[str, Any], ...]
    profile: tuple[dict[str, Any], ...]

    @property
    def empty(self) -> bool:
        return not (self.posts or self.replies or self.profile)

    def steps(self) -> list[str]:
        out: list[str] = []
        for p in self.posts:
            what = "photo update" if not p.get("text") else "post"
            out.append(f"Open {DASHBOARD}, add this {what} exactly as given, then paste the live link back.")
        for r in self.replies:
            out.append(f"Open the {r['kind']} from {r['author'] or 'the customer'} and paste this reply unedited: "
                       f"{r['text']}")
        for c in self.profile:
            out.append(f"Edit the profile: set {c['field']} to {c['proposed_value']!r}. Reason: {c['reason']}")
        return out


def build_pack(*, copy_packs: Sequence[Mapping[str, Any]], drafts: Sequence[ReplyDraft],
               items: Mapping[str, InboxItem], proposals: Sequence[Proposal], decisions: Sequence[Decision]) -> Pack:
    posts = tuple(dict(p) for p in copy_packs if p.get("channel") == "google_business_profile")
    replies: list[dict[str, Any]] = []
    for d in drafts:
        if sha256_text(d.text) != d.text_sha256 or standing(d.id, d.text_sha256, decisions) != "approved":
            continue
        approved = [x for x in decisions if x.subject_id == d.id and x.decision == "approved"
                    and x.subject_sha256 == d.text_sha256]
        if not approved or not approved[-1].read_confirmed:
            continue
        item = items.get(d.item_id)
        if item is None:
            continue
        replies.append({"draft_id": d.id, "kind": item.kind, "external_id": item.external_id, "author": item.author,
                        "rating": item.rating, "text": d.text, "text_sha256": d.text_sha256})
    profile: list[dict[str, Any]] = []
    for pr in proposals:
        if value_hash(pr.field, pr.proposed_value) != pr.value_sha256:
            continue
        if standing(pr.id, pr.value_sha256, decisions) != "approved":
            continue
        profile.append({"proposal_id": pr.id, "field": pr.field, "current_value": pr.current_value,
                        "proposed_value": pr.proposed_value, "reason": pr.reason, "value_sha256": pr.value_sha256})
    return Pack(posts, tuple(replies), tuple(profile))
