"""Public reads for uploaded avatar images."""

from __future__ import annotations

import re
from pathlib import Path

from ai_annotation._http import HttpTransport

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


class Avatars:
    def __init__(self, http: HttpTransport):
        self._http = http

    def download(self, token: str, dest: str | Path) -> Path:
        """Download a public uploaded avatar atomically; ``token`` is 32 hex chars."""
        if not _TOKEN_RE.fullmatch(token):
            raise ValueError("avatar token must be 32 lowercase hexadecimal characters")
        return self._http.stream_download(f"/api/v1/avatars/{token}", Path(dest))
