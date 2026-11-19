"""Shared Google auth for GA4 and Search Console. No `register` here.

The vault holds one JSON object per connection: {"client_id", "client_secret", "refresh_token"},
from an OAuth client Sai creates in the tenant's Google Cloud project (read-only scopes:
analytics.readonly, webmasters.readonly). Each pull exchanges it for a short-lived access token:

    POST https://oauth2.googleapis.com/token   form: grant_type=refresh_token, client_id,
                                               client_secret, refresh_token
    reply {access_token, expires_in, token_type}

The stored credential goes only to the token origin; the access token goes only to the API
origin, as a Bearer header. Both origins are constants, and `_call` refuses any other. The token
is held for this pull only, never stored.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

from thallo.connections.secret import Secret
from thallo.providers.analytics.http_base import AnalyticsHttpProvider, PullFailed, RawCall
from thallo.providers.text.base import Auth, check_header_secret

TOKEN_ORIGIN = ("https", "oauth2.googleapis.com", 443)
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleProvider(AnalyticsHttpProvider):
    api_origin: tuple[str, str, int]

    def _bearer(self) -> Auth:
        try:
            cred = json.loads(self._secret().reveal())
        except ValueError:
            raise PullFailed("the stored Google credential is not JSON; store it again") from None
        keys = ("client_id", "client_secret", "refresh_token")
        if not isinstance(cred, dict) or not all(isinstance(cred.get(k), str) and cred[k] for k in keys):
            raise PullFailed("the stored Google credential lacks client_id, client_secret or refresh_token")
        form = urlencode({"grant_type": "refresh_token", **{k: cred[k] for k in keys}}).encode()
        reply = self._json(RawCall("POST", TOKEN_URL, form, "application/x-www-form-urlencoded",
                                   timeout=self._timeout()), "the Google token exchange")
        token = reply.get("access_token") if isinstance(reply, dict) else None
        if not isinstance(token, str) or not token:
            raise PullFailed("the Google token exchange returned no access token")
        secret = Secret(token)
        try:
            check_header_secret(secret)
        except Exception:
            raise PullFailed("the Google access token cannot be sent as a header") from None
        return Auth("authorization", secret, "Bearer ")
