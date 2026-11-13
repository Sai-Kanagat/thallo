"""A keyed MAC on every approval grant (WP-04).

The audit chain hash is public: anyone who can insert a row can append a well-linked entry. So
each `approval.granted` entry also carries an HMAC-SHA256 over the approval's fields, made with
a key the app database role and agents never hold. `check_publish` refuses an approval whose
grant MAC does not verify, and blocks if no key is configured.

The key comes from the environment variable THALLO_GATE_SIGNING_KEY: 64 or more hex characters
(32 bytes or more). It lives only in the environment of the gate processes (the approval path and
the publish check) and never in the database, a prompt, a log or a commit. See AUDIT.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from cortex_operator.contracts.models import Approval

ENV_KEY = "THALLO_GATE_SIGNING_KEY"
MAC_DOMAIN = b"thallo/grant-mac/v1\n"
MIN_KEY_BYTES = 32
_HEX = re.compile(r"^[0-9a-fA-F]+$")


class GateKeyMissing(ValueError):
    pass


def grant_fields(approval: Approval) -> dict[str, Any]:
    """Every approval field a forger would want to change."""
    return {
        "approval_id": approval.id,
        "tenant_id": approval.tenant_id,
        "subject_kind": approval.subject_kind.value,
        "bundle_id": approval.subject_id,
        "content_hash": approval.content_hash,
        "approver_id": approval.approver_id,
        "method": approval.method.value,
        "approved_at": approval.approved_at.isoformat(),
        "expires_at": approval.expires_at.isoformat(),
        "canary": approval.canary,
        "acknowledged_flag_ids": sorted(approval.acknowledged_flag_ids),
    }


def void_fields(approval: Approval, reason: str) -> dict[str, Any]:
    """What a void MAC covers: which approval, of what content, and why."""
    return {
        "action": "voided",
        "approval_id": approval.id,
        "tenant_id": approval.tenant_id,
        "bundle_id": approval.subject_id,
        "content_hash": approval.content_hash,
        "reason": reason,
    }


@dataclass(frozen=True)
class GateSigner:
    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key, bytes) or len(self.key) < MIN_KEY_BYTES:
            raise GateKeyMissing(f"the gate signing key must be at least {MIN_KEY_BYTES} bytes")

    def __repr__(self) -> str:
        return "GateSigner(key=<hidden>)"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> GateSigner:
        raw = (environ if environ is not None else os.environ).get(ENV_KEY, "")
        if not raw:
            raise GateKeyMissing(f"{ENV_KEY} is not set")
        if not _HEX.match(raw) or len(raw) % 2:
            raise GateKeyMissing(f"{ENV_KEY} must be hex")
        return cls(bytes.fromhex(raw))

    def sign(self, fields: Mapping[str, Any]) -> str:
        blob = json.dumps(dict(fields), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hmac.new(self.key, MAC_DOMAIN + blob.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, fields: Mapping[str, Any], mac: object) -> bool:
        return isinstance(mac, str) and hmac.compare_digest(self.sign(fields), mac)


__all__ = ["ENV_KEY", "GateKeyMissing", "GateSigner", "grant_fields", "void_fields"]
