from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.dataset import VideoSegment
from app.services.project_access import get_workspace_project
from app.db.models.annotation_operation import AnnotationOperation
from sqlalchemy.orm import make_transient_to_detached
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key
from aap_labeling.mask_edit_contract import (
    ApplyMaskMutations,
    AnnotationLineage,
    CreateMaskAnnotation,
    DeactivateMaskAnnotation,
    FindOperation,
    MASK_EDIT_PROTOCOL,
    ReadMaskUploadKeys,
    ReadMaskVideoSegments,
    UpdateMaskAnnotation,
    WriteAnnotationOperation,
)
from app.services.labeling_mask_edit import (
    MaskOwnerCommandError,
    mask_edit_command,
    mask_task_fence,
)
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.mask_mutation import (
    MaskCreateMutation,
    MaskDeleteMutation,
    MaskLineageEdgeOut,
    MaskMutationAffectedReport,
    MaskMutationCommitRequest,
    MaskMutationCommitResponse,
    MaskMutationAnnotationResult,
    MaskMutationReport,
    MaskMutationScope,
    MaskUpdateMutation,
)
from app.services.annotation import AnnotationService
from app.services.labeling_core_read_relations import (
    annotation_row,
    execute_core_read,
    get_core_row,
    lock_annotation_rows_for_core_read,
    lock_task_for_core_read,
    task_row,
)
from app.services.annotation_evidence import record_annotation_actor
from app.services.annotation_slice_contracts import (
    SLICE_RESTORE_TTL as _SLICE_RESTORE_TTL,
)
from app.schemas.annotation_slice import AnnotationSliceResponse
from app.services.mask_slice import slice_mask_rle
from app.services.mask_slice_restore import (
    mask_version_snapshot,
    protect_mask_slice_versions,
)
from app.services.annotation_propagation import _new_track_id
from app.services.annotation_track_identity import prepare_compact_track_identity
from app.services.audit import AuditAction, AuditService
from app.services.audit_origin_contract import AuditOriginContext
from app.services.mask_mutation_contract import MaskMutationError as _MaskMutationError
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    load_coco_rle,
    prepare_mask_payload_for_write,
    preflight_mask_payload_for_write,
)
from app.services.labeling_write_scope import (
    capture_task_source_binding,
    task_source_binding_matches,
)
from app.services.project_access import is_privileged_for_project
from app.services.video_collaboration import (
    assert_task_lock_for_legacy_video,
    collaboration_config,
    heartbeat_task_lock_for_legacy_video,
    segment_work_bounds,
)
from app.services.task_lock import TaskLockConflictError
from app.services.video_tracks import normalize_outside_ranges, resolve_track_at_frame
from app.utils.raster_mask_rle import (
    MAX_DENSE_MASK_PIXELS,
    MAX_MASK_RUNS,
    coco_rle_area,
    coco_rle_bbox_norm,
    validate_coco_rle,
)


MAX_MASK_MUTATION_SCOPE_MEMBERS = 1000
MAX_MASK_MUTATION_RLE_OBJECTS = 1000
MAX_MASK_MUTATION_TOTAL_RUNS = 2_000_000
MAX_MASK_MUTATION_OVERLAP_PAIRS = 100_000
MAX_MASK_MUTATION_ALGEBRA_STEPS = 5_000_000


def _raise_mask_mutation_contract_error(exc: RasterMaskContractError) -> None:
    reason = str(exc.detail.get("reason") or "invalid_geometry")
    if reason == "mask_reference_not_reserved":
        raise _MaskMutationError(
            status_code=exc.status_code,
            reason="missing_ref",
            message=str(exc.detail.get("message") or exc),
            object_keys=exc.detail.get("object_keys", []),
        ) from exc
    if reason in {
        "mask_reference_invalid",
        "mask_geometry_invalid",
        "mask_task_context_missing",
        "raster_mask_dataset_item_required",
        "raster_mask_image_required",
        "raster_mask_source_dimensions_missing",
        "raster_mask_size_mismatch",
    }:
        reason = "invalid_geometry"
    elif reason == "raster_mask_empty_foreground":
        reason = "empty_result"
    raise _MaskMutationError(
        status_code=exc.status_code,
        reason=reason,
        message=str(exc.detail.get("message") or exc),
    ) from exc


class _AlgebraBudget:
    def __init__(self) -> None:
        self.steps = 0

    def consume(self, amount: int = 1) -> None:
        self.steps += amount
        if self.steps > MAX_MASK_MUTATION_ALGEBRA_STEPS:
            raise _MaskMutationError(
                status_code=422,
                reason="operation_too_large",
                message="mask mutation exceeds the cumulative algebra step budget",
            )


def _canonical_digest(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def request_digest(payload: MaskMutationCommitRequest) -> str:
    value = payload.model_dump(mode="json", by_alias=True)
    if payload.cut_path is None:
        value.pop("cut_path", None)
    return _canonical_digest(value)


def scope_fingerprint(
    scope: MaskMutationScope,
    annotations: list[Annotation],
) -> str:
    return _canonical_digest(
        {
            "scope": scope.model_dump(mode="json"),
            "members": [str(annotation.id) for annotation in annotations],
        }
    )


def _annotation_in_scope(annotation: Annotation, scope: MaskMutationScope) -> bool:
    geometry = annotation.geometry or {}
    if scope.media == "image":
        visible = geometry.get("type") == "raster_mask"
    else:
        visible = (
            geometry.get("type") == "video_track_mask"
            and resolve_track_at_frame(geometry, int(scope.frame_index or 0))
            is not None
        )
    if not visible:
        return False
    return scope.instance_filter == "all" or annotation.class_name == scope.class_name


def _geometry_for_frame(geometry: dict, scope: MaskMutationScope) -> dict | None:
    if scope.media == "image":
        return geometry.get("mask") if geometry.get("type") == "raster_mask" else None
    resolved = resolve_track_at_frame(geometry, int(scope.frame_index or 0))
    return (resolved or {}).get("mask")


def _validate_video_update_scope(
    before: dict,
    after: dict,
    frame_index: int,
) -> None:
    if (
        before.get("type") != "video_track_mask"
        or after.get("type") != "video_track_mask"
    ):
        raise _MaskMutationError(
            status_code=422,
            reason="invalid_geometry",
            message="video mutation requires video_track_mask geometry",
        )
    if before.get("track_id") != after.get("track_id"):
        raise _MaskMutationError(
            status_code=422,
            reason="invalid_geometry",
            message="video update cannot change track_id",
        )
    if before.get("semantic_label") != after.get("semantic_label"):
        raise _MaskMutationError(
            status_code=422,
            reason="invalid_geometry",
            message="current-frame mutation cannot change semantic_label",
        )
    if normalize_outside_ranges(
        before.get("outside") or []
    ) != normalize_outside_ranges(after.get("outside") or []):
        raise _MaskMutationError(
            status_code=422,
            reason="invalid_geometry",
            message="current-frame mutation cannot change outside ranges",
        )

    def canonical_keyframe(item: dict) -> dict:
        return {
            "frame_index": int(item.get("frame_index", -1)),
            "mask": item.get("mask"),
            "source": item.get("source", "manual"),
            "occluded": bool(item.get("occluded", False)),
            "attributes": item.get("attributes"),
        }

    def without_frame(value: dict) -> list[dict]:
        return [
            canonical_keyframe(item)
            for item in value.get("keyframes") or []
            if int(item.get("frame_index", -1)) != frame_index
        ]

    if without_frame(before) != without_frame(after):
        raise _MaskMutationError(
            status_code=422,
            reason="invalid_geometry",
            message="current-frame mutation cannot change other keyframes",
        )
    current_after = [
        canonical_keyframe(item)
        for item in after.get("keyframes") or []
        if int(item.get("frame_index", -1)) == frame_index
    ]
    if len(current_after) != 1 or current_after[0]["source"] != "manual":
        raise _MaskMutationError(
            status_code=422,
            reason="invalid_geometry",
            message="video mutation must materialize one manual current keyframe",
        )
    current_before = [
        canonical_keyframe(item)
        for item in before.get("keyframes") or []
        if int(item.get("frame_index", -1)) == frame_index
    ]
    if current_before:
        if (
            current_after[0]["occluded"] != current_before[0]["occluded"]
            or current_after[0]["attributes"] != current_before[0]["attributes"]
        ):
            raise _MaskMutationError(
                status_code=422,
                reason="invalid_geometry",
                message="video mask update cannot change current keyframe metadata",
            )
    elif current_after[0]["occluded"] or current_after[0]["attributes"] is not None:
        raise _MaskMutationError(
            status_code=422,
            reason="invalid_geometry",
            message="materialized video keyframe must use default metadata",
        )


def _validate_operation_shape(
    payload: MaskMutationCommitRequest,
    source_ids: set[uuid.UUID],
) -> None:
    updates = [
        item for item in payload.mutations if isinstance(item, MaskUpdateMutation)
    ]
    creates = [
        item for item in payload.mutations if isinstance(item, MaskCreateMutation)
    ]
    deletes = [
        item for item in payload.mutations if isinstance(item, MaskDeleteMutation)
    ]

    valid = False
    if payload.operation in {"split_components", "slice_mask"}:
        valid = (
            len(source_ids) == 1
            and len(updates) == 1
            and updates[0].annotation_id in source_ids
            and len(creates) >= 1
            and all(set(item.source_annotation_ids) == source_ids for item in creates)
            and not deletes
            and (payload.operation != "slice_mask" or len(creates) == 1)
        )
    elif payload.operation in {"copy_component", "copy_keyframe"}:
        valid = (
            len(source_ids) == 1
            and len(creates) == 1
            and set(creates[0].source_annotation_ids) == source_ids
            and not updates
            and not deletes
        )
    elif payload.operation == "join_masks":
        valid = len(source_ids) >= 2 and (
            (
                not creates
                and (
                    payload.scope.media == "image"
                    and len(updates) == 1
                    and len(deletes) == len(source_ids) - 1
                    and {item.annotation_id for item in deletes}
                    == source_ids - {updates[0].annotation_id}
                )
            )
            or (
                len(creates) == 1
                and set(creates[0].source_annotation_ids) == source_ids
                and not updates
                and not deletes
            )
        )
    elif payload.operation == "overlap":
        valid = (
            isinstance(payload.mutations[0], MaskUpdateMutation)
            and payload.scope.overlap_policy != "allow"
            and bool(updates)
            and not creates
        )
    elif payload.operation in {
        "delete_small_islands",
        "fill_small_holes",
        "resolve_same_class_overlap",
        "mask_repair_rollback",
    }:
        valid = (
            len(source_ids) == 1
            and len(updates) == 1
            and updates[0].annotation_id in source_ids
            and not creates
            and not deletes
        )

    if not valid:
        raise _MaskMutationError(
            status_code=422,
            reason="invalid_operation",
            message=f"mutations do not satisfy {payload.operation} contract",
        )


def _rle_intersects(
    left: dict,
    right: dict,
    budget: _AlgebraBudget | None = None,
) -> bool:
    lh, lw, left_counts = validate_coco_rle(left)
    rh, rw, right_counts = validate_coco_rle(right)
    if (lh, lw) != (rh, rw):
        raise _MaskMutationError(
            status_code=422,
            reason="invalid_geometry",
            message="mask dimensions do not match within mutation scope",
        )
    left_index = right_index = 0
    left_remaining = int(left_counts[0])
    right_remaining = int(right_counts[0])
    left_foreground = right_foreground = False
    consumed = 0
    total = lh * lw
    while consumed < total:
        if budget is not None:
            budget.consume()
        step = min(left_remaining, right_remaining)
        if step > 0 and left_foreground and right_foreground:
            return True
        consumed += step
        left_remaining -= step
        right_remaining -= step
        if left_remaining == 0:
            left_index += 1
            left_foreground = not left_foreground
            left_remaining = (
                int(left_counts[left_index]) if left_index < len(left_counts) else total
            )
        if right_remaining == 0:
            right_index += 1
            right_foreground = not right_foreground
            right_remaining = (
                int(right_counts[right_index])
                if right_index < len(right_counts)
                else total
            )
    return False


def _combine_rles(
    left: dict,
    right: dict,
    mode: str,
    budget: _AlgebraBudget | None = None,
) -> dict:
    try:
        lh, lw, left_counts = validate_coco_rle(left)
        rh, rw, right_counts = validate_coco_rle(right)
    except ValueError as exc:
        raise _MaskMutationError(
            status_code=422,
            reason="invalid_geometry",
            message="mask content is not valid COCO RLE",
        ) from exc
    if (lh, lw) != (rh, rw):
        raise _MaskMutationError(
            status_code=422,
            reason="invalid_geometry",
            message="mask dimensions do not match within mutation scope",
        )

    def positive_runs(counts: list[int]):
        foreground = False
        for count in counts:
            if count:
                yield count, foreground
            foreground = not foreground

    left_runs = iter(positive_runs(left_counts))
    right_runs = iter(positive_runs(right_counts))
    left_remaining, left_foreground = next(left_runs)
    right_remaining, right_foreground = next(right_runs)
    output_counts: list[int] = []
    output_foreground = False
    output_length = 0
    consumed = 0
    total = lh * lw

    def append_output_run(length: int) -> None:
        if len(output_counts) >= MAX_MASK_RUNS:
            raise _MaskMutationError(
                status_code=422,
                reason="operation_too_large",
                message="derived mask exceeds the RLE run budget",
            )
        output_counts.append(length)

    while consumed < total:
        if budget is not None:
            budget.consume()
        step = min(left_remaining, right_remaining)
        if mode == "or":
            next_foreground = left_foreground or right_foreground
        elif mode == "and_not":
            next_foreground = left_foreground and not right_foreground
        elif mode == "xor":
            next_foreground = left_foreground != right_foreground
        else:  # pragma: no cover - internal programming error
            raise AssertionError(f"unsupported RLE combine mode: {mode}")
        if next_foreground == output_foreground:
            output_length += step
        else:
            append_output_run(output_length)
            output_foreground = next_foreground
            output_length = step
        consumed += step
        left_remaining -= step
        right_remaining -= step
        if left_remaining == 0 and consumed < total:
            left_remaining, left_foreground = next(left_runs)
        if right_remaining == 0 and consumed < total:
            right_remaining, right_foreground = next(right_runs)
    append_output_run(output_length)
    return {"encoding": "coco_rle", "size": [lh, lw], "counts": output_counts}


def _union_rles(
    rles: list[dict],
    budget: _AlgebraBudget | None = None,
) -> dict:
    if not rles:
        raise _MaskMutationError(
            status_code=422,
            reason="invalid_operation",
            message="mask operation requires at least one source result",
        )
    combined = rles[0]
    for rle in rles[1:]:
        combined = _combine_rles(combined, rle, "or", budget)
    return combined


def _rle_equal(
    left: dict,
    right: dict,
    budget: _AlgebraBudget | None = None,
) -> bool:
    return coco_rle_area(_combine_rles(left, right, "xor", budget)) == 0


def _rle_subset(
    candidate: dict,
    source: dict,
    budget: _AlgebraBudget | None = None,
) -> bool:
    return coco_rle_area(_combine_rles(candidate, source, "and_not", budget)) == 0


def _rle_changed_pixels(
    before: dict,
    after: dict,
    budget: _AlgebraBudget | None = None,
) -> int:
    return coco_rle_area(_combine_rles(before, after, "xor", budget))


def _vertical_foreground_spans(
    rle: dict,
    budget: _AlgebraBudget,
) -> tuple[int, int, list[list[tuple[int, int]]]]:
    """Return merged foreground intervals per column without decoding pixels."""
    height, width, counts = validate_coco_rle(rle)
    columns: list[list[tuple[int, int]]] = [[] for _ in range(width)]
    offset = 0
    foreground = False
    for length in counts:
        if foreground and length:
            cursor = offset
            remaining = int(length)
            while remaining:
                budget.consume()
                column, start = divmod(cursor, height)
                take = min(remaining, height - start)
                end = start + take - 1
                spans = columns[column]
                if spans and start <= spans[-1][1] + 1:
                    spans[-1] = (spans[-1][0], max(spans[-1][1], end))
                else:
                    spans.append((start, end))
                cursor += take
                remaining -= take
        offset += int(length)
        foreground = not foreground
    return height, width, columns


def _source_component_index(
    source: dict,
    connectivity: int,
    budget: _AlgebraBudget,
) -> tuple[
    int,
    int,
    list[list[tuple[int, int, int]]],
    dict[int, int],
    list[int],
]:
    height, width, raw_columns = _vertical_foreground_spans(source, budget)
    indexed: list[list[tuple[int, int, int]]] = [[] for _ in range(width)]
    parent: list[int] = []
    rank: list[int] = []
    for column, spans in enumerate(raw_columns):
        for start, end in spans:
            node = len(parent)
            parent.append(node)
            rank.append(0)
            indexed[column].append((start, end, node))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if rank[left_root] < rank[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        if rank[left_root] == rank[right_root]:
            rank[left_root] += 1

    padding = 1 if connectivity == 8 else 0
    for column in range(1, width):
        previous = indexed[column - 1]
        current = indexed[column]
        left = right = 0
        while left < len(previous) and right < len(current):
            budget.consume()
            p_start, p_end, p_node = previous[left]
            c_start, c_end, c_node = current[right]
            if p_end + padding < c_start:
                left += 1
                continue
            if c_end + padding < p_start:
                right += 1
                continue
            union(p_node, c_node)
            if p_end < c_end:
                left += 1
            elif c_end < p_end:
                right += 1
            else:
                left += 1
                right += 1

    component_areas: dict[int, int] = {}
    roots = [find(node) for node in range(len(parent))]
    for spans in indexed:
        for start, end, node in spans:
            root = roots[node]
            component_areas[root] = component_areas.get(root, 0) + end - start + 1
    return height, width, indexed, component_areas, roots


def _is_complete_source_component(
    result: dict,
    *,
    source_index: tuple[
        int,
        int,
        list[list[tuple[int, int, int]]],
        dict[int, int],
        list[int],
    ],
    budget: _AlgebraBudget,
) -> bool:
    height, width, source_columns, component_areas, roots = source_index
    result_height, result_width, result_columns = _vertical_foreground_spans(
        result, budget
    )
    if (result_height, result_width) != (height, width):
        return False
    result_root: int | None = None
    result_area = 0
    for column in range(width):
        source_spans = source_columns[column]
        source_cursor = 0
        for result_start, result_end in result_columns[column]:
            while (
                source_cursor < len(source_spans)
                and source_spans[source_cursor][1] < result_start
            ):
                budget.consume()
                source_cursor += 1
            if source_cursor >= len(source_spans):
                return False
            source_start, source_end, source_node = source_spans[source_cursor]
            budget.consume()
            if source_start > result_start or source_end < result_end:
                return False
            root = roots[source_node]
            if result_root is None:
                result_root = root
            elif result_root != root:
                return False
            result_area += result_end - result_start + 1
    return result_root is not None and result_area == component_areas.get(
        result_root, -1
    )


class MaskMutationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _lock_task(self, task_id: uuid.UUID) -> Task:
        task = await lock_task_for_core_read(
            self.db, task_id, lock_mode="update", nowait=False, populate_existing=True
        )
        if task is None:
            raise _MaskMutationError(
                status_code=404,
                reason="task_not_found",
                message="Task not found",
            )
        return task

    async def _owner(self, command):
        try:
            return await mask_edit_command(self.db, command)
        except MaskOwnerCommandError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            reason = str(detail.pop("reason", "mask_write_conflict"))
            message = str(detail.pop("message", exc))
            raise _MaskMutationError(
                status_code=exc.status_code,
                reason=reason,
                message=message,
                **detail,
            ) from exc

    async def _idempotent_replay(
        self,
        task_id: uuid.UUID,
        actor_id: uuid.UUID,
        payload: MaskMutationCommitRequest,
        digest: str,
    ) -> MaskMutationCommitResponse | None:
        operation = await self._owner(
            FindOperation(
                schema=MASK_EDIT_PROTOCOL,
                operation="find_operation",
                task_id=task_id,
                actor_id=actor_id,
                idempotency_key=payload.idempotency_key,
            )
        )
        if not operation.found:
            return None
        if operation.request_digest != digest:
            raise _MaskMutationError(
                status_code=409,
                reason="idempotency_conflict",
                message="idempotency key was already used with another request",
                operation_id=str(operation.operation_id),
            )
        response = dict(operation.response_json or {})
        response["idempotent_replay"] = True
        return MaskMutationCommitResponse.model_validate(response)

    async def _lock_scope(
        self,
        task_id: uuid.UUID,
        scope: MaskMutationScope,
        *,
        for_update: bool = True,
        include_annotation_ids: set[uuid.UUID] | None = None,
    ) -> list[Annotation]:
        included_ids = include_annotation_ids or set()
        expected_type = "raster_mask" if scope.media == "image" else "video_track_mask"
        query = select(annotation_row).where(
            annotation_row.task_id == task_id,
            annotation_row.is_active.is_(True),
            annotation_row.was_cancelled.is_(False),
            annotation_row.annotation_type == expected_type,
        )
        if scope.instance_filter == "same_class":
            class_filter = annotation_row.class_name == scope.class_name
            if included_ids:
                class_filter = or_(class_filter, annotation_row.id.in_(included_ids))
            query = query.where(class_filter)
        query = query.order_by(annotation_row.id.asc()).limit(
            MAX_MASK_MUTATION_SCOPE_MEMBERS + 1
        )
        if for_update:
            # The current Task lock serializes Labeling writers between candidate
            # discovery and the fixed ordered row-lock capability.
            candidates = list(
                (
                    await execute_core_read(
                        self.db, query.execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .all()
            )
            rows = await lock_annotation_rows_for_core_read(
                self.db,
                [annotation.id for annotation in candidates],
                task_id=task_id,
                lock_mode="update",
                nowait=False,
                populate_existing=True,
            )
            rows = [
                annotation
                for annotation in rows
                if annotation.is_active
                and not annotation.was_cancelled
                and annotation.annotation_type == expected_type
                and (
                    scope.instance_filter != "same_class"
                    or annotation.class_name == scope.class_name
                    or annotation.id in included_ids
                )
            ]
        else:
            rows = list((await execute_core_read(self.db, query)).scalars().all())
        if len(rows) > MAX_MASK_MUTATION_SCOPE_MEMBERS:
            raise _MaskMutationError(
                status_code=422,
                reason="operation_too_large",
                message="mask mutation scope contains too many candidate annotations",
                max_scope_members=MAX_MASK_MUTATION_SCOPE_MEMBERS,
            )
        return [
            annotation
            for annotation in rows
            if _annotation_in_scope(annotation, scope) or annotation.id in included_ids
        ]

    async def _assert_segment_lease(
        self,
        task: Task,
        scope: MaskMutationScope,
        actor: User,
        project: Project | None,
        *,
        lock: bool = True,
    ) -> None:
        if scope.media != "video":
            return
        frame_index = int(scope.frame_index or 0)
        if task.dataset_item_id is None:
            if self.db.autoflush:
                await self.db.flush()
            segments = []
        else:
            result = await self._owner(
                ReadMaskVideoSegments(
                    schema=MASK_EDIT_PROTOCOL,
                    operation="read_video_segments",
                    task_id=task.id,
                    project_id=task.project_id,
                    actor_id=actor.id,
                    dataset_item_id=task.dataset_item_id,
                    segment_id=scope.segment_id,
                    lock=lock,
                )
            )
            segments = []
            for record in result.segments:
                key = identity_key(VideoSegment, ident=record.id)
                cached = self.db.sync_session.identity_map.get(key)
                values = record.model_dump()
                if cached is not None:
                    state = inspect(cached)
                    for field, value in values.items():
                        if field in state.unloaded:
                            set_committed_value(cached, field, value)
                    segments.append(cached)
                    continue
                detached = VideoSegment(
                    id=record.id,
                    segment_index=record.segment_index,
                    start_frame=record.start_frame,
                    end_frame=record.end_frame,
                    locked_by=record.locked_by,
                    lock_expires_at=record.lock_expires_at,
                )
                make_transient_to_detached(detached)
                segments.append(await self.db.merge(detached, load=False))
        config = collaboration_config(project)
        frame_count = max((row.end_frame for row in segments), default=0) + 1

        def contains_frame(row: VideoSegment) -> bool:
            work_start, work_end = segment_work_bounds(
                start_frame=row.start_frame,
                end_frame=row.end_frame,
                segment_index=row.segment_index,
                segment_count=max(1, len(segments)),
                frame_count=frame_count,
                overlap_frames=config.overlap_frames if config.enabled else 0,
            )
            return work_start <= frame_index <= work_end

        segment = next((row for row in segments if contains_frame(row)), None)
        if segment is None:
            raise _MaskMutationError(
                status_code=409,
                reason="segment_lock_conflict",
                message="video frame has no matching editable segment",
            )
        if project is not None and is_privileged_for_project(actor, project):
            return
        now = datetime.now(timezone.utc)
        if (
            segment.locked_by != actor.id
            or segment.lock_expires_at is None
            or segment.lock_expires_at <= now
        ):
            raise _MaskMutationError(
                status_code=409,
                reason="segment_lock_conflict",
                message="video segment must be locked by current user",
                segment_id=str(segment.id),
            )

    async def _validate_pixel_algebra(
        self,
        payload: MaskMutationCommitRequest,
        annotations: list[Annotation],
        source_ids: set[uuid.UUID],
        *,
        preloaded_rles: dict[str, dict] | None = None,
    ) -> tuple[MaskMutationReport, dict[str, dict], _AlgebraBudget]:
        rle_cache: dict[str, dict] = dict(preloaded_rles or {})

        def assert_pixel_budget(height: int, width: int) -> None:
            if (
                payload.scope.media == "image"
                and height * width > MAX_DENSE_MASK_PIXELS
            ):
                raise _MaskMutationError(
                    status_code=422,
                    reason="large_mask_full_scan_required",
                    message=(
                        "mask instance operations require a full scan that exceeds "
                        "the synchronous pixel budget"
                    ),
                    max_pixels=MAX_DENSE_MASK_PIXELS,
                )

        total_runs = 0
        for rle in rle_cache.values():
            height, width, counts = validate_coco_rle(rle)
            assert_pixel_budget(height, width)
            total_runs += len(counts)
        if total_runs > MAX_MASK_MUTATION_TOTAL_RUNS:
            raise _MaskMutationError(
                status_code=422,
                reason="operation_too_large",
                message="mask mutation exceeds the total RLE run budget",
            )
        algebra_budget = _AlgebraBudget()

        async def load_reference(reference: dict) -> dict:
            nonlocal total_runs
            key = str(reference.get("object_key") or reference.get("sha256") or "")
            cached = rle_cache.get(key)
            if cached is not None:
                return cached
            if preloaded_rles is not None:
                raise _MaskMutationError(
                    status_code=409,
                    reason="mask_reference_invalid",
                    message="mask content changed after verification",
                )
            if len(rle_cache) >= MAX_MASK_MUTATION_RLE_OBJECTS:
                raise _MaskMutationError(
                    status_code=422,
                    reason="operation_too_large",
                    message="mask mutation references too many RLE objects",
                )
            try:
                rle = await load_coco_rle(reference)
                height, width, counts = validate_coco_rle(rle)
            except Exception as exc:
                raise _MaskMutationError(
                    status_code=422,
                    reason="invalid_geometry",
                    message="mask content could not be validated",
                ) from exc
            assert_pixel_budget(height, width)
            total_runs += len(counts)
            if total_runs > MAX_MASK_MUTATION_TOTAL_RUNS:
                raise _MaskMutationError(
                    status_code=422,
                    reason="operation_too_large",
                    message="mask mutation exceeds the total RLE run budget",
                )
            rle_cache[key] = rle
            return rle

        async def load_geometry(geometry: dict, *, result: bool) -> dict:
            reference = _geometry_for_frame(geometry, payload.scope)
            if reference is None:
                raise _MaskMutationError(
                    status_code=422,
                    reason="invalid_geometry",
                    message="mutation geometry is not visible in the requested scope",
                )
            rle = await load_reference(reference)
            if coco_rle_area(rle) <= 0:
                raise _MaskMutationError(
                    status_code=422,
                    reason="empty_result" if result else "invalid_geometry",
                    message=(
                        "mask instance mutation cannot persist an empty result"
                        if result
                        else "mask mutation source contains no foreground"
                    ),
                )
            return rle

        by_id = {annotation.id: annotation for annotation in annotations}
        needs_full_scope = (
            payload.operation == "overlap" or payload.scope.strict_non_overlap
        )
        needed_source_ids = set(by_id) if needs_full_scope else source_ids
        source_rles: dict[uuid.UUID, dict] = {}
        for annotation_id in sorted(needed_source_ids, key=str):
            if payload.operation == "copy_keyframe" and annotation_id in source_ids:
                source_resolved = resolve_track_at_frame(
                    by_id[annotation_id].geometry,
                    int(payload.source_frame_index or 0),
                )
                source_reference = (source_resolved or {}).get("mask")
                if source_reference is None:
                    raise _MaskMutationError(
                        status_code=422,
                        reason="invalid_geometry",
                        message="copied source keyframe is not visible",
                    )
                source_rles[annotation_id] = await load_reference(source_reference)
            else:
                source_rles[annotation_id] = await load_geometry(
                    by_id[annotation_id].geometry,
                    result=False,
                )

        result_rles: dict[int, dict] = {}
        for index, mutation in enumerate(payload.mutations):
            if not isinstance(mutation, (MaskUpdateMutation, MaskCreateMutation)):
                continue
            result_rles[index] = await load_geometry(
                mutation.geometry.model_dump(mode="json", by_alias=True),
                result=True,
            )

        invalid_message: str | None = None
        if payload.operation in {"copy_component", "copy_keyframe"}:
            source_id = next(iter(source_ids))
            if payload.operation == "copy_keyframe":
                source_rle = source_rles[source_id]
            else:
                source_rle = source_rles[source_id]
            result_rle = next(iter(result_rles.values()))
            if payload.operation == "copy_keyframe":
                if not _rle_equal(result_rle, source_rle, algebra_budget):
                    invalid_message = "copied keyframe must equal its source snapshot"
            else:
                connectivity = payload.report.connectivity
                if connectivity not in {4, 8}:
                    invalid_message = "component copy requires 4 or 8 connectivity"
                elif not _rle_subset(result_rle, source_rle, algebra_budget):
                    invalid_message = "copied component must be a subset of its source"
                elif not _is_complete_source_component(
                    result_rle,
                    source_index=_source_component_index(
                        source_rle, connectivity, algebra_budget
                    ),
                    budget=algebra_budget,
                ):
                    invalid_message = (
                        "copied result must equal one complete source component"
                    )
        elif payload.operation == "slice_mask":
            source_rle = source_rles[next(iter(source_ids))]
            try:
                kept, created = slice_mask_rle(
                    source_rle, payload.cut_path, algebra_budget
                )
                for index, mutation in enumerate(payload.mutations):
                    expected = (
                        kept if isinstance(mutation, MaskUpdateMutation) else created
                    )
                    if not _rle_equal(result_rles[index], expected, algebra_budget):
                        invalid_message = (
                            "切割预览与像素中心分区不一致，或未按大小/左侧规则保留来源"
                        )
                        break
            except ValueError as exc:
                invalid_message = str(exc)
        elif payload.operation == "split_components":
            source_rle = source_rles[next(iter(source_ids))]
            split_results = list(result_rles.values())
            connectivity = payload.report.connectivity
            component_index = (
                _source_component_index(source_rle, connectivity, algebra_budget)
                if connectivity in {4, 8}
                else None
            )
            if component_index is None:
                invalid_message = "component split requires 4 or 8 connectivity"
            combined = split_results[0]
            for result_rle in split_results:
                if component_index is not None and not _is_complete_source_component(
                    result_rle,
                    source_index=component_index,
                    budget=algebra_budget,
                ):
                    invalid_message = (
                        "each split result must equal one complete source component"
                    )
                    break
            for result_rle in split_results[1:]:
                if invalid_message is not None:
                    break
                if _rle_intersects(combined, result_rle, algebra_budget):
                    invalid_message = "split results must not overlap"
                    break
                combined = _combine_rles(combined, result_rle, "or", algebra_budget)
            if invalid_message is None and not _rle_equal(
                combined, source_rle, algebra_budget
            ):
                invalid_message = "split results must exactly partition the source"
        elif payload.operation == "join_masks":
            joined_source = _union_rles(
                [source_rles[item] for item in sorted(source_ids, key=str)],
                algebra_budget,
            )
            joined_result = next(iter(result_rles.values()))
            if not _rle_equal(joined_source, joined_result, algebra_budget):
                invalid_message = "joined result must equal the union of all sources"
        elif payload.operation == "overlap":
            if payload.scope.overlap_policy == "allow":
                invalid_message = "overlap mutation requires an erase policy"
            primary = payload.mutations[0]
            if not isinstance(primary, MaskUpdateMutation):
                invalid_message = "overlap mutation must start with the primary update"
            if invalid_message is None:
                primary_id = primary.annotation_id
                primary_result = result_rles[0]
                targets = {
                    mutation.annotation_id: (index, mutation)
                    for index, mutation in enumerate(payload.mutations)
                    if isinstance(mutation, (MaskUpdateMutation, MaskDeleteMutation))
                }
                for annotation in annotations:
                    if annotation.id == primary_id:
                        continue
                    before = source_rles[annotation.id]
                    expected = _combine_rles(
                        before, primary_result, "and_not", algebra_budget
                    )
                    changed = _rle_changed_pixels(before, expected, algebra_budget)
                    target = targets.get(annotation.id)
                    if changed == 0:
                        if target is not None:
                            invalid_message = (
                                "overlap mutation may not rewrite a disjoint annotation"
                            )
                            break
                        continue
                    if annotation.is_locked:
                        raise _MaskMutationError(
                            status_code=409,
                            reason="annotation_locked",
                            message="a locked annotation overlaps the primary mask",
                            annotation_id=str(annotation.id),
                            unresolved=True,
                        )
                    if target is None:
                        invalid_message = "overlap mutation must include every intersecting annotation"
                        break
                    target_index, target_mutation = target
                    if coco_rle_area(expected) == 0:
                        if payload.scope.media == "video":
                            raise _MaskMutationError(
                                status_code=422,
                                reason="empty_result",
                                message=(
                                    "video overlap cannot erase a track completely on "
                                    "the current frame"
                                ),
                                annotation_id=str(annotation.id),
                            )
                        if not isinstance(target_mutation, MaskDeleteMutation):
                            invalid_message = (
                                "fully erased image masks must use a delete mutation"
                            )
                            break
                    elif not isinstance(target_mutation, MaskUpdateMutation) or not (
                        _rle_equal(result_rles[target_index], expected, algebra_budget)
                    ):
                        invalid_message = (
                            "overlap result must equal source minus the primary mask"
                        )
                        break
        else:
            # Repair payloads are built from a server-side, short-lived plan.
            # The shared mutation service still validates the exact source/result
            # RLEs, scope fingerprint, versions, locks and write contract here.
            source_rle = source_rles[next(iter(source_ids))]
            result_rle = next(iter(result_rles.values()))
            if _rle_equal(source_rle, result_rle, algebra_budget):
                invalid_message = "mask repair must change at least one pixel"
        if invalid_message is not None:
            raise _MaskMutationError(
                status_code=422,
                reason="invalid_operation",
                message=invalid_message,
            )

        ordered_source_ids = sorted(source_ids, key=str)
        source_areas = [coco_rle_area(source_rles[item]) for item in ordered_source_ids]
        result_areas = [
            coco_rle_area(result_rles[index]) for index in sorted(result_rles)
        ]
        affected: list[MaskMutationAffectedReport] = []
        changed_total = 0
        for index, mutation in enumerate(payload.mutations):
            if isinstance(mutation, MaskUpdateMutation):
                changed = _rle_changed_pixels(
                    source_rles[mutation.annotation_id],
                    result_rles[index],
                    algebra_budget,
                )
                changed_total += changed
                affected.append(
                    MaskMutationAffectedReport(
                        annotation_id=mutation.annotation_id,
                        version=next(
                            item.version
                            for item in payload.expected_versions
                            if item.annotation_id == mutation.annotation_id
                        ),
                        changed_pixels=changed,
                    )
                )
            elif isinstance(mutation, MaskDeleteMutation):
                changed = coco_rle_area(source_rles[mutation.annotation_id])
                changed_total += changed
                affected.append(
                    MaskMutationAffectedReport(
                        annotation_id=mutation.annotation_id,
                        version=next(
                            item.version
                            for item in payload.expected_versions
                            if item.annotation_id == mutation.annotation_id
                        ),
                        changed_pixels=changed,
                    )
                )

        before_rle: dict
        after_rle: dict
        if payload.operation == "join_masks":
            before_rle = _union_rles(
                [source_rles[item] for item in ordered_source_ids], algebra_budget
            )
            after_rle = next(iter(result_rles.values()))
        else:
            primary_id = ordered_source_ids[0]
            first_mutation = payload.mutations[0]
            if isinstance(first_mutation, (MaskUpdateMutation, MaskDeleteMutation)):
                primary_id = first_mutation.annotation_id
            before_rle = source_rles[primary_id]
            after_rle = next(iter(result_rles.values()))

        return (
            MaskMutationReport(
                source_areas=source_areas,
                result_areas=result_areas,
                before_area=coco_rle_area(before_rle),
                after_area=coco_rle_area(after_rle),
                changed_pixels=changed_total,
                connectivity=payload.report.connectivity,
                affected_annotations=affected,
            ),
            rle_cache,
            algebra_budget,
        )

    async def _assert_non_overlapping(
        self,
        scope: MaskMutationScope,
        annotations: list[Annotation],
        final_geometries: dict[uuid.UUID, dict],
        deleted_ids: set[uuid.UUID],
        created: list[Annotation],
        rle_cache: dict[str, dict],
        algebra_budget: _AlgebraBudget,
    ) -> None:
        if not scope.strict_non_overlap:
            return
        records: list[tuple[Annotation, dict]] = []
        for annotation in annotations:
            if annotation.id in deleted_ids:
                continue
            geometry = final_geometries.get(annotation.id, annotation.geometry)
            reference = _geometry_for_frame(geometry, scope)
            if reference is not None:
                records.append((annotation, reference))
        for annotation in created:
            reference = _geometry_for_frame(annotation.geometry, scope)
            if reference is not None:
                records.append((annotation, reference))

        materialized: list[tuple[Annotation, dict, dict[str, float]]] = []
        for annotation, reference in records:
            try:
                key = str(reference.get("object_key") or reference.get("sha256") or "")
                rle = rle_cache.get(key)
                if rle is None:
                    raise _MaskMutationError(
                        status_code=409,
                        reason="mask_reference_invalid",
                        message="overlap content changed after verification",
                    )
                bbox = coco_rle_bbox_norm(rle)
                if bbox:
                    materialized.append((annotation, rle, bbox))
            except _MaskMutationError:
                raise
            except Exception as exc:
                raise _MaskMutationError(
                    status_code=422,
                    reason="invalid_geometry",
                    message="mask content could not be validated for overlap",
                ) from exc
        materialized.sort(key=lambda item: (item[2]["x"], str(item[0].id)))
        active: list[tuple[Annotation, dict, dict[str, float]]] = []
        compared_pairs = 0
        for right_annotation, right_rle, right_bbox in materialized:
            active = [
                item for item in active if item[2]["x"] + item[2]["w"] > right_bbox["x"]
            ]
            for left_annotation, left_rle, left_bbox in active:
                if left_bbox["y"] + left_bbox["h"] <= right_bbox["y"] or (
                    right_bbox["y"] + right_bbox["h"] <= left_bbox["y"]
                ):
                    continue
                compared_pairs += 1
                if compared_pairs > MAX_MASK_MUTATION_OVERLAP_PAIRS:
                    raise _MaskMutationError(
                        status_code=422,
                        reason="operation_too_large",
                        message="strict overlap validation exceeds the pair budget",
                    )
                if not _rle_intersects(left_rle, right_rle, algebra_budget):
                    continue
                locked = next(
                    (
                        annotation
                        for annotation in (left_annotation, right_annotation)
                        if annotation.is_locked
                    ),
                    None,
                )
                if locked is not None:
                    raise _MaskMutationError(
                        status_code=409,
                        reason="annotation_locked",
                        message="a locked annotation prevents strict non-overlap",
                        annotation_id=str(locked.id),
                        unresolved=True,
                    )
                raise _MaskMutationError(
                    status_code=409,
                    reason="overlap_conflict",
                    message="strict non-overlap scope still contains overlapping masks",
                    annotation_ids=[str(left_annotation.id), str(right_annotation.id)],
                )
            active.append((right_annotation, right_rle, right_bbox))

    async def _commit(
        self,
        task_id: uuid.UUID,
        payload: MaskMutationCommitRequest,
        actor: User,
        *,
        origin: AuditOriginContext | None = None,
        access: Any = None,
    ) -> MaskMutationCommitResponse:
        digest = request_digest(payload)
        from app.services import task_access

        preflight_task = await get_core_row(
            self.db, task_row, task_id, populate_existing=True
        )
        if preflight_task is None:
            raise _MaskMutationError(
                status_code=404,
                reason="task_not_found",
                message="Task not found",
            )
        source_binding = capture_task_source_binding(preflight_task)
        preflight_access = await task_access.resolve_task_access(
            self.db, preflight_task, actor.id
        )
        await task_access.assert_task_visible(
            self.db, preflight_task, actor.id, access=preflight_access
        )
        task_access.assert_annotation_write_allowed(preflight_task, preflight_access)
        await task_access.assert_review_adjustment_evidence(
            self.db, preflight_task, actor.id, preflight_access
        )
        preflight_replay = await self._idempotent_replay(
            task_id, actor.id, payload, digest
        )
        verified_mask_content = None
        if preflight_replay is None:
            task_access.assert_task_editable(
                preflight_task, actor.id, access=preflight_access
            )
            if payload.scope.media == "video":
                preflight_project = await get_workspace_project(
                    self.db,
                    project_id=preflight_task.project_id,
                    populate_existing=True,
                )
                await self._assert_segment_lease(
                    preflight_task,
                    payload.scope,
                    actor,
                    preflight_project,
                    lock=False,
                )
            preflight_geometries = [
                mutation.geometry.model_dump(mode="json", by_alias=True)
                for mutation in payload.mutations
                if isinstance(mutation, (MaskUpdateMutation, MaskCreateMutation))
            ]
            preflight_references = [
                reference
                for geometry in preflight_geometries
                if (reference := _geometry_for_frame(geometry, payload.scope))
                is not None
            ]
            preflight_explicit_source_ids = {
                source_id
                for mutation in payload.mutations
                if isinstance(mutation, MaskCreateMutation)
                for source_id in mutation.source_annotation_ids
            }
            preflight_mutation_source_ids = preflight_explicit_source_ids | {
                mutation.annotation_id
                for mutation in payload.mutations
                if isinstance(mutation, (MaskUpdateMutation, MaskDeleteMutation))
            }
            preflight_source_ids = set(preflight_mutation_source_ids)
            preflight_annotation_map = {
                annotation.id: annotation
                for annotation in await self._lock_scope(
                    task_id,
                    payload.scope,
                    for_update=False,
                    include_annotation_ids=preflight_source_ids,
                )
            }
            preflight_fingerprint = scope_fingerprint(
                payload.scope, list(preflight_annotation_map.values())
            )
            preflight_versions = {
                str(item.id): int(item.version or 1)
                for item in preflight_annotation_map.values()
            }
            preflight_expected_versions = {
                str(item.annotation_id): item.version
                for item in payload.expected_versions
            }
            preflight_missing = sorted(
                set(preflight_versions) - set(preflight_expected_versions)
            )
            if preflight_missing:
                raise _MaskMutationError(
                    status_code=428,
                    reason="expected_versions_missing",
                    message="expected_versions must cover every annotation in scope",
                    annotation_ids=preflight_missing,
                    current_scope_fingerprint=preflight_fingerprint,
                    current_versions=preflight_versions,
                )
            if payload.scope_fingerprint != preflight_fingerprint or set(
                preflight_expected_versions
            ) != set(preflight_versions):
                raise _MaskMutationError(
                    status_code=409,
                    reason="scope_stale",
                    message="mask mutation scope changed; refresh and recompute the preview",
                    current_scope_fingerprint=preflight_fingerprint,
                    current_versions=preflight_versions,
                )
            preflight_mismatched = [
                annotation_id
                for annotation_id, version in preflight_versions.items()
                if preflight_expected_versions[annotation_id] != version
            ]
            if preflight_mismatched:
                raise _MaskMutationError(
                    status_code=409,
                    reason="version_mismatch",
                    message="one or more source annotations changed",
                    annotation_ids=preflight_mismatched,
                    current_versions=preflight_versions,
                )
            preflight_needs_full_scope = (
                payload.operation == "overlap" or payload.scope.strict_non_overlap
            )
            if preflight_needs_full_scope:
                preflight_source_ids = set(preflight_annotation_map)
            preflight_rle_references = list(preflight_references)
            for source_id in sorted(preflight_source_ids, key=str):
                annotation = preflight_annotation_map.get(source_id)
                if annotation is None:
                    continue
                if (
                    payload.operation == "copy_keyframe"
                    and source_id in preflight_explicit_source_ids
                ):
                    resolved = resolve_track_at_frame(
                        annotation.geometry,
                        int(payload.source_frame_index or 0),
                    )
                    reference = (resolved or {}).get("mask")
                else:
                    reference = _geometry_for_frame(annotation.geometry, payload.scope)
                if reference is not None:
                    preflight_rle_references.append(reference)
            preflight_source_scope = (
                payload.scope.model_copy(
                    update={"frame_index": payload.source_frame_index}
                )
                if payload.operation == "copy_keyframe"
                else payload.scope
            )
            preflight_source_keys = {
                str(reference["object_key"])
                for source_id in preflight_mutation_source_ids
                if (annotation := preflight_annotation_map.get(source_id)) is not None
                and (
                    reference := _geometry_for_frame(
                        annotation.geometry, preflight_source_scope
                    )
                )
                is not None
                and reference.get("object_key")
            }
            preflight_result_keys = {
                str(reference.get("object_key"))
                for reference in preflight_references
                if reference.get("object_key")
            }
            preflight_required_keys = sorted(
                preflight_result_keys - preflight_source_keys
            )
            if preflight_required_keys:
                preflight_owned_keys: set[str] = set()
                for start in range(
                    0, len(preflight_required_keys), MAX_MASK_MUTATION_RLE_OBJECTS
                ):
                    result = await self._owner(
                        ReadMaskUploadKeys(
                            schema=MASK_EDIT_PROTOCOL,
                            operation="read_reserved_mask_uploads",
                            task_id=task_id,
                            object_keys=preflight_required_keys[
                                start : start + MAX_MASK_MUTATION_RLE_OBJECTS
                            ],
                        )
                    )
                    preflight_owned_keys.update(result.object_keys)
                preflight_missing_keys = [
                    key
                    for key in preflight_required_keys
                    if key not in preflight_owned_keys
                ]
                if preflight_missing_keys:
                    raise _MaskMutationError(
                        status_code=422,
                        reason="missing_ref",
                        message="mask reference was not reserved by this task",
                        object_keys=preflight_missing_keys,
                    )
            if (
                len(
                    {
                        str(
                            reference.get("object_key") or reference.get("sha256") or ""
                        )
                        for reference in preflight_rle_references
                    }
                )
                > MAX_MASK_MUTATION_RLE_OBJECTS
            ):
                raise _MaskMutationError(
                    status_code=422,
                    reason="operation_too_large",
                    message="mask mutation references too many RLE objects",
                )
            try:
                verified_mask_content = await preflight_mask_payload_for_write(
                    self.db,
                    preflight_task,
                    [
                        *preflight_geometries,
                        *(
                            preflight_annotation_map[source_id].geometry
                            for source_id in preflight_source_ids
                            if source_id in preflight_annotation_map
                        ),
                    ],
                    reference_value=preflight_rle_references,
                )
            except RasterMaskContractError as exc:
                _raise_mask_mutation_contract_error(exc)
        access = await task_access.resolve_task_access(
            self.db, preflight_task, actor.id, lock_membership=True
        )
        task = await self._lock_task(task_id)
        await task_access.assert_task_visible(self.db, task, actor.id, access=access)
        task_access.assert_annotation_write_allowed(task, access)
        # The FastAPI dependency precheck is not authoritative: re-read the
        # freshly locked task and revalidate the round's frozen non-self
        # evidence before an idempotent replay or any mutation.
        await task_access.assert_review_adjustment_evidence(
            self.db, task, actor.id, access
        )
        if preflight_replay is None and not task_source_binding_matches(
            task, source_binding
        ):
            raise _MaskMutationError(
                status_code=409,
                reason="source_changed",
                message="Task media source changed during mask verification",
            )
        replay = await self._idempotent_replay(task_id, actor.id, payload, digest)
        if replay is not None:
            return replay
        task_access.assert_task_editable(task, actor.id, access=access)

        if payload.operation == "slice_mask" and task.file_type != "image":
            raise _MaskMutationError(
                status_code=422,
                reason="unsupported_media",
                message="切割仅支持图片任务",
            )

        try:
            await assert_task_lock_for_legacy_video(self.db, task, actor.id)
        except TaskLockConflictError as exc:
            raise _MaskMutationError(
                status_code=409,
                reason="task_lock_conflict",
                message="task is locked by another user",
            ) from exc

        # Read the bounded scope before acquiring Annotation row locks.  The Task
        # row above serializes every mask persistence path; RLE/upload locks must
        # come before Annotation locks to match GC and ordinary writes.
        explicit_source_ids = (
            {
                source_id
                for mutation in payload.mutations
                if isinstance(mutation, MaskCreateMutation)
                for source_id in mutation.source_annotation_ids
            }
            if payload.operation == "copy_keyframe"
            else set()
        )
        annotations = await self._lock_scope(
            task_id,
            payload.scope,
            for_update=False,
            include_annotation_ids=explicit_source_ids,
        )
        current_fingerprint = scope_fingerprint(payload.scope, annotations)
        current_versions = {
            str(item.id): int(item.version or 1) for item in annotations
        }
        expected_versions = {
            str(item.annotation_id): item.version for item in payload.expected_versions
        }
        missing = sorted(set(current_versions) - set(expected_versions))
        if missing:
            raise _MaskMutationError(
                status_code=428,
                reason="expected_versions_missing",
                message="expected_versions must cover every annotation in scope",
                annotation_ids=missing,
                current_scope_fingerprint=current_fingerprint,
                current_versions=current_versions,
            )
        if payload.scope_fingerprint != current_fingerprint or set(
            expected_versions
        ) != set(current_versions):
            raise _MaskMutationError(
                status_code=409,
                reason="scope_stale",
                message="mask mutation scope changed; refresh and recompute the preview",
                current_scope_fingerprint=current_fingerprint,
                current_versions=current_versions,
            )
        mismatched = [
            annotation_id
            for annotation_id, version in current_versions.items()
            if expected_versions[annotation_id] != version
        ]
        if mismatched:
            raise _MaskMutationError(
                status_code=409,
                reason="version_mismatch",
                message="one or more source annotations changed",
                annotation_ids=mismatched,
                current_versions=current_versions,
            )

        by_id = {annotation.id: annotation for annotation in annotations}
        source_ids: set[uuid.UUID] = set()
        target_ids: set[uuid.UUID] = set()
        geometries: list[dict] = []
        for mutation in payload.mutations:
            if isinstance(mutation, MaskCreateMutation):
                source_ids.update(mutation.source_annotation_ids)
                geometries.append(
                    mutation.geometry.model_dump(mode="json", by_alias=True)
                )
            else:
                target_ids.add(mutation.annotation_id)
                source_ids.add(mutation.annotation_id)
                if isinstance(mutation, MaskUpdateMutation):
                    geometries.append(
                        mutation.geometry.model_dump(mode="json", by_alias=True)
                    )

        unknown = sorted(
            str(item) for item in source_ids | target_ids if item not in by_id
        )
        if unknown:
            raise _MaskMutationError(
                status_code=409,
                reason="scope_stale",
                message="mutation references annotations outside the current scope",
                annotation_ids=unknown,
                current_scope_fingerprint=current_fingerprint,
            )
        _validate_operation_shape(payload, source_ids)

        if payload.operation == "slice_mask":
            source = by_id[next(iter(source_ids))]
            if source.was_cancelled or source.sensor_role is not None:
                raise _MaskMutationError(
                    status_code=409,
                    reason="annotation_locked",
                    message="切割对象不可编辑",
                )
            child = (
                await execute_core_read(
                    self.db,
                    select(annotation_row.id)
                    .where(
                        annotation_row.parent_annotation_id == source.id,
                        annotation_row.is_active.is_(True),
                    )
                    .limit(1),
                )
            ).scalar_one_or_none()
            if child:
                raise _MaskMutationError(
                    status_code=409,
                    reason="active_children",
                    message="有活动子对象的标注不能切割",
                )
            if source.parent_annotation_id is not None:
                await AnnotationService(self.db)._validate_parent_annotation(
                    task_id, source.parent_annotation_id
                )

        delete_ids = {
            item.annotation_id
            for item in payload.mutations
            if isinstance(item, MaskDeleteMutation)
        }
        if delete_ids:
            child_ids = list(
                (
                    await execute_core_read(
                        self.db,
                        select(annotation_row.id)
                        .where(
                            annotation_row.parent_annotation_id.in_(delete_ids),
                            annotation_row.is_active.is_(True),
                        )
                        .order_by(annotation_row.id),
                    )
                )
                .scalars()
                .all()
            )
            locked_children = await lock_annotation_rows_for_core_read(
                self.db,
                child_ids,
                lock_mode="update",
                nowait=False,
                populate_existing=True,
            )
            child = next(
                (
                    row.id
                    for row in locked_children
                    if row.parent_annotation_id in delete_ids and row.is_active
                ),
                None,
            )
            if child is not None:
                raise _MaskMutationError(
                    status_code=422,
                    reason="invalid_operation",
                    message="mask mutation cannot delete an annotation with active children",
                    child_annotation_id=str(child),
                )

        source_classes = {by_id[item].class_name for item in source_ids}
        require_same_class = payload.operation != "overlap" or (
            payload.scope.overlap_policy != "erase_all"
        )
        if require_same_class and len(source_classes) != 1:
            raise _MaskMutationError(
                status_code=422,
                reason="class_mismatch",
                message="mask instance operations require one shared class",
            )
        if payload.scope.class_name and source_classes != {payload.scope.class_name}:
            raise _MaskMutationError(
                status_code=422,
                reason="class_mismatch",
                message="source class does not match mutation scope",
            )
        for annotation_id in source_ids | target_ids:
            if by_id[annotation_id].is_locked:
                raise _MaskMutationError(
                    status_code=409,
                    reason="annotation_locked",
                    message="annotation is locked",
                    annotation_id=str(annotation_id),
                )

        project = await get_workspace_project(
            self.db, project_id=task.project_id, populate_existing=True
        )
        await self._assert_segment_lease(task, payload.scope, actor, project)

        frame_index = int(payload.scope.frame_index or 0)
        for mutation in payload.mutations:
            geometry = (
                mutation.geometry.model_dump(mode="json", by_alias=True)
                if isinstance(mutation, (MaskUpdateMutation, MaskCreateMutation))
                else None
            )
            if geometry is None:
                continue
            expected_type = (
                "raster_mask" if payload.scope.media == "image" else "video_track_mask"
            )
            if geometry.get("type") != expected_type:
                raise _MaskMutationError(
                    status_code=422,
                    reason="invalid_geometry",
                    message=f"{payload.scope.media} scope requires {expected_type} geometry",
                )
            if (
                isinstance(mutation, MaskUpdateMutation)
                and payload.scope.media == "video"
            ):
                _validate_video_update_scope(
                    by_id[mutation.annotation_id].geometry,
                    geometry,
                    frame_index,
                )
            if (
                isinstance(mutation, MaskCreateMutation)
                and payload.scope.media == "video"
            ):
                keyframes = geometry.get("keyframes") or []
                if (
                    len(keyframes) != 1
                    or int(keyframes[0].get("frame_index", -1)) != frame_index
                    or geometry.get("outside")
                ):
                    raise _MaskMutationError(
                        status_code=422,
                        reason="invalid_geometry",
                        message="new video mask instance must contain only the current keyframe",
                    )

        submitted_references = [
            reference
            for geometry in geometries
            if (reference := _geometry_for_frame(geometry, payload.scope)) is not None
        ]
        submitted_keys = {
            str(reference["object_key"])
            for reference in submitted_references
            if reference.get("object_key")
        }
        source_scope = (
            payload.scope.model_copy(update={"frame_index": payload.source_frame_index})
            if payload.operation == "copy_keyframe"
            else payload.scope
        )
        source_keys = {
            str(reference["object_key"])
            for annotation_id in source_ids
            if (
                reference := _geometry_for_frame(
                    by_id[annotation_id].geometry, source_scope
                )
            )
            is not None
            and reference.get("object_key")
        }
        if not submitted_keys:
            raise _MaskMutationError(
                status_code=422,
                reason="missing_ref",
                message="mask mutation geometry must contain a content reference",
            )
        try:
            await prepare_mask_payload_for_write(
                self.db,
                task,
                geometries,
                reference_value=submitted_references,
                required_upload_keys=submitted_keys - source_keys,
                verified_content=verified_mask_content,
                require_verified_content=True,
            )
        except RasterMaskContractError as exc:
            _raise_mask_mutation_contract_error(exc)

        locked_annotations = await self._lock_scope(
            task_id,
            payload.scope,
            include_annotation_ids=explicit_source_ids,
        )
        locked_fingerprint = scope_fingerprint(payload.scope, locked_annotations)
        locked_versions = {
            str(item.id): int(item.version or 1) for item in locked_annotations
        }
        if (
            locked_fingerprint != current_fingerprint
            or locked_versions != current_versions
        ):
            raise _MaskMutationError(
                status_code=409,
                reason="scope_stale",
                message="mask mutation scope changed while committing",
                current_scope_fingerprint=locked_fingerprint,
                current_versions=locked_versions,
            )
        annotations = locked_annotations
        by_id = {annotation.id: annotation for annotation in annotations}

        verified_report, rle_cache, algebra_budget = await self._validate_pixel_algebra(
            payload,
            annotations,
            source_ids,
            preloaded_rles=(
                dict(verified_mask_content.rle_cache)
                if verified_mask_content is not None
                else None
            ),
        )

        before_snapshot = [
            {
                "id": str(annotation.id),
                "version": int(annotation.version or 1),
                "geometry": annotation.geometry,
            }
            for annotation in annotations
        ]
        slice_before = (
            {
                str(source_id): mask_version_snapshot(by_id[source_id])
                for source_id in source_ids
            }
            if payload.operation == "slice_mask"
            else None
        )
        updated: list[Annotation] = []
        created: list[Annotation] = []
        created_from: list[tuple[Annotation, Annotation]] = []
        deleted: list[Annotation] = []
        final_geometries: dict[uuid.UUID, dict] = {}
        changes = []
        for mutation in payload.mutations:
            if isinstance(mutation, MaskUpdateMutation):
                annotation = by_id[mutation.annotation_id]
                geometry, track_id = prepare_compact_track_identity(
                    mutation.geometry.model_dump(mode="json", by_alias=True)
                )
                changes.append(
                    UpdateMaskAnnotation(
                        action="update",
                        annotation_id=annotation.id,
                        geometry=geometry,
                        track_id=track_id,
                    )
                )
                final_geometries[annotation.id] = geometry
                updated.append(annotation)
            elif isinstance(mutation, MaskCreateMutation):
                source = by_id[mutation.source_annotation_ids[0]]
                created_geometry = mutation.geometry.model_dump(
                    mode="json", by_alias=True
                )
                if payload.scope.media == "video":
                    created_geometry["track_id"] = _new_track_id()
                    created_geometry["keyframes"] = [
                        {**created_geometry["keyframes"][0], "source": "manual"}
                    ]
                geometry, track_id = prepare_compact_track_identity(created_geometry)
                annotation = Annotation(
                    id=uuid.uuid4(),
                    task_id=task.id,
                    project_id=task.project_id,
                    video_segment_id=source.video_segment_id,
                    user_id=actor.id,
                    source="manual",
                    annotation_type=str(geometry.get("type")),
                    tool_unit_id=source.tool_unit_id,
                    class_name=source.class_name,
                    geometry=geometry,
                    track_id=track_id,
                    confidence=None,
                    attributes=deepcopy(source.attributes or {}),
                    attributes_meta=deepcopy(source.attributes_meta or {}),
                    z_order=source.z_order,
                    parent_annotation_id=source.parent_annotation_id
                    if payload.operation == "slice_mask"
                    else None,
                )
                changes.append(
                    CreateMaskAnnotation(
                        action="create",
                        annotation_id=annotation.id,
                        source_annotation_id=source.id,
                        geometry=geometry,
                        track_id=track_id,
                    )
                )
                created.append(annotation)
                if payload.operation == "slice_mask":
                    created_from.append((source, annotation))
            else:
                annotation = by_id[mutation.annotation_id]
                changes.append(
                    DeactivateMaskAnnotation(
                        action="deactivate", annotation_id=annotation.id
                    )
                )
                deleted.append(annotation)

        persisted = await self._owner(
            ApplyMaskMutations(
                schema=MASK_EDIT_PROTOCOL,
                operation="apply_mask_mutations",
                **mask_task_fence(task, actor.id),
                mutation_kind=payload.operation,
                expected_versions=expected_versions,
                changes=changes,
            )
        )
        result_by_id = {row.annotation_id: row for row in persisted.annotations}
        for annotation in updated:
            row = result_by_id[annotation.id]
            for field in (
                "geometry",
                "annotation_type",
                "track_id",
                "user_id",
                "version",
                "is_active",
                "updated_at",
            ):
                set_committed_value(annotation, field, getattr(row, field))
        for annotation in created:
            row = result_by_id[annotation.id]
            for field in (
                "annotation_type",
                "track_id",
                "user_id",
                "geometry",
                "confidence",
                "attributes",
                "attributes_meta",
                "z_order",
                "parent_annotation_id",
                "version",
                "is_active",
                "updated_at",
            ):
                setattr(annotation, field, getattr(row, field))
        for annotation in deleted:
            row = result_by_id[annotation.id]
            set_committed_value(annotation, "is_active", row.is_active)
            set_committed_value(annotation, "version", row.version)
            set_committed_value(annotation, "updated_at", row.updated_at)
        await self.db.flush()
        if created_from:
            from app.services.accepted_inputs import copy_annotation_input_sources

            for source_annotation, created_annotation in created_from:
                await copy_annotation_input_sources(
                    self.db,
                    source_annotation_id=source_annotation.id,
                    target_annotation_id=created_annotation.id,
                )
            if payload.scope.media == "video":
                from app.services.accepted_video_inputs import (
                    copy_annotation_video_input_sources,
                )
                from app.services.accepted_tracker_inputs import (
                    copy_annotation_video_tracker_input_sources,
                )

                for source_annotation, created_annotation in created_from:
                    await copy_annotation_video_input_sources(
                        self.db,
                        source_annotation_id=source_annotation.id,
                        target_annotation_id=created_annotation.id,
                    )
                    await copy_annotation_video_tracker_input_sources(
                        self.db,
                        actor_id=actor.id,
                        source_annotation_id=source_annotation.id,
                        target_annotation_id=created_annotation.id,
                    )
        await self._assert_non_overlapping(
            payload.scope,
            annotations,
            final_geometries,
            {item.id for item in deleted},
            created,
            rle_cache,
            algebra_budget,
        )
        await record_annotation_actor(self.db, task, actor.id)
        await AnnotationService(self.db)._update_task_stats(task_id)
        await heartbeat_task_lock_for_legacy_video(self.db, task, actor.id)

        operation_id = uuid.uuid4()
        result_versions = {
            str(annotation.id): int(annotation.version or 1)
            for annotation in [*updated, *created, *deleted]
        }
        result_ids = [item.id for item in [*updated, *created]]
        lineage: list[AnnotationLineage] = []
        relation = {
            "slice_mask": "split",
            "split_components": "split",
            "copy_component": "copied",
            "copy_keyframe": "keyframe_copied",
            "join_masks": "joined",
            "overlap": "overlap_erased",
            "delete_small_islands": "mask_repaired",
            "fill_small_holes": "mask_repaired",
            "resolve_same_class_overlap": "mask_repaired",
            "mask_repair_rollback": "mask_repair_rolled_back",
        }[payload.operation]
        if payload.operation in {
            "slice_mask",
            "split_components",
            "copy_component",
            "copy_keyframe",
            "join_masks",
        }:
            for source_id in sorted(source_ids, key=str):
                for result_id in result_ids:
                    lineage.append(
                        AnnotationLineage(
                            source_annotation_id=source_id,
                            result_annotation_id=result_id,
                            relation=relation,
                            source_version=expected_versions[str(source_id)],
                            result_version=result_versions[str(result_id)],
                            frame_index=payload.scope.frame_index,
                        )
                    )
        elif payload.operation == "overlap":
            for annotation in updated:
                lineage.append(
                    AnnotationLineage(
                        source_annotation_id=annotation.id,
                        result_annotation_id=annotation.id,
                        relation=relation,
                        source_version=expected_versions[str(annotation.id)],
                        result_version=result_versions[str(annotation.id)],
                        frame_index=payload.scope.frame_index,
                    )
                )
            for annotation in deleted:
                lineage.append(
                    AnnotationLineage(
                        source_annotation_id=annotation.id,
                        result_annotation_id=None,
                        relation="overlap_erased",
                        source_version=expected_versions[str(annotation.id)],
                        result_version=result_versions[str(annotation.id)],
                        frame_index=payload.scope.frame_index,
                    )
                )
        else:
            for annotation in updated:
                lineage.append(
                    AnnotationLineage(
                        source_annotation_id=annotation.id,
                        result_annotation_id=annotation.id,
                        relation=relation,
                        source_version=expected_versions[str(annotation.id)],
                        result_version=result_versions[str(annotation.id)],
                        frame_index=payload.scope.frame_index,
                    )
                )
        now = datetime.now(timezone.utc)
        slice_receipt = None
        operation_report = verified_report.model_dump(mode="json")
        if slice_before is not None:
            source, new = updated[0], created[0]
            slice_before[str(new.id)] = mask_version_snapshot(new, active=False)
            operation_report.update(
                {
                    "slice_snapshot_schema": 2,
                    "before": slice_before,
                    "after": {
                        str(row.id): mask_version_snapshot(row) for row in (source, new)
                    },
                    "current_side": "after",
                    "current_versions": result_versions,
                    "cut_path": payload.model_dump(mode="json")["cut_path"],
                }
            )
            slice_receipt = AnnotationSliceResponse(
                operation_id=operation_id,
                slice_operation_id=operation_id,
                source_annotation_id=source.id,
                created_annotation_id=new.id,
                result_versions=result_versions,
                active_annotation_ids=[source.id, new.id],
                target="after",
                restore_expires_at=now + _SLICE_RESTORE_TTL,
            )
            await protect_mask_slice_versions(
                self.db, task_id, operation_report, slice_receipt.restore_expires_at
            )
        operation = AnnotationOperation(
            id=operation_id,
            task_id=task_id,
            actor_id=actor.id,
            kind=payload.operation,
            idempotency_key=payload.idempotency_key,
            request_digest=digest,
            scope_fingerprint=payload.scope_fingerprint,
            source_versions={
                str(item.annotation_id): item.version
                for item in payload.expected_versions
            },
            result_versions=result_versions,
            report=operation_report,
            created_at=now,
            status="committed",
            response_json={},
        )
        after_snapshot = [
            {
                "id": str(annotation.id),
                "version": int(annotation.version or 1),
                "geometry": annotation.geometry,
                "active": annotation.is_active,
            }
            for annotation in [
                *[
                    item
                    for item in annotations
                    if item.id not in {row.id for row in deleted}
                ],
                *created,
            ]
        ]
        audit = await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.ANNOTATION_MASK_MUTATION,
            target_type="annotation_operation",
            target_id=operation_id,
            origin=origin,
            status_code=200,
            detail={
                "task_id": str(task_id),
                "operation": payload.operation,
                "updated": [str(item.id) for item in updated],
                "created": [str(item.id) for item in created],
                "deleted": [str(item.id) for item in deleted],
                "report": verified_report.model_dump(mode="json"),
            },
        )
        edge_out = [
            MaskLineageEdgeOut(
                source_annotation_id=item.source_annotation_id,
                result_annotation_id=item.result_annotation_id,
                relation=item.relation,
                source_version=item.source_version,
                result_version=item.result_version,
                frame_index=item.frame_index,
            )
            for item in lineage
        ]
        response = MaskMutationCommitResponse(
            operation_id=operation_id,
            updated_annotations=[
                MaskMutationAnnotationResult(id=item.id, version=int(item.version or 1))
                for item in updated
            ],
            created_annotations=[
                MaskMutationAnnotationResult(id=item.id, version=int(item.version or 1))
                for item in created
            ],
            deleted_annotation_ids=[item.id for item in deleted],
            result_versions=result_versions,
            lineage_edges=edge_out,
            before_digest=_canonical_digest(before_snapshot),
            after_digest=_canonical_digest(after_snapshot),
            audit_id=audit.id,
            slice_restore=slice_receipt,
        )
        operation.response_json = response.model_dump(mode="json")
        await self._owner(
            WriteAnnotationOperation(
                schema=MASK_EDIT_PROTOCOL,
                operation="write_operation",
                **mask_task_fence(task, actor.id),
                operation_id=operation_id,
                kind=operation.kind,
                idempotency_key=operation.idempotency_key,
                request_digest=operation.request_digest,
                scope_fingerprint=operation.scope_fingerprint,
                source_versions=operation.source_versions,
                result_versions=operation.result_versions,
                report=operation.report,
                response_json=operation.response_json,
                created_at=operation.created_at,
                completed_at=operation.completed_at,
                lineage=lineage,
                original_operation_id=None,
                original_report=None,
            )
        )
        await self.db.flush()
        return response
