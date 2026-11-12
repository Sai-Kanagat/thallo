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

`check_publish` refuses an approval row unless the log holds a matching `approval.granted` entry: same tenant, subject `("approval", approval.id)`, a human actor whose id is the approval's `approver_id`, the same `content_hash` and bundle id, and an entry hash that matches its contents. It also refuses an approver who is not a user of the tenant with role founder, approver or operator, a lifetime over `MAX_APPROVAL_TTL`, and an approval whose `content_hash` is not the bundle's current `root_hash`. A row written straight to storage therefore does not publish.

## The chain anchor (WP-20)

The chain detects any edit, deletion, insertion or reorder of entries, except one: removing entries from the end leaves a shorter chain that is still valid. Only an anchor catches that: the head `(seq, entry_hash)` from `AuditLog.head(tenant_id)`, recorded at a point in time and later passed to `verify(tenant_id, anchor=...)`.

- The anchor must be stored where agents and the app database role cannot write it: outside the tenant database, for example in the weekly report sent to the client, or in a separate append-only store only the operator controls. An anchor an agent can rewrite proves nothing.
- WP-20 (reports) owns recording the anchor on a schedule and verifying the chain against the last recorded anchor. Until WP-20 lands, no anchor is recorded, and tail truncation is not detected.

## Privacy

Entries carry an `ActorRef` (kind and id), never a session. The word "session" is refused anywhere in an entry: in the action, subject fields, actor id, payload keys and every string value, including a void reason.
