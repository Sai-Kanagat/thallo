"""The web API (WP-09): login by identity, tenant switch, review deck, week plan, connections.

Rules every route follows:
- The tenant comes from the server-side session, never from the request. Every query runs inside
  one tenant transaction (`Backend.tenant`), so row-level security applies.
- Every state-changing route needs the session's CSRF token in `X-CSRF-Token`.
- The client never supplies a hash, a verdict or a tenant id that is trusted. The review card's
  root hash is recorded server-side when the card is shown, and approval passes that value to the
  gate, which recomputes everything.
- Errors are uniform and never carry a secret, a stored value or whether an email exists.

`create_app(services)` in `api.app` builds the FastAPI application.
"""
