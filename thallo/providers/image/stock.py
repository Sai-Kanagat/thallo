"""Stock photos from Pexels, with the licence recorded on every file.

A search for the request's prompt (the query), one photo, downloaded and stored in the tenant's
folder. The licence (Pexels License), photographer, photographer page and source page go into the
provenance settings, which ingest copies to `media_file.licence` and `media_file.settings`. Not
behind the generation switch: nothing is generated. But a stock photo is not the client's own:
`ingest_provenance` labels it GENERATED (anything not template_render or manual), so it carries a
disclosure label and the check stack treats it like generated imagery. It is never a picture of
the client's product, team or premises.

    GET {base}/search?query=..&per_page=1&orientation=portrait|landscape|square
        Authorization: <key>          (Pexels takes the bare key, no scheme)
    reply {"photos": [{"id", "width", "height", "url", "photographer", "photographer_url", "alt",
                       "src": {"original", "large2x", "large", ...}}]}
    GET {base}/curated?per_page=1    health

Operator configuration, in the environment only:
    THALLO_STOCK_ORIGINS        the https origins allowed: the API and the image host
                                (https://api.pexels.com,https://images.pexels.com)
    THALLO_STOCK_ORIGINS_BASE   https://api.pexels.com/v1
    THALLO_BRAND_ROOT           where the tenant's output folder is
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.connections.secret import Secret
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode, MediaKind, MediaProvenance
from thallo.providers.image.http_media import (
    HttpMediaProvider,
    MediaCall,
    MediaProviderError,
    MediaSettings,
    MediaTransportFn,
    check_reply,
    image_kind,
)
from thallo.providers.text.base import Auth

NAME = "stock"
LICENCE = "Pexels License (https://www.pexels.com/license/)"


class StockPhotos(HttpMediaProvider):
    capability = Capability.IMAGE
    name = NAME
    mode = ConnectionMode.HOSTED
    generates = False
    needs_model = False
    origins_env = "THALLO_STOCK_ORIGINS"
    licence_note = LICENCE

    def auth(self, secret: Secret) -> Auth:
        return Auth("authorization", secret, "")

    def health_path(self) -> str:
        return "/curated?per_page=1"

    def serves(self, request: c.Request) -> str | None:
        assert isinstance(request, c.ImageRequest)
        if request.purpose not in ("background", "illustration"):
            return "stock photos are only backgrounds or illustrations"
        if not request.prompt or not request.prompt.strip():
            return "a stock search needs a query"
        if request.allow_generated_people:
            return "a stock photo of a person is never shown as the client's customer or team"
        return None

    def produce(self, request: c.Request, auth: Auth | None) -> tuple[bytes, str, str, dict[str, Any], Decimal]:
        assert isinstance(request, c.ImageRequest) and self.settings is not None and request.prompt
        ratio = request.width / request.height
        orientation = "square" if 0.9 <= ratio <= 1.1 else ("landscape" if ratio > 1 else "portrait")
        query = urlencode({"query": " ".join(request.prompt.split())[:100], "per_page": 1,
                           "orientation": orientation})
        reply = self._send(MediaCall("GET", f"{self.base_url()}/search?{query}", {}, None, auth,
                                     self.settings.timeout_s))
        check_reply(reply, NAME)
        b = reply.body
        photos = b.get("photos") if isinstance(b, dict) else None
        if not isinstance(photos, list):
            raise MediaProviderError("stock returned an unexpected response shape (photos)")
        if not photos:
            raise MediaProviderError("stock found no photo for that query")
        p = photos[0]
        src = p.get("src") if isinstance(p, dict) else None
        url = (src.get("large2x") or src.get("original")) if isinstance(src, dict) else None
        if not isinstance(url, str) or not isinstance(p.get("id"), int):
            raise MediaProviderError("stock returned an unexpected response shape (src)")
        image = self._send(MediaCall("GET", url, {}, None, None, self.settings.timeout_s))  # no key to the CDN
        check_reply(image, NAME)
        kind = image_kind(image.raw)
        if kind is None:
            raise MediaProviderError("stock returned bytes that are not an image")
        settings = {"licence": LICENCE, "external_id": str(p["id"]), "source_page": _text(p.get("url")),
                    "photographer": _text(p.get("photographer")),
                    "photographer_page": _text(p.get("photographer_url")), "query": request.prompt,
                    "vendor_alt": _text(p.get("alt"))}
        return image.raw, kind[0], kind[1], settings, Decimal(1)

    def media_result(self, request: c.Request, sha: str, mime: str, path: Path) -> c.MediaResult:
        assert isinstance(request, c.ImageRequest)
        return c.MediaResult(kind=MediaKind.IMAGE, sha256=sha, mime=mime, storage_uri=path.as_uri(),
                             provenance=MediaProvenance.GENERATED, width=request.width, height=request.height,
                             disclosure_required=True)


def _text(value: Any) -> str | None:
    return value[:300] if isinstance(value, str) else None


def create(ctx: ProviderContext, transport: MediaTransportFn | None = None,
           environ: Mapping[str, str] | None = None) -> StockPhotos:
    return StockPhotos(ctx, transport=transport, environ=environ)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.IMAGE, NAME, ConnectionMode.HOSTED, create, settings_schema=MediaSettings)
