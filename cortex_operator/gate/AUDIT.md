# Approvals, the audit log and transactions

Code: `cortex_operator/gate/approvals.py`, `cortex_operator/trust/audit.py`.

## One transaction per change (WP-01, WP-09, every editor)

The gate never writes. Each of these returns a `GateChange`:

- `approve_bundle_from_repo(...)` (WP-09, on "approve"): the new approval row, any earlier live approval of the same bundle voided, the bundle set to APPROVED with `root_hash`, every variant set to APPROVED with `content_sha256`, and the audit entries (`approval.voided` for each superseded approval, then `approval.granted`).
- `void_bundle_approvals(...)` (any edit after a grant: text, media, claim, link, rule version, or a person withdrawing): every live approval voided, the bundle set to VOIDED, approved variants set to CHECKING, and one `approval.voided` entry per approval.

The caller writes the whole `GateChange` in ONE database transaction, together with the edit that caused it:

1. insert `new_approval`, if any;
2. update each row in `voided_approvals` (`voided_at`, `void_reason`);
3. update the `bundle` row and each `variants` row;
4. insert each `audit_entries` row, in order.

If any step fails, all of them roll back. The audit entries are prepared with the next seq numbers after the current head (`AuditLog.prepare`). If another writer appended first, the unique `(tenant_id, seq)` constraint makes the insert fail and the whole transaction rolls back; recompute the change and try again. So the log can never say "granted" or "voided" while the rows say otherwise, and the rows can never change without the log.

`AuditLog.append` writes one entry on its own. Use it only for events that change no row.

## What publish checks against the log

`check_publish` refuses an approval row unless the log holds a matching `approval.granted` entry: same tenant, subject `("approval", approval.id)`, a human actor whose id is the approval's `approver_id`, and the same content hash, bundle id, method, `approved_at`, `expires_at` and canary flag as the row. The entry's own hash must match its contents, and the entry must sit inside an intact run of the chain from the entry before it through the head (and through the anchor, when WP-20 has recorded one). It also refuses an approver who is not a user of the tenant with role founder, approver or operator, a lifetime over `MAX_APPROVAL_TTL`, and an approval whose `content_hash` is not the bundle's current `root_hash`.

### What the chain does not prove, and the MAC that does

The chain hash is public: anyone who can insert into `audit_log` can compute a valid next entry. So a forged approval row plus a forged `approval.granted` entry appended at the head would pass every chain check above. The chain alone does not stop that.

What stops it is a keyed MAC. Every grant's payload carries `mac`, an HMAC-SHA256 (`cortex_operator/gate/signing.py`) over the approval's fields, made with the key in `THALLO_GATE_SIGNING_KEY` (32 bytes or more, hex). `check_publish` refuses a grant whose MAC does not verify, and blocks every publish when no key is configured.

- Only the gate processes hold the key: the approval path (WP-09 calling `approve_bundle_from_repo`) and the publish check (WP-08 calling `check_publish`). It is never stored in the database, never readable by the app database role, and never given to an agent, a prompt, a log or a commit. Sai adds it to the environment of those processes.
- With HMAC the verifier holds the same key as the signer, so the publish worker could in principle sign. Moving to an asymmetric signature (the publisher holds only a public key) needs an Ed25519 library that is not a dependency today; that is a follow-up for the lead.
- Rotating the key makes every outstanding approval fail verification, so they must be re-approved. Rotation needs a key id in the payload; there is none yet.

## The chain anchor (WP-20)

The chain detects any edit, deletion, insertion or reorder of entries, except one: removing entries from the end leaves a shorter chain that is still valid. Only an anchor catches that: the head `(seq, entry_hash)` from `AuditLog.head(tenant_id)`, recorded at a point in time and later passed to `verify(tenant_id, anchor=...)`.

- The anchor must be stored where agents and the app database role cannot write it: outside the tenant database, for example in the weekly report sent to the client, or in a separate append-only store only the operator controls. An anchor an agent can rewrite proves nothing.
- WP-20 (reports) owns recording the anchor on a schedule and verifying the chain against the last recorded anchor. Until WP-20 lands, no anchor is recorded, and tail truncation is not detected.

## Privacy

Entries carry an `ActorRef` (kind and id), never a session. The word "session" is refused anywhere in an entry: in the action, subject fields, actor id, payload keys and every string value, including a void reason.
