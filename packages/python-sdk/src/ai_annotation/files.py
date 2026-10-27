"""Task file upload coordination and authenticated original downloads."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from ai_annotation._http import HttpTransport, _idempotency_headers
from ai_annotation.file_models import (
    FileUploadComplete,
    FileUploadInit,
    FileUploadInitRequest,
    OriginalIdentity,
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

    def original_identity(self, task_id: IdLike) -> OriginalIdentity:
        """Read the verified original descriptor required for authenticated download."""
        resp = self._http.request("GET", f"/files/tasks/{task_id}/original-identity")
        return OriginalIdentity.model_validate(resp.json())

    def download_original(self, task_id: IdLike, destination: str | Path) -> Path:
        """Stream the full SHA-256 verified original over platform credentials."""
        identity = self.original_identity(task_id)
        return self._http.stream_download(
            f"/api/v1/files/tasks/{task_id}/original",
            Path(destination),
            params={
                "content_id": str(identity.content_id),
                "sha256": identity.sha256,
            },
            expected_sha256=identity.sha256,
            expected_size=identity.size_bytes,
        )

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
