"""Industry claim packs (WP-33, module 36): manufacturer facts shared across clients.

- `pack`: the closed file schema, canonical bytes, Ed25519 signing, privacy rules, `export_pack`
- `importer`: `import_pack` (signature, expiry, rules, fresh candidates) and `verify_pack_claim`
- `persist`: write an import in one transaction and read the provenance rows (migration 0041)
"""
from thallo.extensions.packs.importer import PackImport, PackLink, import_pack, verify_pack_claim
from thallo.extensions.packs.pack import (
    ALLOWED_KINDS,
    ALLOWED_SOURCES,
    Ed25519Signer,
    PackBody,
    PackError,
    PackItem,
    PackSource,
    SignedPack,
    TrustedKeys,
    export_pack,
    open_pack,
    to_bytes,
)
from thallo.extensions.packs.persist import imported_digests, link_for, links, write_import

__all__ = ["ALLOWED_KINDS", "ALLOWED_SOURCES", "Ed25519Signer", "PackBody", "PackError", "PackImport", "PackItem",
           "PackLink", "PackSource", "SignedPack", "TrustedKeys", "export_pack", "import_pack", "imported_digests",
           "link_for", "links", "open_pack", "to_bytes", "verify_pack_claim", "write_import"]
