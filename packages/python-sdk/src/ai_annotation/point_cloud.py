"""Public REST resource for point-cloud manifests, cameras, and calibration."""

from __future__ import annotations

from urllib.parse import quote
from uuid import UUID

from ai_annotation._http import HttpTransport, _drop_none, _idempotency_headers
from ai_annotation.point_cloud_models import (
    CameraAnnotationMember,
    CameraAnnotationMemberCreate,
    CameraAnnotationMemberDelete,
    CameraAnnotationMemberList,
    CameraAnnotationMemberRestore,
    CameraAnnotationMemberUpdate,
    PointCloudManifest,
    SensorCalibrationHistory,
    SensorCalibrationUpdate,
    SensorCalibrationRevision,
)

IdLike = str | UUID


def _camera_role_segment(camera_role: str) -> str:
    if not camera_role.startswith("camera_"):
        raise ValueError("camera_role must start with 'camera_'")
    return quote(camera_role, safe="")


class PointCloud:
    """Task-scoped camera members and dataset-item calibration history."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def manifest(self, task_id: IdLike) -> PointCloudManifest:
        """Return a visible task's signed point-cloud/camera URLs and frame metadata.

        The server only serves lidar projects and checks task visibility. URLs
        expire according to ``expires_in`` (currently one hour). The SDK returns
        the manifest once and does not refresh or fetch the signed URLs.
        """
        response = self._http.request("GET", f"/tasks/{task_id}/point-cloud/manifest")
        return PointCloudManifest.model_validate(response.json())

    def camera_members(
        self,
        task_id: IdLike,
        *,
        scene_track_id: IdLike | None = None,
        projection_camera_role: str | None = None,
        include_inactive: bool = False,
    ) -> CameraAnnotationMemberList:
        """List visible camera members; optionally calculate a track projection.

        The API rechecks task visibility and verifies that any requested track and
        camera role belong to that task.
        ``relation_status`` compares the member's captured calibration digest with
        the current calibration. ``projection_camera_role`` only produces
        ``projected_bbox`` when a ``scene_track_id`` is supplied.
        """
        if projection_camera_role is not None:
            _camera_role_segment(projection_camera_role)
        params = _drop_none(
            {
                "scene_track_id": (
                    str(scene_track_id) if scene_track_id is not None else None
                ),
                "projection_camera_role": projection_camera_role,
                "include_inactive": True if include_inactive else None,
            }
        )
        response = self._http.request(
            "GET",
            f"/tasks/{task_id}/point-cloud/camera-members",
            params=params,
        )
        return CameraAnnotationMemberList.model_validate(response.json())

    def calibration_history(
        self, task_id: IdLike, camera_role: str
    ) -> SensorCalibrationHistory:
        """Read the linked camera's current and historical calibration snapshots.

        The API checks task visibility and resolves ``camera_role`` through that
        task's dataset-item links.
        Legacy metadata without materialized history appears as virtual revision 1
        with ``created_at=None``.
        """
        camera_role = _camera_role_segment(camera_role)
        response = self._http.request(
            "GET",
            f"/tasks/{task_id}/point-cloud/cameras/{camera_role}/calibration",
        )
        return SensorCalibrationHistory.model_validate(response.json())

    def create_camera_member(
        self, task_id: IdLike, body: CameraAnnotationMemberCreate
    ) -> CameraAnnotationMember:
        """Create one camera bbox for an active 3D source annotation.

        The caller needs ``annotations:write`` plus task annotation-write access
        (manager, assigned annotator, or permitted review adjustment). The API
        verifies source/task/SceneTrack/camera-link relationships and compares the
        supplied track revision and calibration revision/digest before writing.
        The source annotation is locked and checked active by the server; the
        request intentionally has no invented source-version or retry token.
        """
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/point-cloud/camera-members",
            json=body.model_dump(mode="json"),
        )
        return CameraAnnotationMember.model_validate(response.json())

    def update_camera_member(
        self,
        task_id: IdLike,
        member_id: IdLike,
        body: CameraAnnotationMemberUpdate,
    ) -> CameraAnnotationMember:
        """Update bbox/visibility with annotation, SceneTrack, and calibration CAS.

        Requires annotation-write scope and editable task access. A successful
        update increments both member version and SceneTrack revision.
        """
        response = self._http.request(
            "PATCH",
            f"/tasks/{task_id}/point-cloud/camera-members/{member_id}",
            json=body.model_dump(mode="json"),
        )
        return CameraAnnotationMember.model_validate(response.json())

    def delete_camera_member(
        self,
        task_id: IdLike,
        member_id: IdLike,
        body: CameraAnnotationMemberDelete,
    ) -> CameraAnnotationMember:
        """Soft-delete a member, checking its annotation version and track revision.

        Requires annotation-write scope and editable task access. Delete marks the
        member inactive and increments its version and SceneTrack revision.
        """
        response = self._http.request(
            "DELETE",
            f"/tasks/{task_id}/point-cloud/camera-members/{member_id}",
            json=body.model_dump(mode="json"),
        )
        return CameraAnnotationMember.model_validate(response.json())

    def restore_camera_member(
        self,
        task_id: IdLike,
        member_id: IdLike,
        body: CameraAnnotationMemberRestore,
    ) -> CameraAnnotationMember:
        """Restore a soft-deleted member after current track/calibration checks.

        Requires annotation-write scope and editable task access. The server rejects
        a duplicate active member for the same task, track, and camera role.
        """
        response = self._http.request(
            "POST",
            f"/tasks/{task_id}/point-cloud/camera-members/{member_id}/restore",
            json=body.model_dump(mode="json"),
        )
        return CameraAnnotationMember.model_validate(response.json())

    def update_calibration(
        self,
        task_id: IdLike,
        camera_role: str,
        body: SensorCalibrationUpdate,
        *,
        idempotency_key: IdLike | None = None,
    ) -> SensorCalibrationRevision:
        """Append a calibration revision using expected revision and digest CAS.

        Requires project-management authority. If the camera DatasetItem is linked
        across projects, only a super admin can update its shared calibration.
        The operation updates the DatasetItem metadata and durable calibration
        history; an identical calibration is a server-side no-op receipt. Writes
        are sent once, with conflicts returned to the caller unchanged.
        """
        camera_role = _camera_role_segment(camera_role)
        response = self._http.request(
            "PATCH",
            f"/tasks/{task_id}/point-cloud/cameras/{camera_role}/calibration",
            json=body.model_dump(mode="json"),
            headers=_idempotency_headers(idempotency_key),
        )
        return SensorCalibrationRevision.model_validate(response.json())
