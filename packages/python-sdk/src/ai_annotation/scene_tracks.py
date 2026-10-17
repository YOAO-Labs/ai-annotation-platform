"""Scene timeline, track command, and compatibility track-operation APIs."""

from __future__ import annotations

from urllib.parse import quote
from uuid import UUID

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.scene_track_models import (
    LegacyTrackOperationExecuteRequest,
    LegacyTrackOperationPreview,
    LegacyTrackOperationRequest,
    LegacyTrackOperationResult,
    SceneTimeline,
    SceneTrackCommandExecuteRequest,
    SceneTrackCommandPreview,
    SceneTrackCommandRequest,
    SceneTrackCommandResult,
    SceneTrackDiagnosticReport,
    SceneTrackDetail,
    SceneTrackOperationList,
    SceneTrackRevertRequest,
    TrackOperationCandidates,
)

IdLike = str | UUID


def _check_track_id(track_id: str) -> None:
    if not isinstance(track_id, str) or not 1 <= len(track_id) <= 64:
        raise ValueError("track_id must contain 1 to 64 characters")


class SceneTracks:
    """Scene track APIs with explicit whole-scene and CAS boundaries.

    The server owns task visibility/editability across all affected scene members.
    Read/preview methods do not make partial-write assumptions or resolve conflicts.
    """

    def __init__(self, http: HttpTransport):
        self._http = http

    def scene_timeline(
        self,
        task_id: IdLike,
        *,
        start_frame: int,
        end_frame: int,
        track_id: str | None = None,
    ) -> SceneTimeline:
        """Read an inclusive timeline window of at most 200 frames.

        Unavailable scene tasks are represented in the response rather than omitted;
        callers must inspect each frame's ``state``.
        """
        if type(start_frame) is not int or start_frame < 0:
            raise ValueError("start_frame must be a non-negative integer")
        if type(end_frame) is not int or end_frame < start_frame:
            raise ValueError("end_frame must be an integer >= start_frame")
        if end_frame - start_frame + 1 > 200:
            raise ValueError("scene timeline windows cannot exceed 200 frames")
        if track_id is not None:
            _check_track_id(track_id)
        response = self._http.request(
            "GET",
            f"/tasks/{task_id}/scene-timeline",
            params=_drop_none(
                {
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "track_id": track_id,
                }
            ),
        )
        return SceneTimeline.model_validate(response.json())

    def get_track(self, task_id: IdLike, track_id: str) -> SceneTrackDetail:
        """Read a track only when all of its member tasks are visible."""
        _check_track_id(track_id)
        # Keep the literal route placeholder name for offline API discovery.
        track_id = quote(track_id, safe="")
        response = self._http.request(
            "GET", f"/tasks/{task_id}/scene-tracks/{track_id}"
        )
        return SceneTrackDetail.model_validate(response.json())

    def diagnostics(
        self, task_id: IdLike, *, limit: int = 200
    ) -> SceneTrackDiagnosticReport:
        """Diagnose a complete scene; the API requires visibility of every scene task."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        response = self._http.request(
            "GET",
            f"/tasks/{task_id}/scene-track-diagnostics",
            params={"limit": limit},
        )
        return SceneTrackDiagnosticReport.model_validate(response.json())

    def list_command_operations(
        self,
        task_id: IdLike,
        track_id: str,
        *,
        limit: int = 20,
    ) -> SceneTrackOperationList:
        """List visible command operations for one track (limit 1–100)."""
        _check_track_id(track_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        response = self._http.request(
            "GET",
            f"/tasks/{task_id}/scene-track-commands",
            params={"track_id": track_id, "limit": limit},
        )
        return SceneTrackOperationList.model_validate(response.json())

    def list_track_operation_candidates(
        self,
        task_id: IdLike,
        track_id: str,
        *,
        limit: int = 20,
    ) -> TrackOperationCandidates:
        """List merge candidates; this read requires task write authority."""
        _check_track_id(track_id)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer between 1 and 20")
        response = self._http.request(
            "GET",
            f"/tasks/{task_id}/track-operations/candidates",
            params={"track_id": track_id, "limit": limit},
        )
        return TrackOperationCandidates.model_validate(response.json())

    def preview_command(
        self, task_id: IdLike, body: SceneTrackCommandRequest
    ) -> SceneTrackCommandPreview:
        """Preview the preferred scene command; every affected task must be editable."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/scene-track-commands/preview",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return SceneTrackCommandPreview.model_validate(response.json())

    def execute_command(
        self, task_id: IdLike, body: SceneTrackCommandExecuteRequest
    ) -> SceneTrackCommandResult:
        """Execute the exact preview token and caller idempotency key once.

        A stale snapshot returns a conflict; this method never refreshes the preview
        or changes the idempotency key. The key can be reused only for the same body.
        """
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/scene-track-commands/execute",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return SceneTrackCommandResult.model_validate(response.json())

    def revert_command(
        self,
        task_id: IdLike,
        operation_id: IdLike,
        body: SceneTrackRevertRequest,
    ) -> SceneTrackCommandResult:
        """Revert the original operation ID with a caller-supplied idempotency key."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/scene-track-commands/{operation_id}/revert",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return SceneTrackCommandResult.model_validate(response.json())

    def preview_track_operation(
        self, task_id: IdLike, body: LegacyTrackOperationRequest
    ) -> LegacyTrackOperationPreview:
        """Preview the compatibility split/merge operation contract."""
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/track-operations/preview",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return LegacyTrackOperationPreview.model_validate(response.json())

    def execute_track_operation(
        self, task_id: IdLike, body: LegacyTrackOperationExecuteRequest
    ) -> LegacyTrackOperationResult:
        """Execute compatibility split/merge with its exact snapshot token.

        This legacy endpoint generates its own idempotency key. If a request times out,
        the caller cannot safely replay it; inspect the scene before deciding what next.
        """
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/track-operations",
            json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )
        return LegacyTrackOperationResult.model_validate(response.json())
