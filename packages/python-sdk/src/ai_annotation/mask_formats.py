"""Mask-format catalogue, export preflight, and resumable import batches."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import httpx

from ai_annotation._http import HttpTransport
from ai_annotation.errors import APIStatusError, AAPError
from ai_annotation.mask_format_models import (
    MaskFormatDescriptor,
    MaskFormatExportPreflightRequest,
    MaskFormatExportPreflightResult,
    MaskFormatImportBatch,
    MaskFormatImportExecuteRequest,
    MaskFormatImportPreflightRequest,
    MaskFormatImportPreflightResult,
    MaskFormatUploadInitRequest,
    MaskFormatUploadInitResult,
)

IdLike = str | UUID


class MaskFormats:
    """Explicit mask-format REST operations. Import phases remain separate."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self, project_id: IdLike) -> list[MaskFormatDescriptor]:
        resp = self._http.request("GET", f"/projects/{project_id}/mask-formats")
        return [MaskFormatDescriptor.model_validate(item) for item in resp.json()]

    def export_preflight(
        self,
        project_id: IdLike,
        body: MaskFormatExportPreflightRequest,
    ) -> MaskFormatExportPreflightResult:
        payload = body.model_dump(mode="json", by_alias=True, exclude_unset=True)
        if body.scope is not None:
            payload["scope"]["selection"]["kind"] = body.scope.selection.kind
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/mask-formats/exports:preflight",
            json=payload,
        )
        return MaskFormatExportPreflightResult.model_validate(resp.json())

    def upload_init(
        self,
        project_id: IdLike,
        body: MaskFormatUploadInitRequest,
    ) -> MaskFormatUploadInitResult:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/mask-formats/imports:upload-init",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return MaskFormatUploadInitResult.model_validate(resp.json())

    def upload_bytes(
        self,
        upload_url: str,
        file_path: str | Path,
        content_type: str,
    ) -> None:
        """PUT staged bytes without platform credentials or URL-bearing errors."""
        try:
            self._http.put_presigned(upload_url, Path(file_path), content_type)
        except APIStatusError as exc:
            raise APIStatusError(
                exc.status_code, "presigned mask-format upload failed"
            ) from None
        except (httpx.HTTPError, httpx.InvalidURL):
            raise AAPError("presigned mask-format upload failed") from None

    def import_preflight(
        self,
        project_id: IdLike,
        body: MaskFormatImportPreflightRequest,
    ) -> MaskFormatImportPreflightResult:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/mask-formats/imports:preflight",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return MaskFormatImportPreflightResult.model_validate(resp.json())

    def execute_import(
        self,
        project_id: IdLike,
        body: MaskFormatImportExecuteRequest,
    ) -> MaskFormatImportBatch:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/mask-formats/imports",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return MaskFormatImportBatch.model_validate(resp.json())

    def get_import(
        self, project_id: IdLike, import_id: IdLike
    ) -> MaskFormatImportBatch:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/mask-formats/imports/{import_id}",
        )
        return MaskFormatImportBatch.model_validate(resp.json())

    def resume_import(
        self, project_id: IdLike, import_id: IdLike
    ) -> MaskFormatImportBatch:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/mask-formats/imports/{import_id}/resume",
        )
        return MaskFormatImportBatch.model_validate(resp.json())
