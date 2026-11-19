"""The one public route of WP-12: `GET /r/{slug}`, the tracked link redirector.

Mount the API (or a proxy to this path) so that THALLO_REDIRECT_PREFIXES is `https://<host>/r/`:
the gate resolves `<prefix><slug>` through the tenant's tracked_link row, and this route sends the
browser to that same stored destination. No session, no cookie, no CSRF (it changes nothing a
person owns), nothing from the request reaches the response. All rules live in
`thallo/tracking/redirector.py`. With no redirector configured (no THALLO_REDIRECT_DATABASE_URL)
every slug is a 404.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response

from api.context import services
from api.sessions import CLIENT_IP_HEADER, CLIENT_STAMP_HEADER, client_keys

router = APIRouter()

_TEXT = {404: "Link not found.", 429: "Too many requests. Try again in a minute.",
         503: "Link temporarily unavailable."}


@router.api_route("/r/{slug}", methods=["GET", "HEAD"], include_in_schema=False)
def redirect(slug: str, request: Request) -> Response:
    svc = services(request)
    if svc.redirector is None:
        return Response(_TEXT[404], status_code=404, media_type="text/plain")
    client, network = client_keys(request.client.host if request.client else None,
                                  request.headers.get(CLIENT_IP_HEADER), request.headers.get(CLIENT_STAMP_HEADER),
                                  stamp_key=svc.stamp_key, now=svc.clock.now(), proxy_peers=svc.proxy_peers)
    out = svc.redirector.handle(slug, client=client, network=network, count=request.method == "GET")
    if out.status == 302 and out.location:
        return Response(status_code=302, headers={"Location": out.location})
    headers = {"Retry-After": "60"} if out.status == 429 else None
    return Response(_TEXT.get(out.status, _TEXT[404]), status_code=out.status, media_type="text/plain",
                    headers=headers)
