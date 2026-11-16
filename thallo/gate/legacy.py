"""The original engine's approval seal (`content_hash` on its passports). Its publish check, the
legacy `Gate`, was removed in WP-08: the only way out is `thallo.publish`, which calls
`gate.check_publish`."""
from __future__ import annotations

from thallo.contracts import Item, sha256
from thallo.truth import ClaimLedger


def content_hash(item: Item, rule_version: int, claims: ClaimLedger,
                 claim_ids: list[str] | tuple[str, ...]) -> str:
    return sha256([item.text, item.media, item.channel, rule_version,
                   list(claim_ids), claims.fingerprint(claim_ids)])

