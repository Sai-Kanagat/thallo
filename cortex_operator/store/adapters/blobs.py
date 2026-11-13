"""Where media bytes live. The gate hashes the bytes it reads here, never a stored hash.

`BlobStore` is the small Protocol the gate adapter reads through. `LocalBlobStore` keeps files on
local disk under one root, one folder per tenant, for tests and the demo. A real object store
(Supabase Storage, S3) implements the same two calls in a later package.

URIs look like `local:<tenant_id>/<name>`. A read names the tenant it is reading for, and a URI
of another tenant, or one that would leave the root, is refused.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from cortex_operator.contracts.ids import is_id

SCHEME = "local:"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class BlobError(ValueError):
    pass


class BlobStore(Protocol):
    def write(self, tenant_id: str, name: str, data: bytes) -> str:
        """Store bytes and return the storage_uri to put on the media row. Overwrites."""
        ...

    def read(self, tenant_id: str, uri: str) -> bytes:
        """The bytes stored at `uri` for this tenant, read now. Raises if missing or foreign."""
        ...


class LocalBlobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, tenant_id: str, name: str) -> Path:
        if not is_id(tenant_id):
            raise BlobError("not a tenant id")
        if not _NAME.match(name) or ".." in name:
            raise BlobError(f"not a safe blob name: {name!r}")
        path = (self.root / tenant_id.lower() / name).resolve()
        if path.parent != self.root / tenant_id.lower():
            raise BlobError("blob path leaves the tenant folder")
        return path

    def write(self, tenant_id: str, name: str, data: bytes) -> str:
        path = self._path(tenant_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(data))
        return f"{SCHEME}{tenant_id.lower()}/{name}"

    def read(self, tenant_id: str, uri: str) -> bytes:
        if not uri.startswith(SCHEME):
            raise BlobError("not a local blob uri")
        owner, _, name = uri[len(SCHEME):].partition("/")
        if owner != tenant_id.lower():
            raise BlobError("blob belongs to another tenant")
        path = self._path(tenant_id, name)
        if not path.is_file():
            raise BlobError("blob not found")
        return path.read_bytes()


__all__ = ["SCHEME", "BlobError", "BlobStore", "LocalBlobStore"]
