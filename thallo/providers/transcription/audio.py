"""Reading a recording back from the tenant's folder, for the transcription providers.

Capture (`thallo/capture/service.py`) stores each recording as `<root>/<tenant_id>/capture-<sha256>`
through the API's media store, where <root> is the operator's THALLO_BRAND_ROOT. The request only
carries the sha256, so a provider finds the file by that name inside the tenant's own folder:

- the root comes from the operator's environment, never from connection settings;
- the tenant id must be a canonical id and the sha256 64 lower-case hex characters, so neither
  can climb out of the folder;
- nothing on the way may be a symlink and the file must be a regular file under the size cap;
- the caller re-hashes the bytes and refuses on any difference.
"""
from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

from thallo.contracts.ids import is_id

ROOT_ENV = "THALLO_BRAND_ROOT"
MAX_AUDIO_BYTES = 64 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{64}$")


class AudioNotFound(LookupError):
    pass


def tenant_audio_reader(root: str | Path):  # noqa: ANN201 - returns an AudioReader
    base = Path(root).resolve(strict=True)

    def read(tenant_id: str, sha256: str) -> bytes:
        if not isinstance(tenant_id, str) or not is_id(tenant_id) or not _SHA.match(sha256 or ""):
            raise AudioNotFound("not a recording reference")
        folder = base / tenant_id.lower()
        path = folder / f"capture-{sha256}"
        for p in (folder, path):
            try:
                mode = os.lstat(p).st_mode
            except OSError:
                raise AudioNotFound("recording not found") from None
            if stat.S_ISLNK(mode):
                raise AudioNotFound("recording path holds a symlink")
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise AudioNotFound("recording is not a regular file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as f:
            data = f.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise AudioNotFound("recording is too large")
        return data

    return read


def operator_audio_reader(environ: Mapping[str, str]):  # noqa: ANN201 - AudioReader | None
    root = environ.get(ROOT_ENV, "")
    if not root or not os.path.isabs(root):
        return None
    try:
        return tenant_audio_reader(root)
    except OSError:
        return None


__all__ = ["AudioNotFound", "ROOT_ENV", "operator_audio_reader", "tenant_audio_reader"]
