"""Task file upload coordination and signed file URLs."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from ai_annotation._http import HttpTransport, _idempotency_headers
from ai_annotation.file_models import (
    FileUploadComplete,
    FileUploadInit,
    FileUploadInitRequest,
    TaskFileUrl,
    ThumbnailBackfill,
)

IdLike = str | UUID


class Files:
    def __init__(self, http: HttpTransport):
        self._http = http

    def upload_init(self, body: FileUploadInitRequest) -> FileUploadInit:
        """Create an uploading task and receive its short-lived PUT URL."""
        resp = self._http.request(
            "POST",
            "/files/upload-init",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return FileUploadInit.model_validate(resp.json())

    def upload_complete(self, task_id: IdLike) -> FileUploadComplete:
        resp = self._http.request("POST", f"/files/upload-complete/{task_id}")
        return FileUploadComplete.model_validate(resp.json())

    def upload_content(
        self,
        upload: FileUploadInit,
        file_path: str | Path,
        *,
        content_type: str,
    ) -> None:
        """Stream to the init URL without API credentials; completion is separate."""
        if not upload.upload_url.startswith(("http://", "https://")):
            raise ValueError("upload_url must be an absolute HTTP(S) URL")
        self._http.put_presigned(upload.upload_url, Path(file_path), content_type)

    def file_url(self, task_id: IdLike) -> TaskFileUrl:
        resp = self._http.request("GET", f"/files/tasks/{task_id}/file-url")
        return TaskFileUrl.model_validate(resp.json())

    def backfill_thumbnails(
        self, project_id: IdLike, *, idempotency_key: IdLike | None = None
    ) -> ThumbnailBackfill:
        """Accept a project thumbnail job; a saved UUID replays its receipt."""
        resp = self._http.request(
            "POST",
            f"/files/projects/{project_id}/backfill-thumbnails",
            headers=_idempotency_headers(
                uuid4() if idempotency_key is None else idempotency_key
            ),
        )
        return ThumbnailBackfill.model_validate(resp.json())
