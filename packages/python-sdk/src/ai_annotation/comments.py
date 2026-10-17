"""Typed access to annotation comments and task discussion feeds."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.comment_models import (
    AnnotationComment,
    AnnotationCommentCounts,
    AnnotationCommentCreate,
    AnnotationCommentPage,
    AnnotationCommentUpdate,
    CommentAttachmentInput,
    CommentAttachmentDownloadURL,
    CommentAttachmentUpload,
    CommentAttachmentUploadRequest,
    TaskDiscussionPage,
)
from ai_annotation.errors import AAPError, APIStatusError

IdLike = str | UUID


def _redirect_location(response: httpx.Response) -> str:
    """Validate a redirect without putting the signed URL in error text."""
    if response.status_code != 302:
        raise APIStatusError(
            response.status_code,
            "comment attachment endpoint must return HTTP 302",
        )
    location = response.headers.get("location")
    try:
        parsed = urlsplit(location or "")
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        valid = False
    if not valid:
        raise AAPError(
            "comment attachment redirect must contain an absolute HTTP(S) URL"
        )
    return location  # type: ignore[return-value]


class Comments:
    """Annotation-comment CRUD, keyset pages, task feeds and file transfer.

    Comment and task visibility is decided by the API from actual annotation/task
    membership and assignment state. This client deliberately does no local filtering.
    """

    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self, annotation_id: IdLike) -> list[AnnotationComment]:
        """Return the legacy full array; prefer :meth:`page` for large discussions."""
        response = self._http.request("GET", f"/annotations/{annotation_id}/comments")
        return [AnnotationComment.model_validate(item) for item in response.json()]

    def page(
        self,
        annotation_id: IdLike,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> AnnotationCommentPage:
        response = self._http.request(
            "GET",
            f"/annotations/{annotation_id}/comments/page",
            params=_drop_none({"limit": limit, "cursor": cursor}),
        )
        return AnnotationCommentPage.model_validate(response.json())

    def task_page(
        self,
        task_id: IdLike,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> AnnotationCommentPage:
        """Keyset page of active comments from every annotation on a task."""
        response = self._http.request(
            "GET",
            f"/tasks/{task_id}/comments/page",
            params=_drop_none({"limit": limit, "cursor": cursor}),
        )
        return AnnotationCommentPage.model_validate(response.json())

    def discussion_page(
        self,
        task_id: IdLike,
        *,
        scope: Literal["all", "task", "annotation"] = "all",
        annotation_id: IdLike | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> TaskDiscussionPage:
        """Keyset page of the mixed feedback/comment timeline for a visible task."""
        response = self._http.request(
            "GET",
            f"/tasks/{task_id}/discussion/page",
            params=_drop_none(
                {
                    "scope": scope,
                    "annotation_id": (
                        str(annotation_id) if annotation_id is not None else None
                    ),
                    "limit": limit,
                    "cursor": cursor,
                }
            ),
        )
        return TaskDiscussionPage.model_validate(response.json())

    def annotation_counts(self, task_id: IdLike) -> AnnotationCommentCounts:
        """Sparse active-comment counts keyed by annotation UUID for a visible task."""
        response = self._http.request(
            "GET", f"/tasks/{task_id}/discussion/annotation-counts"
        )
        return AnnotationCommentCounts.model_validate(response.json())

    def create(
        self, annotation_id: IdLike, request: AnnotationCommentCreate
    ) -> AnnotationComment:
        payload = request.model_dump(mode="json", by_alias=True, exclude_unset=True)
        response = self._http.request(
            "POST", f"/annotations/{annotation_id}/comments", json=payload
        )
        return AnnotationComment.model_validate(response.json())

    def update(
        self, comment_id: IdLike, request: AnnotationCommentUpdate
    ) -> AnnotationComment:
        response = self._http.request(
            "PATCH",
            f"/comments/{comment_id}",
            json=request.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return AnnotationComment.model_validate(response.json())

    def delete(self, comment_id: IdLike) -> None:
        self._http.request("DELETE", f"/comments/{comment_id}")

    def init_attachment_upload(
        self,
        annotation_id: IdLike,
        request: CommentAttachmentUploadRequest,
    ) -> CommentAttachmentUpload:
        response = self._http.request(
            "POST",
            f"/annotations/{annotation_id}/comment-attachments/upload-init",
            json=request.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return CommentAttachmentUpload.model_validate(response.json())

    def upload_attachment(
        self, annotation_id: IdLike, path: str | Path
    ) -> CommentAttachmentInput:
        """Upload one file and return comment-ready metadata.

        The API signs the PUT but has no completion route; the returned metadata can be
        included in ``AnnotationCommentCreate.attachments``. A rejected comment may
        leave an unattached object for storage lifecycle cleanup.
        """
        file_path = Path(path)
        try:
            size = file_path.stat().st_size
        except OSError:
            raise AAPError("comment attachment metadata is unavailable") from None
        content_type = (
            mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        )
        upload = self.init_attachment_upload(
            annotation_id,
            CommentAttachmentUploadRequest(
                file_name=file_path.name,
                content_type=content_type,
            ),
        )
        try:
            self._http.put_presigned(upload.upload_url, file_path, content_type)
        except APIStatusError as exc:
            raise APIStatusError(
                exc.status_code, "comment attachment upload failed"
            ) from None
        except (httpx.HTTPError, httpx.InvalidURL):
            raise AAPError("comment attachment upload failed") from None
        return CommentAttachmentInput(
            storage_key=upload.storage_key,
            file_name=file_path.name,
            mime_type=content_type,
            size=size,
        )

    def attachment_download_url(
        self, annotation_id: IdLike, key: str
    ) -> CommentAttachmentDownloadURL:
        """Return the signed URL using the API's explicit JSON response variant."""
        response = self._http.request(
            "GET",
            f"/annotations/{annotation_id}/comment-attachments/download",
            params={"key": key, "as_json": True},
        )
        return CommentAttachmentDownloadURL.model_validate(response.json())

    def download_attachment(
        self, annotation_id: IdLike, key: str, destination: str | Path
    ) -> Path:
        """Follow the API's exact 302 with a credential-free streaming download."""
        response = self._http.request(
            "GET",
            f"/annotations/{annotation_id}/comment-attachments/download",
            params={"key": key},
        )
        download_url = _redirect_location(response)
        try:
            return self._http.stream_download(download_url, Path(destination))
        except APIStatusError as exc:
            raise APIStatusError(
                exc.status_code, "comment attachment download failed"
            ) from None
        except (httpx.HTTPError, httpx.InvalidURL):
            raise AAPError("comment attachment download failed") from None


__all__ = ["Comments"]
