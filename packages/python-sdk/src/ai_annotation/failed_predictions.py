"""Project-scoped management operations for failed prediction records."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.failed_prediction_models import (
    FailedPredictionDismissReceipt,
    FailedPredictionPage,
    FailedPredictionRetryReceipt,
)

IdLike = str | UUID


class FailedPredictions:
    """Failed prediction queue; the server enforces platform/project authority."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        include_dismissed: bool = False,
    ) -> FailedPredictionPage:
        """List failed predictions (super admin globally, project admin for owned projects)."""
        if type(page) is not int or page < 1:
            raise ValueError("page must be a positive integer")
        if type(page_size) is not int or not 1 <= page_size <= 200:
            raise ValueError("page_size must be an integer between 1 and 200")
        resp = self._http.request(
            "GET",
            "/admin/failed-predictions",
            params={
                "page": page,
                "page_size": page_size,
                "include_dismissed": include_dismissed,
            },
        )
        return FailedPredictionPage.model_validate(resp.json())

    def retry(self, failed_id: IdLike) -> FailedPredictionRetryReceipt:
        """Queue one model retry (HTTP 202); never wait or retry automatically.

        The target must be in a project managed by the caller, not dismissed,
        and below the server's retry limit. A repeated queue attempt returns 409.
        """
        resp = self._http.request(
            "POST", f"/admin/failed-predictions/{failed_id}/retry"
        )
        return FailedPredictionRetryReceipt.model_validate(resp.json())

    def dismiss(self, failed_id: IdLike) -> FailedPredictionDismissReceipt:
        """Hide one failed prediction from the default queue; operation is idempotent."""
        resp = self._http.request(
            "POST", f"/admin/failed-predictions/{failed_id}/dismiss"
        )
        return FailedPredictionDismissReceipt.model_validate(resp.json())

    def restore(self, failed_id: IdLike) -> FailedPredictionDismissReceipt:
        """Restore a dismissed record to the default failed prediction queue."""
        resp = self._http.request(
            "POST", f"/admin/failed-predictions/{failed_id}/restore"
        )
        return FailedPredictionDismissReceipt.model_validate(resp.json())
