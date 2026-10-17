"""Bug-report REST operations and credential-isolated attachment transfer."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.bug_report_models import (
    BugComment,
    BugCommentCreate,
    BugReport,
    BugReportClusters,
    BugReportCreate,
    BugReportDetail,
    BugReportPage,
    BugReportUpdate,
    BugScreenshot,
    BugScreenshotUpload,
    BugScreenshotUploadRequest,
)
from ai_annotation.errors import AAPError, APIStatusError

IdLike = str | UUID
_ALLOWED_SCREENSHOT_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}


def _redirect_location(response: httpx.Response) -> str:
    """Validate a redirect without exposing its signed URL in errors."""
    if response.status_code != 302:
        raise APIStatusError(
            response.status_code, "bug attachment endpoint must return HTTP 302"
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
        raise AAPError("bug attachment redirect must contain an absolute HTTP(S) URL")
    return location  # type: ignore[return-value]


class BugReports:
    """Bug-report operations.

    Report ``project_id`` and ``task_id`` fields are diagnostic context only. The API
    controls reporter access and grants list/management operations by global platform
    role; this resource does not infer permissions from project membership.
    """

    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self,
        *,
        status: str | None = None,
        severity: str | None = None,
        route: str | None = None,
        limit: int = 50,
        offset: int = 0,
        format: Literal["markdown"] | None = None,
    ) -> BugReportPage | str:
        """Global manager list; markdown output is an intentional plain-text variant."""
        response = self._http.request(
            "GET",
            "/bug_reports",
            params=_drop_none(
                {
                    "status": status,
                    "severity": severity,
                    "route": route,
                    "limit": limit,
                    "offset": offset,
                    "format": format,
                }
            ),
        )
        if response.headers.get("content-type", "").lower().startswith("text/markdown"):
            return response.text
        return BugReportPage.model_validate(response.json())

    def list_mine(self, *, limit: int = 50, offset: int = 0) -> BugReportPage:
        response = self._http.request(
            "GET", "/bug_reports/mine", params={"limit": limit, "offset": offset}
        )
        return BugReportPage.model_validate(response.json())

    def create(self, request: BugReportCreate) -> BugReport:
        response = self._http.request(
            "POST",
            "/bug_reports",
            json=request.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return BugReport.model_validate(response.json())

    def get(self, report_id: IdLike) -> BugReportDetail:
        response = self._http.request("GET", f"/bug_reports/{report_id}")
        return BugReportDetail.model_validate(response.json())

    def update(self, report_id: IdLike, request: BugReportUpdate) -> BugReport:
        """PATCH a report; reporters can edit content, managers own admin fields.

        ``exclude_unset`` preserves explicit nulls for fields whose API contract allows
        clearing, while the server remains authoritative for the caller's role.
        """
        response = self._http.request(
            "PATCH",
            f"/bug_reports/{report_id}",
            json=request.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return BugReport.model_validate(response.json())

    def delete(self, report_id: IdLike) -> None:
        self._http.request("DELETE", f"/bug_reports/{report_id}")

    def cluster(self) -> BugReportClusters:
        """Compute duplicate suggestions; this operation does not mutate reports."""
        response = self._http.request("POST", "/bug_reports/cluster")
        return BugReportClusters.model_validate(response.json())

    def add_comment(self, report_id: IdLike, request: BugCommentCreate) -> BugComment:
        response = self._http.request(
            "POST",
            f"/bug_reports/{report_id}/comments",
            json=request.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return BugComment.model_validate(response.json())

    def init_screenshot_upload(
        self,
        request: BugScreenshotUploadRequest | None = None,
        *,
        file_name: str = "screenshot.png",
        content_type: Literal["image/png", "image/jpeg", "image/webp"] = "image/png",
    ) -> BugScreenshotUpload:
        upload_request = request or BugScreenshotUploadRequest(
            file_name=file_name,
            content_type=content_type,
        )
        response = self._http.request(
            "POST",
            "/bug_reports/screenshot/upload-init",
            json=upload_request.model_dump(
                mode="json", by_alias=True, exclude_unset=True
            ),
        )
        return BugScreenshotUpload.model_validate(response.json())

    def upload_screenshot(self, path: str | Path) -> BugScreenshot:
        """Upload a local screenshot and return metadata safe to attach to a report."""
        file_path = Path(path)
        content_type = mimetypes.guess_type(file_path.name)[0] or "image/png"
        if content_type not in _ALLOWED_SCREENSHOT_MIME_TYPES:
            raise AAPError("bug screenshots must be PNG, JPEG or WebP")
        try:
            size = file_path.stat().st_size
        except OSError:
            raise AAPError("screenshot metadata is unavailable") from None
        if size > 10 * 1024 * 1024:
            raise AAPError("bug screenshots must be at most 10 MiB")
        upload = self.init_screenshot_upload(
            file_name=file_path.name,
            content_type=content_type,  # type: ignore[arg-type]
        )
        try:
            self._http.put_presigned(upload.upload_url, file_path, content_type)
        except APIStatusError as exc:
            raise APIStatusError(
                exc.status_code, "bug screenshot upload failed"
            ) from None
        except (httpx.HTTPError, httpx.InvalidURL):
            raise AAPError("bug screenshot upload failed") from None
        return BugScreenshot(
            storage_key=upload.storage_key,
            file_name=file_path.name,
            mime_type=content_type,
            size=size,
        )

    def download_attachment(
        self, report_id: IdLike, key: str, destination: str | Path
    ) -> Path:
        """Follow the API's exact 302 with a bare client, never platform credentials."""
        response = self._http.request(
            "GET",
            f"/bug_reports/{report_id}/attachments/download",
            params={"key": key},
        )
        download_url = _redirect_location(response)
        try:
            return self._http.stream_download(download_url, Path(destination))
        except APIStatusError as exc:
            raise APIStatusError(
                exc.status_code, "bug attachment download failed"
            ) from None
        except (httpx.HTTPError, httpx.InvalidURL):
            raise AAPError("bug attachment download failed") from None


__all__ = ["BugReports"]
