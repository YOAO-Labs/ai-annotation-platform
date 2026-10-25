"""Portable guide-object effects and fixed JSON handoff helpers."""

from __future__ import annotations

import json
from typing import Any, Protocol
from uuid import UUID


class GuideAssetObjectPort(Protocol):
    """The four narrow object effects used by Workspace guide operations."""

    async def generate_upload_url(
        self, project_id: UUID, key: str, *, content_type: str
    ) -> str: ...

    async def verify_upload(self, project_id: UUID, key: str) -> int | None: ...

    async def delete_object(self, project_id: UUID, key: str) -> None: ...

    async def generate_download_url(self, project_id: UUID, key: str) -> str: ...


def guide_assets_ascii(value: Any) -> str:
    """Serialize the staged full guide_assets list for the fixed Native action."""
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
