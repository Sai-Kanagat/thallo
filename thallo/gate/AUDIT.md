# Approvals, the audit log and transactions

Code: `thallo/gate/approvals.py`, `thallo/trust/audit.py`.

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

`check_publish` refuses an approval row unless the log holds a matching `approval.granted` entry: same tenant, subject `("approval", approval.id)`, a human actor whose id is the approval's `approver_id`, and the same content hash, bundle id, method, `approved_at`, `expires_at`, canary flag and `acknowledged_flag_ids` as the row (the MAC covers all of them).

Check results are bound to content (contracts 2.5.0, `gate/checks.py`). Only `check_result` rows whose `content_sha256` is the variant's current leaf hash count; rows about any other content are ignored, so correcting the content is what clears a FAIL, and restoring the old content brings it back. Among the counting rows, the rows at the highest attempt of each (variant, layer, check name) decide, and every such row counts. Coverage is decided by the gate per content type, from the one table `COVERAGE` in `gate/checks.py`: every variant needs a counting CODE-layer row from the truth compiler (`truth.compile`, or `truth.compile.<kind>` for a kind the compiler has), and a variant with media also needs a counting CODE-layer design-lint row (a name in `media.lint.CHECKS`). A name outside the table never satisfies coverage, though its FAIL and FLAG rows still count. A grant is refused when a variant misses any required type ("not checked", naming the missing types), and outright when any deciding row is a FAIL; a FAIL cannot be acknowledged. Otherwise `acknowledged_flag_ids` must cover every deciding FLAG row (two findings of the same check at one attempt need two acknowledgements), and it may not name ids that are not open flags. A rerun on unchanged content clears a FAIL when the world changed (a claim got verified; claim state is not in the hash) because `truth.to_check_results` writes a row under every key the compiler has on every run (PASS for each kind not raised, plus the summary `truth.compile`, FAIL when anything blocked), so the higher attempt decides; FAIL is not sticky. The final rule, per key (variant, layer, check name): let top be the highest attempt among its counting rows. Every PASS and FLAG at top decides; a FLAG that decides must be acknowledged. A FAIL decides unless a PASS or FLAG at top, at a higher attempt than the FAIL, was written after it (row ids are uuid7, ordered by creation time). So a FAIL written after a PASS, even at a lower attempt, blocks; a PASS or FLAG at the FAIL's own attempt, or below the top, never clears it; the repair loop (FAIL at 1, then PASS or FLAG at 2) clears it; and every row that clears a FAIL is itself deciding, so nothing is cleared by a row nobody sees. Assumption: every check-row id comes from `new_id` on one trusted clock (the check stack's process, or a clock it trusts). The order rule is only as good as that clock: a writer with a clock ahead of the others could make its rows look newer, and ids made within the same millisecond have no defined order, so a writer must not write a non-FAIL and a FAIL for one key in the same millisecond (copy production orders its batch ids by attempt for this reason). Every writer gets `content_sha256` from `gate.check_hash`, given the variant as approval will load it and the repository's resolver and non-link allowlist; with any of them missing it raises, so no row is hashed with guessed inputs. Copy production binds all its rows (every attempt) to the hash of the variant as it stores it, so its rows stop counting once media or tracked links are attached. At publish, `check_publish` applies the same coverage rule to the payload being sent (from the same `content_types`), re-reads the rows for the payload's exact hash and blocks on a FAIL (`check_failed`), on missing coverage (`not_checked`) and on any read error (`checks_unreadable`). `check_result` is append-only for the app role (revoked, plus a trigger that refuses UPDATE and DELETE for every role), so a FAIL stays on record. It does not stop the app role inserting a later PASS under the same key for the same content, which would supersede the FAIL; closing that needs a check writer the app role is not. A flag raised after approval voids nothing by itself; any content change still voids through the hash. If the chain holds any `approval.voided` entry for that approval after the grant, the approval is dead, whatever its row says (voids are signed with the gate key like grants, but any void entry counts, since voiding only removes permission). The entry's own hash must match its contents, and the entry must sit inside an intact run of the chain from the entry before it through the head (and through the anchor, when WP-20 has recorded one). It also refuses an approver who is not a user of the tenant with role founder, approver or operator, a lifetime over `MAX_APPROVAL_TTL`, and an approval whose `content_hash` is not the bundle's current `root_hash`.

### What the chain does not prove, and the MAC that does

The chain hash is public: anyone who can insert into `audit_log` can compute a valid next entry. So a forged approval row plus a forged `approval.granted` entry appended at the head would pass every chain check above. The chain alone does not stop that.

What stops it is a keyed MAC. Every grant's payload carries `mac`, an HMAC-SHA256 (`thallo/gate/signing.py`) over the approval's fields, made with the key in `THALLO_GATE_SIGNING_KEY` (32 bytes or more, hex). `check_publish` refuses a grant whose MAC does not verify, and blocks every publish when no key is configured.

- Only the gate processes hold the key: the approval path (WP-09 calling `approve_bundle_from_repo`) and the publish check (WP-08 calling `check_publish`). It is never stored in the database, never readable by the app database role, and never given to an agent, a prompt, a log or a commit. Sai adds it to the environment of those processes.
- With HMAC the verifier holds the same key as the signer, so the publish worker could in principle sign. Moving to an asymmetric signature (the publisher holds only a public key) needs an Ed25519 library that is not a dependency today; that is a follow-up for the lead.
- Rotating the key makes every outstanding approval fail verification, so they must be re-approved. Rotation needs a key id in the payload; there is none yet.

## The chain anchor (WP-20)

The chain detects any edit, deletion, insertion or reorder of entries, except one: removing entries from the end leaves a shorter chain that is still valid. Only an anchor catches that: the head `(seq, entry_hash)` from `AuditLog.head(tenant_id)`, recorded at a point in time and later passed to `verify(tenant_id, anchor=...)`.

- The anchor must be stored where agents and the app database role cannot write it: outside the tenant database, for example in the weekly report sent to the client, or in a separate append-only store only the operator controls. An anchor an agent can rewrite proves nothing.
- WP-20 (reports) owns recording the anchor on a schedule and verifying the chain against the last recorded anchor. Until WP-20 lands, no anchor is recorded, and tail truncation is not detected.

## Privacy

Entries carry an `ActorRef` (kind and id), never a session. The word "session" is refused anywhere in an entry: in the action, subject fields, actor id, payload keys and every string value, including a void reason.
