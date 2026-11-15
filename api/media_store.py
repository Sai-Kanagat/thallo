"""Media bytes for the review deck, confined to the tenant's folder under THALLO_BRAND_ROOT.

One reader serves both the gate repository (what is hashed) and the media route (what the
reviewer sees), so the bytes on screen are the bytes approved.

Two URI shapes exist today:
  local:<tenant_id>/<name>     the blob store's shape (`thallo.store.adapters.blobs`)
  file:///abs/path             what the template renderer writes (<root>/<tenant_id>/renders/...)
Both resolve to a path that must sit strictly inside <root>/<tenant_id>/. Nothing on the way may
be a symlink, the target must be a regular file, and `..` is refused, so a media row of one tenant
can never point at another tenant's folder or anywhere else on disk. The client never sends a
path: the route takes a media id and reads the row under the session's tenant.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from urllib.parse import unquote, urlparse

from thallo.contracts.ids import is_id
from thallo.store.adapters.blobs import BlobError

LOCAL = "local:"
MAX_BYTES = 64 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class TenantMediaStore:
    """A `BlobStore` over the operator's brand root."""

    def __init__(self, root: str | Path | None) -> None:
        self.root = Path(root).resolve(strict=True) if root else None

    def _tenant_root(self, tenant_id: str) -> Path:
        if self.root is None:
            raise BlobError("no media root is configured")
        if not isinstance(tenant_id, str) or not is_id(tenant_id):
            raise BlobError("not a tenant id")
        return self.root / tenant_id.lower()

    def _confine(self, tenant_id: str, path: Path) -> Path:
        base = self._tenant_root(tenant_id)
        if not path.is_absolute() or ".." in path.parts:
            raise BlobError("media path is not allowed")
        try:
            rel = path.relative_to(base)
        except ValueError:
            raise BlobError("media is outside the tenant folder") from None
        if not rel.parts:
            raise BlobError("media path is not allowed")
        current = base
        for part in (None, *rel.parts):  # the tenant folder itself, then every component
            if part is not None:
                current = current / part
            try:
                mode = os.lstat(current).st_mode
            except OSError:
                raise BlobError("media not found") from None
            if stat.S_ISLNK(mode):
                raise BlobError("media path holds a symlink")
        if not stat.S_ISREG(os.lstat(current).st_mode):
            raise BlobError("media is not a regular file")
        return current

    def _path_for(self, tenant_id: str, uri: str) -> Path:
        if not isinstance(uri, str):
            raise BlobError("not a media uri")
        if uri.startswith(LOCAL):
            owner, _, name = uri[len(LOCAL):].partition("/")
            if owner != tenant_id.lower() or not _NAME.match(name):
                raise BlobError("media belongs to another tenant or has an unsafe name")
            return self._confine(tenant_id, self._tenant_root(tenant_id) / name)
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise BlobError("unsupported media uri")
        return self._confine(tenant_id, Path(unquote(parsed.path)))

    def read(self, tenant_id: str, uri: str) -> bytes:
        path = self._path_for(tenant_id, uri)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            raise BlobError("media not readable") from None
        with os.fdopen(fd, "rb") as f:
            data = f.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise BlobError("media is too large to serve")
        return data

    def write(self, tenant_id: str, name: str, data: bytes) -> str:
        """For tests and the demo: store bytes in the tenant folder and return a local: uri."""
        if not _NAME.match(name):
            raise BlobError("unsafe media name")
        base = self._tenant_root(tenant_id)
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        (base / name).write_bytes(bytes(data))
        return f"{LOCAL}{tenant_id.lower()}/{name}"


__all__ = ["MAX_BYTES", "TenantMediaStore"]
