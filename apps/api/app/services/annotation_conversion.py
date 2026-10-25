from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.services.project_access import get_workspace_project
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.labeling_core_read_relations import lock_task_for_core_read
from app.db.models.user import User
from app.schemas.annotation import AnnotationOut
from app.schemas.annotation_conversion import (
    AnnotationConversionDryRunRequest,
    AnnotationConversionDryRunResponse,
    AnnotationConversionExecuteRequest,
    AnnotationConversionExecuteResponse,
    AnnotationConversionItemReport,
    AnnotationConversionLineageOut,
    AnnotationConversionSummary,
)
from app.services.identity_contracts import IdentityActorFacts
from app.services.annotation import AnnotationService
from app.services.annotation_evidence import record_annotation_actor
from app.services.annotation_propagation import _new_track_id
from app.services.annotation_track_identity import prepare_compact_track_identity
from app.services.audit import AuditAction, AuditService
from app.services.mask_conversion import (
    ConversionMetrics,
    mask_to_bbox_conversion,
    mask_to_region_conversion,
    region_to_mask_conversion,
)
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    assert_raster_mask_write_enabled,
    build_rle_reference,
    load_coco_rle,
    lock_raster_mask_references,
    prepare_mask_payload_for_write,
    reserve_raster_mask_upload,
    store_coco_rle,
)
from app.services.project_access import is_privileged_for_project
from app.services.video_collaboration import (
    assert_task_lock_for_legacy_video,
    collaboration_config,
    heartbeat_task_lock_for_legacy_video,
    segment_work_bounds,
)
from app.services.task_lock import TaskLockConflictError
from app.services.video_tracks import (
    frame_is_outside,
    normalize_outside_ranges,
    resolve_track_at_frame,
    sorted_keyframes,
)
from app.utils.raster_mask_rle import MAX_DENSE_MASK_PIXELS
from aap_labeling.annotation_transfer_contract import (
    ANNOTATION_TRANSFER_PROTOCOL,
    ApplyConversion,
    ConversionAction,
    ConversionSourceFence,
    CreateConversionPlan,
    ExpireConversionPlans,
    FinalizeConversion,
    LockConversionPlan,
    ReadConversionPlan,
    ReadConversionReplay,
    ReadConversionSources,
    ReadConversionVideoSegments,
)
from app.services.labeling_annotation_transfer import (
    AnnotationTransferRejected,
    annotation_transfer_command,
)

CONVERSION_PLAN_TTL_SECONDS = 600
MAX_CONVERSION_MASK_OBJECTS_PER_TASK = 256
MAX_CONVERSION_RASTER_PIXELS = 32 * 1024 * 1024


async def expire_conversion_plans(
    db: AsyncSession, *, now: datetime | None = None
) -> int:
    """Delete conversion plans whose own expiration deadline has elapsed."""
    data = await annotation_transfer_command(
        db,
        ExpireConversionPlans(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="expire_conversion_plans",
            now=now or datetime.now(timezone.utc),
        ),
    )
    assert data.expired_count is not None
    return data.expired_count


class AnnotationConversionError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        reason: str,
        message: str,
        **detail: Any,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"reason": reason, "message": message, **detail}


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _without_track_ids(value: Any) -> Any:
    """Remove generated track identities from a plan before comparing it."""
    if isinstance(value, dict):
        return {
            key: _without_track_ids(child)
            for key, child in value.items()
            if key != "track_id"
        }
    if isinstance(value, list):
        return [_without_track_ids(child) for child in value]
    return value


def _annotation_snapshot(annotation: Annotation) -> dict[str, Any]:
    return {
        "id": str(annotation.id),
        "version": int(annotation.version or 1),
        "annotation_type": annotation.annotation_type,
        "tool_unit_id": annotation.tool_unit_id,
        "class_name": annotation.class_name,
        "geometry": annotation.geometry,
        "source": annotation.source,
        "confidence": annotation.confidence,
        "attributes": annotation.attributes or {},
        "attributes_meta": annotation.attributes_meta or {},
        "z_order": annotation.z_order,
        "is_locked": annotation.is_locked,
        "is_active": annotation.is_active,
    }


def _source_record(annotation: Annotation) -> dict[str, Any]:
    snapshot = _annotation_snapshot(annotation)
    return {
        "id": str(annotation.id),
        "version": int(annotation.version or 1),
        "digest": _canonical_digest(snapshot),
    }


def _target_tool_unit(geometry: dict[str, Any]) -> str:
    return "bbox" if geometry.get("type") in {"bbox", "video_bbox"} else "region"


def _aggregate_metrics(metrics: list[ConversionMetrics]) -> ConversionMetrics:
    if not metrics:
        raise ValueError("conversion must produce at least one result")
    reasons = tuple(
        dict.fromkeys(reason for item in metrics for reason in item.reasons)
    )
    return ConversionMetrics(
        source_area_pixels=sum(item.source_area_pixels for item in metrics),
        target_area_pixels=sum(item.target_area_pixels for item in metrics),
        changed_pixels=sum(item.changed_pixels for item in metrics),
        source_components=sum(item.source_components for item in metrics),
        target_components=sum(item.target_components for item in metrics),
        source_holes=sum(item.source_holes for item in metrics),
        target_holes=sum(item.target_holes for item in metrics),
        source_vertices=sum(item.source_vertices for item in metrics),
        target_vertices=sum(item.target_vertices for item in metrics),
        lossy=any(item.lossy for item in metrics),
        reasons=reasons,
    )


def _item_report(
    annotation: Annotation,
    *,
    target_type: str,
    frame_indexes: list[int],
    metrics: list[ConversionMetrics],
    materialized_held_frames: int = 0,
) -> AnnotationConversionItemReport:
    combined = _aggregate_metrics(metrics)
    return AnnotationConversionItemReport(
        source_annotation_id=annotation.id,
        source_type=str((annotation.geometry or {}).get("type") or ""),
        target_type=target_type,
        source_version=int(annotation.version or 1),
        frame_indexes=frame_indexes,
        result_count=1,
        materialized_held_frames=materialized_held_frames,
        **combined.as_dict(),
    )


def _summary(
    items: list[AnnotationConversionItemReport],
) -> AnnotationConversionSummary:
    return AnnotationConversionSummary(
        source_count=len(items),
        result_count=sum(item.result_count for item in items),
        materialized_held_frames=sum(item.materialized_held_frames for item in items),
        lossy_count=sum(1 for item in items if item.lossy),
    )


def _region_from_video_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "polygon",
        "points": geometry.get("points") or [],
        **({"holes": geometry.get("holes")} if geometry.get("holes") else {}),
    }


def _update_action(
    source: Annotation,
    geometry: dict[str, Any],
    *,
    frame_index: int | None = None,
) -> dict[str, Any]:
    return {
        "kind": "update",
        "source_id": str(source.id),
        "geometry": geometry,
        "annotation_type": str(geometry.get("type")),
        "tool_unit_id": _target_tool_unit(geometry),
        "frame_index": frame_index,
    }


def _create_action(
    source: Annotation,
    geometry: dict[str, Any],
    *,
    frame_index: int | None = None,
) -> dict[str, Any]:
    return {
        "kind": "create",
        "source_id": str(source.id),
        "geometry": geometry,
        "annotation_type": str(geometry.get("type")),
        "tool_unit_id": _target_tool_unit(geometry),
        "frame_index": frame_index,
    }


def _suppress_track_frame_action(
    source: Annotation, frame_index: int
) -> dict[str, Any]:
    geometry = dict(source.geometry or {})
    keyframes = [dict(item) for item in geometry.get("keyframes") or []]
    remaining = [
        item for item in keyframes if int(item.get("frame_index", -1)) != frame_index
    ]
    if not remaining:
        return {
            "kind": "deactivate",
            "source_id": str(source.id),
            "frame_index": frame_index,
        }
    outside = normalize_outside_ranges(
        [
            *(geometry.get("outside") or []),
            {"from": frame_index, "to": frame_index, "source": "manual"},
        ]
    )
    return _update_action(
        source,
        {**geometry, "keyframes": remaining, "outside": outside},
        frame_index=frame_index,
    )


class AnnotationConversionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._generated_rles: dict[str, dict[str, Any]] = {}

    async def _transfer_command(self, command):
        try:
            return await annotation_transfer_command(self.db, command)
        except AnnotationTransferRejected as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            raise AnnotationConversionError(
                status_code=exc.status_code,
                reason=exc.reason,
                message=str(detail.get("message") or exc.reason),
                **{key: value for key, value in detail.items() if key != "message"},
            ) from exc

    async def _task_item_and_dimensions(self, task: Task) -> tuple[None, int, int, str]:
        if task.dataset_item_id is None:
            raise AnnotationConversionError(
                status_code=409,
                reason="source_media_missing",
                message="conversion requires a primary dataset item",
            )
        from app.services.assets_media_summary import AssetsTaskMediaSummaryReader

        reader = AssetsTaskMediaSummaryReader(self.db)
        if task.file_type == "video":
            video_summary = await reader.current_video_summary(task.dataset_item_id)
            if video_summary is None:
                raise AnnotationConversionError(
                    status_code=409,
                    reason="source_dimensions_missing",
                    message="conversion requires current source video dimensions",
                )
            width, height, _video = video_summary
            media = "video"
        else:
            summary = await reader.one(task.dataset_item_id)
            if summary is None:
                raise AnnotationConversionError(
                    status_code=409,
                    reason="source_media_missing",
                    message="conversion requires a primary dataset item",
                )
            width, height = summary[0], summary[1]
            media = "image"
        if not width or not height:
            raise AnnotationConversionError(
                status_code=409,
                reason="source_dimensions_missing",
                message="conversion requires source width and height",
            )
        return None, int(width), int(height), media

    async def _load_sources(
        self, task_id: uuid.UUID, annotation_ids: list[uuid.UUID], *, lock: bool
    ) -> list[Annotation]:
        data = await self._transfer_command(
            ReadConversionSources(
                schema=ANNOTATION_TRANSFER_PROTOCOL,
                operation="read_conversion_sources",
                task_id=task_id,
                annotation_ids=annotation_ids,
                lock=lock,
            )
        )
        sources = [
            Annotation(**snapshot.model_dump(mode="python"))
            for snapshot in data.conversion_sources
        ]
        if len(sources) != len(annotation_ids):
            raise AnnotationConversionError(
                status_code=404,
                reason="annotation_not_found",
                message="one or more source annotations were not found",
            )
        locked = [str(item.id) for item in sources if item.is_locked]
        if locked:
            raise AnnotationConversionError(
                status_code=409,
                reason="annotation_locked",
                message="locked annotations cannot be converted",
                annotation_ids=locked,
            )
        source_types = {
            str((item.geometry or {}).get("type") or "") for item in sources
        }
        if len(source_types) != 1:
            raise AnnotationConversionError(
                status_code=422,
                reason="mixed_source_types",
                message="batch conversion requires one source geometry type",
                source_types=sorted(source_types),
            )
        return sources

    def _verify_source_records(
        self,
        sources: list[Annotation],
        source_records: list[dict[str, Any]],
    ) -> dict[str, Annotation]:
        source_by_id = {str(source.id): source for source in sources}
        for expected in source_records:
            source = source_by_id[expected["id"]]
            current_version = int(source.version or 1)
            if current_version != int(expected["version"]):
                raise AnnotationConversionError(
                    status_code=409,
                    reason="version_mismatch",
                    message="source annotation version changed after dry-run",
                    annotation_id=str(source.id),
                    expected_version=int(expected["version"]),
                    current_version=current_version,
                )
            if _canonical_digest(_annotation_snapshot(source)) != expected["digest"]:
                raise AnnotationConversionError(
                    status_code=409,
                    reason="snapshot_stale",
                    message="source annotation snapshot changed after dry-run",
                    annotation_id=str(source.id),
                )
        return source_by_id

    def _plan_rle_reference(self, rle: dict[str, Any]) -> dict[str, Any]:
        """Build a deterministic manifest without writing preview content."""
        expected = build_rle_reference(rle)
        object_key = str(expected["object_key"])
        previous = self._generated_rles.get(object_key)
        if previous is not None and _canonical_digest(previous) != _canonical_digest(
            rle
        ):
            raise RuntimeError("mask content identity collision during conversion")
        self._generated_rles[object_key] = rle
        return expected

    def _generated_rle_references(
        self,
        required_keys: set[str],
    ) -> list[dict[str, Any]]:
        if set(self._generated_rles) != required_keys:
            raise AnnotationConversionError(
                status_code=409,
                reason="plan_report_mismatch",
                message="conversion output changed after dry-run",
            )
        return [
            build_rle_reference(self._generated_rles[key])
            for key in sorted(required_keys)
        ]

    async def _reserve_generated_rles(
        self,
        task: Task,
        required_keys: set[str],
    ) -> None:
        references = self._generated_rle_references(required_keys)
        await lock_raster_mask_references(self.db, references, verify=False)
        for reference in references:
            object_key = str(reference["object_key"])
            try:
                await reserve_raster_mask_upload(
                    self.db,
                    task_id=task.id,
                    object_key=object_key,
                    limit=MAX_CONVERSION_MASK_OBJECTS_PER_TASK,
                )
            except ValueError as exc:
                raise AnnotationConversionError(
                    status_code=422,
                    reason="mask_quota_exceeded",
                    message="conversion would exceed the task mask object quota",
                    limit=MAX_CONVERSION_MASK_OBJECTS_PER_TASK,
                ) from exc

    async def _store_generated_rles(self, required_keys: set[str]) -> None:
        references = self._generated_rle_references(required_keys)
        for reference in references:
            object_key = str(reference["object_key"])
            try:
                stored = await store_coco_rle(self._generated_rles[object_key])
            except Exception as exc:
                raise AnnotationConversionError(
                    status_code=503,
                    reason="mask_storage_unavailable",
                    message="mask content storage is unavailable",
                ) from exc
            if stored["object_key"] != object_key:
                raise RuntimeError("mask storage identity changed during conversion")

    async def _load_source_rle(self, reference: dict[str, Any]) -> dict[str, Any]:
        try:
            return await load_coco_rle(reference)
        except (KeyError, TypeError, ValueError) as exc:
            raise AnnotationConversionError(
                status_code=409,
                reason="source_mask_invalid",
                message="source mask content is invalid or inconsistent",
            ) from exc
        except Exception as exc:
            raise AnnotationConversionError(
                status_code=503,
                reason="mask_storage_unavailable",
                message="source mask content is unavailable",
            ) from exc

    async def _validate_target_class(
        self, source: Annotation, target_geometry: dict[str, Any]
    ) -> None:
        await AnnotationService(self.db)._validate_class_name(
            source.project_id,
            _target_tool_unit(target_geometry),
            source.class_name,
        )

    async def _plan_image_source(
        self,
        source: Annotation,
        payload: AnnotationConversionDryRunRequest,
        *,
        width: int,
        height: int,
    ) -> tuple[list[dict[str, Any]], AnnotationConversionItemReport, list[str]]:
        geometry = source.geometry or {}
        geometry_type = geometry.get("type")
        upload_keys: list[str] = []
        if geometry_type in {"polygon", "multi_polygon"} and payload.target == "mask":
            rle, metrics = region_to_mask_conversion(geometry, width, height)
            reference = self._plan_rle_reference(rle)
            upload_keys.append(str(reference["object_key"]))
            target_geometry = {"type": "raster_mask", "mask": reference}
        elif geometry_type == "raster_mask" and payload.target == "polygon":
            rle = await self._load_source_rle(geometry.get("mask") or {})
            target_geometry, metrics = mask_to_region_conversion(rle)
        elif geometry_type == "raster_mask" and payload.target == "bbox":
            rle = await self._load_source_rle(geometry.get("mask") or {})
            target_geometry, metrics = mask_to_bbox_conversion(rle)
        else:
            raise AnnotationConversionError(
                status_code=422,
                reason="unsupported_conversion",
                message=f"unsupported image conversion: {geometry_type} -> {payload.target}",
            )
        await self._validate_target_class(source, target_geometry)
        action = (
            _update_action(source, target_geometry)
            if payload.operation == "replace"
            else _create_action(source, target_geometry)
        )
        return (
            [action],
            _item_report(
                source,
                target_type=str(target_geometry.get("type")),
                frame_indexes=[],
                metrics=[metrics],
            ),
            upload_keys,
        )

    async def _video_polygon_result(
        self,
        source: Annotation,
        *,
        frame_index: int,
        points_geometry: dict[str, Any],
        width: int,
        height: int,
        occluded: bool = False,
        attributes: dict[str, Any] | None = None,
        track_id: str | None = None,
    ) -> tuple[dict[str, Any], ConversionMetrics, str]:
        rle, metrics = region_to_mask_conversion(points_geometry, width, height)
        reference = self._plan_rle_reference(rle)
        target_geometry = {
            "type": "video_track_mask",
            "track_id": track_id or _new_track_id(),
            "keyframes": [
                {
                    "frame_index": frame_index,
                    "mask": reference,
                    "source": "manual",
                    "occluded": occluded,
                    **({"attributes": attributes} if attributes is not None else {}),
                }
            ],
            "outside": [],
        }
        await self._validate_target_class(source, target_geometry)
        return target_geometry, metrics, str(reference["object_key"])

    async def _plan_video_polygon(
        self,
        source: Annotation,
        payload: AnnotationConversionDryRunRequest,
        *,
        width: int,
        height: int,
    ) -> tuple[list[dict[str, Any]], AnnotationConversionItemReport, list[str]]:
        geometry = source.geometry or {}
        geometry_type = geometry.get("type")
        if payload.target != "mask":
            raise AnnotationConversionError(
                status_code=422,
                reason="unsupported_conversion",
                message="video polygon conversion only supports target=mask",
            )
        actions: list[dict[str, Any]] = []
        upload_keys: list[str] = []
        metrics: list[ConversionMetrics] = []
        frame_indexes: list[int] = []
        materialized_held_frames = 0

        if geometry_type == "video_polygon":
            if payload.scope != "current_frame" or int(
                geometry.get("frame_index", -1)
            ) != int(payload.frame_index if payload.frame_index is not None else -1):
                raise AnnotationConversionError(
                    status_code=422,
                    reason="frame_scope_mismatch",
                    message="video_polygon must be converted at its own frame",
                )
            frame_index = int(payload.frame_index or 0)
            target_geometry, item_metrics, key = await self._video_polygon_result(
                source,
                frame_index=frame_index,
                points_geometry=_region_from_video_geometry(geometry),
                width=width,
                height=height,
            )
            metrics.append(item_metrics)
            upload_keys.append(key)
            frame_indexes.append(frame_index)
            actions.append(
                _update_action(source, target_geometry, frame_index=frame_index)
                if payload.operation == "replace"
                else _create_action(source, target_geometry, frame_index=frame_index)
            )
        elif geometry_type == "video_track_polygon":
            if payload.scope == "keyframes":
                target_keyframes: list[dict[str, Any]] = []
                for keyframe in sorted_keyframes(geometry):
                    frame_index = int(keyframe.get("frame_index", 0))
                    if frame_is_outside(geometry, frame_index):
                        continue
                    result, item_metrics, key = await self._video_polygon_result(
                        source,
                        frame_index=frame_index,
                        points_geometry={
                            "type": "polygon",
                            "points": keyframe.get("points") or [],
                        },
                        width=width,
                        height=height,
                        occluded=bool(keyframe.get("occluded", False)),
                        attributes=keyframe.get("attributes"),
                        track_id=str(geometry.get("track_id") or _new_track_id()),
                    )
                    target_keyframes.extend(result["keyframes"])
                    metrics.append(item_metrics)
                    upload_keys.append(key)
                    frame_indexes.append(frame_index)
                if not target_keyframes:
                    raise AnnotationConversionError(
                        status_code=422,
                        reason="empty_result",
                        message="polygon track has no visible keyframes",
                    )
                target_geometry = {
                    "type": "video_track_mask",
                    "track_id": (
                        str(geometry.get("track_id"))
                        if payload.operation == "replace"
                        else _new_track_id()
                    ),
                    **(
                        {"semantic_label": geometry.get("semantic_label")}
                        if geometry.get("semantic_label") is not None
                        else {}
                    ),
                    "keyframes": target_keyframes,
                    "outside": normalize_outside_ranges(geometry.get("outside") or []),
                }
                await self._validate_target_class(source, target_geometry)
                actions.append(
                    _update_action(source, target_geometry)
                    if payload.operation == "replace"
                    else _create_action(source, target_geometry)
                )
            elif payload.scope == "current_frame":
                frame_index = int(payload.frame_index or 0)
                resolved = resolve_track_at_frame(geometry, frame_index)
                if resolved is None:
                    raise AnnotationConversionError(
                        status_code=422,
                        reason="frame_not_visible",
                        message="polygon track is not visible at the requested frame",
                    )
                exact = next(
                    (
                        item
                        for item in sorted_keyframes(geometry)
                        if int(item.get("frame_index", -1)) == frame_index
                    ),
                    None,
                )
                if exact is None and not payload.materialize_held:
                    raise AnnotationConversionError(
                        status_code=422,
                        reason="held_materialization_required",
                        message="held polygon frames require materialize_held=true",
                    )
                materialized_held_frames = int(exact is None)
                target_geometry, item_metrics, key = await self._video_polygon_result(
                    source,
                    frame_index=frame_index,
                    points_geometry={
                        "type": "polygon",
                        "points": resolved.get("points") or [],
                    },
                    width=width,
                    height=height,
                    occluded=bool((exact or resolved).get("occluded", False)),
                    attributes=(exact or {}).get("attributes"),
                )
                metrics.append(item_metrics)
                upload_keys.append(key)
                frame_indexes.append(frame_index)
                actions.append(
                    _create_action(source, target_geometry, frame_index=frame_index)
                )
                if payload.operation == "replace":
                    actions.append(_suppress_track_frame_action(source, frame_index))
            else:
                raise AnnotationConversionError(
                    status_code=422,
                    reason="scope_mismatch",
                    message="video track polygon requires current_frame or keyframes scope",
                )
        else:
            raise AnnotationConversionError(
                status_code=422,
                reason="unsupported_conversion",
                message=f"unsupported video polygon source: {geometry_type}",
            )

        return (
            actions,
            _item_report(
                source,
                target_type="video_track_mask",
                frame_indexes=frame_indexes,
                metrics=metrics,
                materialized_held_frames=materialized_held_frames,
            ),
            upload_keys,
        )

    async def _plan_video_mask_bbox(
        self,
        source: Annotation,
        payload: AnnotationConversionDryRunRequest,
    ) -> tuple[list[dict[str, Any]], AnnotationConversionItemReport, list[str]]:
        geometry = source.geometry or {}
        if (
            geometry.get("type") != "video_track_mask"
            or payload.target != "bbox"
            or payload.scope != "current_frame"
        ):
            raise AnnotationConversionError(
                status_code=422,
                reason="unsupported_conversion",
                message="video mask conversion only supports current_frame -> bbox",
            )
        frame_index = int(payload.frame_index or 0)
        resolved = resolve_track_at_frame(geometry, frame_index)
        if resolved is None:
            raise AnnotationConversionError(
                status_code=422,
                reason="frame_not_visible",
                message="mask track is not visible at the requested frame",
            )
        rle = await self._load_source_rle(resolved.get("mask") or {})
        target_geometry, metrics = mask_to_bbox_conversion(
            rle, video_frame_index=frame_index
        )
        await self._validate_target_class(source, target_geometry)
        actions = [_create_action(source, target_geometry, frame_index=frame_index)]
        if payload.operation == "replace":
            actions.append(_suppress_track_frame_action(source, frame_index))
        return (
            actions,
            _item_report(
                source,
                target_type="video_bbox",
                frame_indexes=[frame_index],
                metrics=[metrics],
            ),
            [],
        )

    async def _build_plan(
        self,
        sources: list[Annotation],
        payload: AnnotationConversionDryRunRequest,
        *,
        width: int,
        height: int,
        media: str,
    ) -> tuple[
        list[dict[str, Any]],
        list[AnnotationConversionItemReport],
        list[str],
    ]:
        if media == "image" and width * height > MAX_DENSE_MASK_PIXELS:
            raise AnnotationConversionError(
                status_code=422,
                reason="large_mask_full_scan_required",
                message=(
                    "mask conversion requires a full scan that exceeds the "
                    "synchronous pixel budget"
                ),
                max_pixels=MAX_DENSE_MASK_PIXELS,
            )
        raster_frames = 0
        for source in sources:
            geometry = source.geometry or {}
            if (
                media == "video"
                and payload.scope == "keyframes"
                and geometry.get("type") == "video_track_polygon"
            ):
                raster_frames += sum(
                    1
                    for keyframe in sorted_keyframes(geometry)
                    if not frame_is_outside(
                        geometry,
                        int(keyframe.get("frame_index", 0)),
                    )
                )
            else:
                raster_frames += 1
        raster_pixels = width * height * raster_frames
        if raster_pixels > MAX_CONVERSION_RASTER_PIXELS:
            raise AnnotationConversionError(
                status_code=422,
                reason="conversion_budget_exceeded",
                message="conversion exceeds the synchronous raster pixel budget",
                requested_pixels=raster_pixels,
                limit_pixels=MAX_CONVERSION_RASTER_PIXELS,
            )

        self._generated_rles = {}
        actions: list[dict[str, Any]] = []
        reports: list[AnnotationConversionItemReport] = []
        upload_keys: list[str] = []
        for source in sources:
            source_type = str((source.geometry or {}).get("type") or "")
            try:
                if media == "image":
                    (
                        planned_actions,
                        report,
                        planned_uploads,
                    ) = await self._plan_image_source(
                        source,
                        payload,
                        width=width,
                        height=height,
                    )
                elif source_type in {"video_polygon", "video_track_polygon"}:
                    (
                        planned_actions,
                        report,
                        planned_uploads,
                    ) = await self._plan_video_polygon(
                        source,
                        payload,
                        width=width,
                        height=height,
                    )
                else:
                    (
                        planned_actions,
                        report,
                        planned_uploads,
                    ) = await self._plan_video_mask_bbox(source, payload)
            except AnnotationConversionError:
                raise
            except ValueError as exc:
                raise AnnotationConversionError(
                    status_code=422,
                    reason="conversion_geometry_invalid",
                    message=f"source geometry cannot be converted: {exc}",
                    annotation_id=str(source.id),
                ) from exc
            actions.extend(planned_actions)
            reports.append(report)
            upload_keys.extend(planned_uploads)
        return actions, reports, upload_keys

    async def dry_run(
        self,
        *,
        task: Task,
        actor: User | IdentityActorFacts,
        payload: AnnotationConversionDryRunRequest,
        access: Any = None,
    ) -> AnnotationConversionDryRunResponse:
        try:
            await assert_task_lock_for_legacy_video(self.db, task, actor.id)
        except TaskLockConflictError as exc:
            raise AnnotationConversionError(
                status_code=409,
                reason="task_lock_conflict",
                message="task is locked by another user",
            ) from exc
        _, width, height, media = await self._task_item_and_dimensions(task)
        if (media == "image") != (payload.scope == "image"):
            raise AnnotationConversionError(
                status_code=422,
                reason="scope_media_mismatch",
                message="conversion scope does not match task media",
            )
        sources = await self._load_sources(task.id, payload.annotation_ids, lock=False)
        project = await get_workspace_project(self.db, project_id=task.project_id)
        if media == "image" and payload.target == "mask":
            assert_raster_mask_write_enabled(project)
        if media == "video":
            if payload.scope == "current_frame":
                frame_indexes = [int(payload.frame_index or 0)]
            else:
                frame_indexes = sorted(
                    {
                        int(keyframe.get("frame_index", 0))
                        for source in sources
                        for keyframe in sorted_keyframes(source.geometry or {})
                        if not frame_is_outside(
                            source.geometry or {},
                            int(keyframe.get("frame_index", 0)),
                        )
                    }
                )
            await self._lock_video_segments(
                task,
                actor,
                project,
                frame_indexes,
            )

        actions, reports, upload_keys = await self._build_plan(
            sources,
            payload,
            width=width,
            height=height,
            media=media,
        )

        source_records = [_source_record(source) for source in sources]
        summary = _summary(reports)
        request_json = payload.model_dump(mode="json")
        plan_json = {
            "sources": source_records,
            "source_snapshots": [
                {"id": str(source.id), "snapshot": _annotation_snapshot(source)}
                for source in sources
            ],
            "actions": actions,
            "items": [item.model_dump(mode="json") for item in reports],
            "summary": summary.model_dump(mode="json"),
            "upload_keys": sorted(set(upload_keys)),
            "media": media,
        }
        token = f"cvp_{secrets.token_urlsafe(32)}"
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=CONVERSION_PLAN_TTL_SECONDS
        )
        await self._transfer_command(
            CreateConversionPlan(
                schema=ANNOTATION_TRANSFER_PROTOCOL,
                operation="create_conversion_plan",
                plan_id=uuid.uuid4(),
                token_hash=_token_hash(token),
                task_id=task.id,
                actor_id=actor.id,
                request_digest=_canonical_digest(request_json),
                snapshot_digest=_canonical_digest(source_records),
                request_json=request_json,
                plan_json=plan_json,
                expires_at=expires_at,
            )
        )
        return AnnotationConversionDryRunResponse(
            plan_token=token,
            expires_at=expires_at,
            target=payload.target,
            operation=payload.operation,
            scope=payload.scope,
            items=reports,
            summary=summary,
        )

    async def _lock_video_segments(
        self,
        task: Task,
        actor: User | IdentityActorFacts,
        project: Project | None,
        frame_indexes: list[int],
    ) -> None:
        if not frame_indexes:
            return
        segment_data = await self._transfer_command(
            ReadConversionVideoSegments(
                schema=ANNOTATION_TRANSFER_PROTOCOL,
                operation="read_conversion_video_segments",
                dataset_item_id=task.dataset_item_id,
            )
        )
        segments = segment_data.video_segments
        now = datetime.now(timezone.utc)
        privileged = project is not None and is_privileged_for_project(actor, project)
        config = collaboration_config(project)
        frame_count = max((segment.end_frame for segment in segments), default=0) + 1

        def contains_frame(segment, frame_index: int) -> bool:
            work_start, work_end = segment_work_bounds(
                start_frame=segment.start_frame,
                end_frame=segment.end_frame,
                segment_index=segment.segment_index,
                segment_count=max(1, len(segments)),
                frame_count=frame_count,
                overlap_frames=config.overlap_frames if config.enabled else 0,
            )
            return work_start <= frame_index <= work_end

        for frame_index in frame_indexes:
            matching = [
                segment for segment in segments if contains_frame(segment, frame_index)
            ]
            if not matching:
                raise AnnotationConversionError(
                    status_code=409,
                    reason="segment_lock_conflict",
                    message="video conversion frame has no editable segment",
                    frame_index=frame_index,
                )
            if privileged:
                continue
            if not any(
                segment.locked_by == actor.id
                and segment.lock_expires_at is not None
                and segment.lock_expires_at > now
                for segment in matching
            ):
                raise AnnotationConversionError(
                    status_code=409,
                    reason="segment_lock_conflict",
                    message="video conversion frames must be locked by current user",
                    frame_index=frame_index,
                )

    async def _load_plan(self, token: str):
        data = await self._transfer_command(
            ReadConversionPlan(
                schema=ANNOTATION_TRANSFER_PROTOCOL,
                operation="read_conversion_plan",
                token_hash=_token_hash(token),
            )
        )
        return data.plan

    async def _replay_existing_operation(
        self,
        *,
        task_id: uuid.UUID,
        actor_id: uuid.UUID,
        idempotency_key: str,
        execution_digest: str,
    ) -> AnnotationConversionExecuteResponse | None:
        data = await self._transfer_command(
            ReadConversionReplay(
                schema=ANNOTATION_TRANSFER_PROTOCOL,
                operation="read_conversion_replay",
                task_id=task_id,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
            )
        )
        existing_operation = data.replay
        if existing_operation is None:
            return None
        if existing_operation.request_digest != execution_digest:
            raise AnnotationConversionError(
                status_code=409,
                reason="idempotency_conflict",
                message="idempotency key was already used by another operation",
                operation_id=str(existing_operation.operation_id),
            )
        replay = dict(existing_operation.response_json)
        replay["idempotent_replay"] = True
        return AnnotationConversionExecuteResponse.model_validate(replay)

    async def execute(
        self,
        *,
        task_id: uuid.UUID,
        actor: User | IdentityActorFacts,
        payload: AnnotationConversionExecuteRequest,
        request: Request,
        access: Any = None,
    ) -> AnnotationConversionExecuteResponse:
        execution_payload = payload.model_dump(mode="json")
        execution_payload.pop("plan_token")
        execution_digest = _canonical_digest(
            {
                "plan_token_hash": _token_hash(payload.plan_token),
                **execution_payload,
            }
        )
        replay = await self._replay_existing_operation(
            task_id=task_id,
            actor_id=actor.id,
            idempotency_key=payload.idempotency_key,
            execution_digest=execution_digest,
        )
        if replay is not None:
            return replay

        initial_plan = await self._load_plan(payload.plan_token)
        if initial_plan is None or initial_plan.task_id != task_id:
            raise AnnotationConversionError(
                status_code=404,
                reason="plan_token_invalid",
                message="conversion plan token is invalid",
            )
        if initial_plan.actor_id != actor.id:
            raise AnnotationConversionError(
                status_code=403,
                reason="plan_actor_mismatch",
                message="conversion plan belongs to another user",
            )

        task = await lock_task_for_core_read(self.db, task_id, populate_existing=True)
        if task is None:
            raise AnnotationConversionError(
                status_code=404, reason="task_not_found", message="Task not found"
            )
        from app.services import task_access

        if access is None:
            access = await task_access.resolve_task_access(
                self.db, task, actor.id, lock_membership=True
            )
        task_access.assert_annotation_write_allowed(task, access)
        await task_access.assert_review_adjustment_evidence(
            self.db, task, actor.id, access
        )
        is_review_adjuster = bool(
            access is not None
            and (access.is_manager or access.project_role == "reviewer")
        )
        if task.status == "completed" or (
            task.status == "review" and not is_review_adjuster
        ):
            raise AnnotationConversionError(
                status_code=409,
                reason="task_locked",
                message="task status does not allow annotation conversion",
                status=task.status,
            )
        try:
            await assert_task_lock_for_legacy_video(self.db, task, actor.id)
        except TaskLockConflictError as exc:
            raise AnnotationConversionError(
                status_code=409,
                reason="task_lock_conflict",
                message="task is locked by another user",
            ) from exc

        replay = await self._replay_existing_operation(
            task_id=task_id,
            actor_id=actor.id,
            idempotency_key=payload.idempotency_key,
            execution_digest=execution_digest,
        )
        if replay is not None:
            return replay

        locked_plan = await self._transfer_command(
            LockConversionPlan(
                schema=ANNOTATION_TRANSFER_PROTOCOL,
                operation="lock_conversion_plan",
                token_hash=_token_hash(payload.plan_token),
                task_id=task_id,
                actor_id=actor.id,
            )
        )
        plan = locked_plan.plan
        assert plan is not None
        if plan.executed_operation_id is not None:
            raise AnnotationConversionError(
                status_code=409,
                reason="plan_consumed",
                message="conversion plan was already executed",
                operation_id=str(plan.executed_operation_id),
            )
        if plan.expires_at <= datetime.now(timezone.utc):
            raise AnnotationConversionError(
                status_code=409,
                reason="plan_expired",
                message="conversion plan has expired",
            )

        request_json = plan.request_json
        if request_json.get("operation") == "replace" and not payload.confirm_replace:
            raise AnnotationConversionError(
                status_code=422,
                reason="replace_confirmation_required",
                message="replace conversion requires explicit confirmation",
            )
        summary = AnnotationConversionSummary.model_validate(
            plan.plan_json.get("summary") or {}
        )
        if summary.lossy_count and not payload.confirm_lossy:
            raise AnnotationConversionError(
                status_code=422,
                reason="lossy_confirmation_required",
                message="lossy conversion requires explicit confirmation",
            )

        source_records = plan.plan_json.get("sources") or []
        source_ids = [uuid.UUID(item["id"]) for item in source_records]
        project = await get_workspace_project(self.db, project_id=task.project_id)
        frame_indexes = sorted(
            {
                int(frame_index)
                for item in plan.plan_json.get("items") or []
                for frame_index in item.get("frame_indexes") or []
            }
        )
        if plan.plan_json.get("media") == "video":
            await self._lock_video_segments(task, actor, project, frame_indexes)
        elif request_json.get("target") == "mask":
            assert_raster_mask_write_enabled(project)

        sources = await self._load_sources(task_id, source_ids, lock=False)
        self._verify_source_records(sources, source_records)

        _, width, height, media = await self._task_item_and_dimensions(task)
        planned_request = AnnotationConversionDryRunRequest.model_validate(request_json)
        rebuilt_actions, rebuilt_reports, rebuilt_upload_keys = await self._build_plan(
            sources,
            planned_request,
            width=width,
            height=height,
            media=media,
        )
        rebuilt_summary = _summary(rebuilt_reports)
        expected_plan = {
            "actions": _without_track_ids(plan.plan_json.get("actions") or []),
            "items": plan.plan_json.get("items") or [],
            "summary": plan.plan_json.get("summary") or {},
            "upload_keys": sorted(set(plan.plan_json.get("upload_keys") or [])),
            "media": plan.plan_json.get("media"),
        }
        rebuilt_plan = {
            "actions": _without_track_ids(rebuilt_actions),
            "items": [item.model_dump(mode="json") for item in rebuilt_reports],
            "summary": rebuilt_summary.model_dump(mode="json"),
            "upload_keys": sorted(set(rebuilt_upload_keys)),
            "media": media,
        }
        if _canonical_digest(rebuilt_plan) != _canonical_digest(expected_plan):
            raise AnnotationConversionError(
                status_code=409,
                reason="plan_report_mismatch",
                message="conversion output changed after dry-run",
            )

        actions = plan.plan_json.get("actions") or []
        mask_payload = [
            action.get("geometry")
            for action in actions
            if action.get("kind") in {"create", "update"}
        ]
        upload_keys = set(plan.plan_json.get("upload_keys") or [])
        if upload_keys:
            await self._reserve_generated_rles(task, upload_keys)

        # Keep the shared mutation lock order: Task / segment / RLE / upload
        # before annotation UUID. Re-read after taking row locks so a source
        # change during the pre-lock plan rebuild is still rejected atomically.
        sources = await self._load_sources(task_id, source_ids, lock=True)
        source_by_id = self._verify_source_records(sources, source_records)
        if upload_keys:
            await self._store_generated_rles(upload_keys)
        if mask_payload:
            await prepare_mask_payload_for_write(
                self.db,
                task,
                mask_payload,
                required_upload_keys=upload_keys,
            )

        command_actions: list[ConversionAction] = []
        for action in actions:
            source = source_by_id[action["source_id"]]
            if action["kind"] == "update":
                geometry, track_id = prepare_compact_track_identity(
                    action["geometry"],
                    source.track_id,
                    reject_identity_change=False,
                )
                await self._validate_target_class(source, geometry)
                command_actions.append(
                    ConversionAction(
                        kind="update",
                        source_id=source.id,
                        result_id=None,
                        geometry=geometry,
                        annotation_type=action["annotation_type"],
                        tool_unit_id=action["tool_unit_id"],
                        track_id=track_id,
                        frame_index=action.get("frame_index"),
                    )
                )
            elif action["kind"] == "create":
                geometry, track_id = prepare_compact_track_identity(action["geometry"])
                await self._validate_target_class(source, geometry)
                command_actions.append(
                    ConversionAction(
                        kind="create",
                        source_id=source.id,
                        result_id=uuid.uuid4(),
                        geometry=geometry,
                        annotation_type=action["annotation_type"],
                        tool_unit_id=action["tool_unit_id"],
                        track_id=track_id,
                        frame_index=action.get("frame_index"),
                    )
                )
            else:
                command_actions.append(
                    ConversionAction(
                        kind="deactivate",
                        source_id=source.id,
                        result_id=None,
                        geometry=None,
                        annotation_type=None,
                        tool_unit_id=None,
                        track_id=None,
                        frame_index=action.get("frame_index"),
                    )
                )

        operation_id = uuid.uuid4()
        conversion_data = await self._transfer_command(
            ApplyConversion(
                schema=ANNOTATION_TRANSFER_PROTOCOL,
                operation="apply_conversion",
                plan_token_hash=_token_hash(payload.plan_token),
                plan_id=plan.id,
                task_id=task_id,
                project_id=task.project_id,
                actor_id=actor.id,
                expected_batch_id=task.batch_id,
                expected_status=task.status,
                expected_review_round_id=task.review_round_id,
                execution_id=operation_id,
                idempotency_key=payload.idempotency_key,
                execution_digest=execution_digest,
                scope_fingerprint=plan.snapshot_digest,
                source_fences=[
                    ConversionSourceFence(
                        annotation_id=uuid.UUID(item["id"]),
                        version=int(item["version"]),
                        digest=item["digest"],
                    )
                    for item in source_records
                ],
                actions=command_actions,
                report=summary.model_dump(mode="json"),
            )
        )
        assert conversion_data.conversion is not None
        conversion = conversion_data.conversion
        await record_annotation_actor(self.db, task, actor.id)
        await AnnotationService(self.db)._update_task_stats(task_id)
        await heartbeat_task_lock_for_legacy_video(self.db, task, actor.id)

        source_versions = {item["id"]: int(item["version"]) for item in source_records}
        lineage_out: list[AnnotationConversionLineageOut] = []
        for action in command_actions:
            if action.kind == "deactivate":
                continue
            lineage_out.append(
                AnnotationConversionLineageOut(
                    source_annotation_id=action.source_id,
                    result_annotation_id=action.result_id or action.source_id,
                    source_version=source_versions[str(action.source_id)],
                    result_version=conversion.result_versions[
                        str(action.result_id or action.source_id)
                    ],
                    frame_index=action.frame_index,
                )
            )
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.ANNOTATION_CONVERT,
            target_type="annotation_operation",
            target_id=operation_id,
            request=request,
            status_code=200,
            detail={
                "task_id": str(task_id),
                "operation": request_json.get("operation"),
                "target": request_json.get("target"),
                "scope": request_json.get("scope"),
                "source_count": summary.source_count,
                "result_count": summary.result_count,
                "lossy_count": summary.lossy_count,
            },
        )
        response = AnnotationConversionExecuteResponse(
            operation_id=operation_id,
            updated_annotations=[
                AnnotationOut.model_validate(item.model_dump(mode="python"))
                for item in conversion.updated_annotations
            ],
            created_annotations=[
                AnnotationOut.model_validate(item.model_dump(mode="python"))
                for item in conversion.created_annotations
            ],
            deleted_annotation_ids=conversion.deleted_annotation_ids,
            lineage_edges=lineage_out,
            report=summary,
        )
        await self._transfer_command(
            FinalizeConversion(
                schema=ANNOTATION_TRANSFER_PROTOCOL,
                operation="finalize_conversion",
                plan_token_hash=_token_hash(payload.plan_token),
                task_id=task_id,
                actor_id=actor.id,
                idempotency_key=payload.idempotency_key,
                operation_id=operation_id,
                response_json=response.model_dump(mode="json"),
            )
        )
        return response


__all__ = [
    "AnnotationConversionError",
    "AnnotationConversionService",
    "RasterMaskContractError",
]
