"""Project-scoped annotation guide assets over authenticated transfer."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import get_args
from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.guide_asset_models import (
    GuideAssetContentType,
    GuideAssetDeleteResult,
    GuideAssetEntry,
)

ProjectIdLike = str | UUID


class GuideAssets:
    def __init__(self, http: HttpTransport):
        self._http = http

    def upload(
        self,
        project_id: ProjectIdLike,
        file_path: str | Path,
        *,
        file_name: str | None = None,
        content_type: GuideAssetContentType | None = None,
    ) -> GuideAssetEntry:
        """Upload verified bytes; the server owns storage naming and validation."""
        path = Path(file_path)
        name = file_name or path.name
        mime_type = content_type or mimetypes.guess_type(name)[0]
        if mime_type not in get_args(GuideAssetContentType):
            raise ValueError(f"unsupported guide asset content type: {mime_type!r}")
        response = self._http.request(
            "POST",
            f"/projects/{project_id}/guide-assets/upload",
            params={"file_name": name},
            content=path.read_bytes(),
            headers={"Content-Type": mime_type},
        )
        return GuideAssetEntry.model_validate(response.json())

    def content(self, project_id: ProjectIdLike, key: str) -> bytes:
        """Read authenticated guide bytes; no signed URL is issued."""
        response = self._http.request(
            "GET",
            f"/projects/{project_id}/guide-assets/content",
            params={"key": key},
        )
        return response.content

    def delete(self, project_id: ProjectIdLike, key: str) -> GuideAssetDeleteResult:
        response = self._http.request(
            "DELETE",
            f"/projects/{project_id}/guide-assets",
            params={"key": key},
        )
        return GuideAssetDeleteResult.model_validate(response.json())
