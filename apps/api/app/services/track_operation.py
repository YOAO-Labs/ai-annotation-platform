"""3D Scene 轨迹拆分 / 合并的快照与原子改写。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes
from sqlalchemy.orm.util import identity_key

from aap_labeling.scene_read_contract import SceneAnnotationRecord
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.schemas.track_operation import TrackOperationRequest, TrackSummary
from app.services.labeling_scene_read import read_scene_data
from app.services.labeling_scene import (
    get_scene_frame_task_map,
    resolve_task_scene_frames,
)


CONTRACT_VERSION = 1
MAX_TRACK_MEMBERS = 5000
MAX_CANDIDATE_SCAN = 80
MAX_CANDIDATE_ROWS = 20_000


def _error(status_code: int, reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"reason": reason, "message": message},
    )


@dataclass(frozen=True)
class SceneTrackContext:
    project_id: uuid.UUID
    scene_id: uuid.UUID
    scene_name: str | None
    anchor_frame: int
    task_to_frame: dict[uuid.UUID, int]


@dataclass(frozen=True)
class ValidatedTrack:
    summary: TrackSummary
    rows: tuple[SceneAnnotationRecord, ...]
    frames: frozenset[int]
    task_ids: frozenset[uuid.UUID]


@dataclass(frozen=True)
class PreparedTrackOperation:
    context: SceneTrackContext
    request: TrackOperationRequest
    primary: ValidatedTrack
    secondary: ValidatedTrack | None
    snapshot_token: str
    affected_member_count: int
    rewritten_member_count: int

    @property
    def task_ids(self) -> frozenset[uuid.UUID]:
        task_ids = set(self.primary.task_ids)
        if self.secondary is not None:
            task_ids.update(self.secondary.task_ids)
        return frozenset(task_ids)


@dataclass(frozen=True)
class CandidateTracks:
    primary: ValidatedTrack
    candidates: tuple[ValidatedTrack, ...]
    truncated: bool


async def resolve_scene_track_context(
    db: AsyncSession, anchor_task: Task
) -> SceneTrackContext:
    scene_frame = (await resolve_task_scene_frames(db, [anchor_task.id]))[
        anchor_task.id
    ]
    if scene_frame.scene_id is None or scene_frame.frame_index is None:
        raise _error(
            422,
            "scene_required",
            "track operations require a task with a Scene frame",
        )
    frame_task_map = await get_scene_frame_task_map(db, scene_frame.scene_id)
    return SceneTrackContext(
        project_id=anchor_task.project_id,
        scene_id=scene_frame.scene_id,
        scene_name=scene_frame.scene_name,
        anchor_frame=scene_frame.frame_index,
        task_to_frame={task_id: frame for frame, task_id in frame_task_map.items()},
    )


async def _load_rows(
    db: AsyncSession,
    *,
    context: SceneTrackContext,
    track_ids: list[str],
    for_update: bool,
) -> list[SceneAnnotationRecord]:
    result = await read_scene_data(
        db,
        "lock_track_members" if for_update else "read_track_members",
        project_id=context.project_id,
        track_ids=track_ids,
        limit=MAX_TRACK_MEMBERS + 1,
    )
    rows = list(result.members)
    if for_update:
        for row in rows:
            instance = db.sync_session.identity_map.get(
                identity_key(Annotation, ident=row.id)
            )
            if instance is None:
                continue
            for field, value in row.model_dump(mode="python").items():
                if field != "frame_index":
                    attributes.set_committed_value(instance, field, value)
    if len(rows) > MAX_TRACK_MEMBERS:
        raise _error(
            422,
            "track_member_limit_exceeded",
            f"track operation cannot exceed {MAX_TRACK_MEMBERS} active members",
        )
    return rows


def _validate_track(
    *,
    context: SceneTrackContext,
    track_id: str,
    rows: list[SceneAnnotationRecord],
) -> ValidatedTrack:
    track_rows = [row for row in rows if row.track_id == track_id]
    if not track_rows:
        raise _error(404, "track_not_found", "3D track was not found")
    if any((row.geometry or {}).get("type") != "box_3d" for row in track_rows):
        raise _error(
            409,
            "track_geometry_unsupported",
            "track contains a non-box_3d annotation",
        )
    if any(row.is_locked for row in track_rows):
        raise _error(
            409,
            "annotation_locked",
            "unlock every track member before changing track identity",
        )
    class_names = {row.class_name for row in track_rows}
    if len(class_names) != 1:
        raise _error(
            409,
            "track_class_drift",
            "track members must share one class before identity changes",
        )

    frame_rows: dict[int, SceneAnnotationRecord] = {}
    for row in track_rows:
        frame_index = context.task_to_frame.get(row.task_id)
        if frame_index is None:
            raise _error(
                409,
                "track_crosses_scene",
                "track contains a member outside the current Scene",
            )
        if frame_index in frame_rows:
            raise _error(
                409,
                "track_duplicate_frame",
                "track contains more than one active member on a frame",
            )
        frame_rows[frame_index] = row

    frames = frozenset(frame_rows)
    class_name = next(iter(class_names))
    return ValidatedTrack(
        summary=TrackSummary(
            track_id=track_id,
            class_name=class_name,
            member_count=len(track_rows),
            first_frame=min(frames),
            last_frame=max(frames),
        ),
        rows=tuple(track_rows),
        frames=frames,
        task_ids=frozenset(row.task_id for row in track_rows),
    )


def _snapshot_token(
    *,
    request: TrackOperationRequest,
    context: SceneTrackContext,
    tracks: list[ValidatedTrack],
) -> str:
    frozen_rows = []
    for track in tracks:
        for row in track.rows:
            frozen_rows.append(
                {
                    "annotation_id": str(row.id),
                    "version": int(row.version or 1),
                    "task_id": str(row.task_id),
                    "frame_index": context.task_to_frame[row.task_id],
                    "track_id": row.track_id,
                    "class_name": row.class_name,
                }
            )
    frozen_rows.sort(key=lambda row: row["annotation_id"])
    payload = {
        "contract_version": CONTRACT_VERSION,
        "scene_id": str(context.scene_id),
        "operation": request.operation,
        "primary_track_id": request.primary_track_id,
        "secondary_track_id": request.secondary_track_id,
        "split_after_frame": request.split_after_frame,
        "members": frozen_rows,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


async def prepare_track_operation(
    db: AsyncSession,
    *,
    anchor_task: Task,
    request: TrackOperationRequest,
    for_update: bool = False,
) -> PreparedTrackOperation:
    context = await resolve_scene_track_context(db, anchor_task)
    if (
        request.operation == "split"
        and request.split_after_frame != context.anchor_frame
    ):
        raise _error(
            422,
            "split_anchor_mismatch",
            "split_after_frame must equal the anchor task frame",
        )

    track_ids = [request.primary_track_id]
    if request.secondary_track_id is not None:
        track_ids.append(request.secondary_track_id)
    track_ids = sorted(track_ids)
    rows = await _load_rows(
        db,
        context=context,
        track_ids=track_ids,
        for_update=for_update,
    )
    primary = _validate_track(
        context=context,
        track_id=request.primary_track_id,
        rows=rows,
    )
    if context.anchor_frame not in primary.frames:
        raise _error(
            409,
            "primary_track_not_on_anchor_frame",
            "the selected track is not present on the anchor frame",
        )

    secondary = None
    if request.operation == "split":
        assert request.split_after_frame is not None
        tail_frames = {
            frame for frame in primary.frames if frame > request.split_after_frame
        }
        if not tail_frames:
            raise _error(
                409,
                "split_tail_missing",
                "the selected track has no members after the anchor frame",
            )
        rewritten_member_count = len(tail_frames)
    else:
        assert request.secondary_track_id is not None
        secondary = _validate_track(
            context=context,
            track_id=request.secondary_track_id,
            rows=rows,
        )
        if primary.summary.class_name != secondary.summary.class_name:
            raise _error(
                409,
                "track_class_conflict",
                "only tracks with the same class can be merged",
            )
        if primary.frames & secondary.frames:
            raise _error(
                409,
                "track_frame_conflict",
                "tracks with an active member on the same frame cannot be merged",
            )
        rewritten_member_count = secondary.summary.member_count

    tracks = [primary, *([secondary] if secondary is not None else [])]
    return PreparedTrackOperation(
        context=context,
        request=request,
        primary=primary,
        secondary=secondary,
        snapshot_token=_snapshot_token(
            request=request,
            context=context,
            tracks=tracks,
        ),
        affected_member_count=sum(track.summary.member_count for track in tracks),
        rewritten_member_count=rewritten_member_count,
    )


async def list_structural_merge_candidates(
    db: AsyncSession,
    *,
    anchor_task: Task,
    primary_track_id: str,
    limit: int,
) -> tuple[SceneTrackContext, CandidateTracks]:
    context = await resolve_scene_track_context(db, anchor_task)
    primary_rows = await _load_rows(
        db,
        context=context,
        track_ids=[primary_track_id],
        for_update=False,
    )
    primary = _validate_track(
        context=context,
        track_id=primary_track_id,
        rows=primary_rows,
    )
    if context.anchor_frame not in primary.frames:
        raise _error(
            409,
            "primary_track_not_on_anchor_frame",
            "the selected track is not present on the anchor frame",
        )

    scan_limit = min(MAX_CANDIDATE_SCAN, max(limit * 4, limit))
    scene_task_ids = list(context.task_to_frame)
    counts = await read_scene_data(
        db,
        "candidate_track_counts",
        project_id=context.project_id,
        task_ids=scene_task_ids,
        class_name=primary.summary.class_name,
        exclude_track_id=primary_track_id,
        limit=scan_limit + 1,
    )
    count_rows = [(row.track_id, row.member_count) for row in counts.candidate_counts]
    truncated = len(count_rows) > scan_limit
    candidate_ids = [
        str(track_id)
        for track_id, count in count_rows[:scan_limit]
        if int(count) <= MAX_TRACK_MEMBERS
    ]
    if not candidate_ids:
        return context, CandidateTracks(
            primary=primary,
            candidates=(),
            truncated=truncated,
        )

    candidate_result = await read_scene_data(
        db,
        "read_track_members",
        project_id=context.project_id,
        track_ids=candidate_ids,
        limit=MAX_CANDIDATE_ROWS + 1,
    )
    candidate_rows = list(candidate_result.members)
    if len(candidate_rows) > MAX_CANDIDATE_ROWS:
        truncated = True
        candidate_rows = candidate_rows[:MAX_CANDIDATE_ROWS]
        last_track_id = candidate_rows[-1].track_id
        candidate_rows = [
            row for row in candidate_rows if row.track_id != last_track_id
        ]

    candidates: list[ValidatedTrack] = []
    for track_id in candidate_ids:
        rows = [row for row in candidate_rows if row.track_id == track_id]
        if not rows:
            continue
        try:
            candidate = _validate_track(
                context=context,
                track_id=track_id,
                rows=rows,
            )
        except HTTPException:
            continue
        if candidate.summary.class_name != primary.summary.class_name:
            continue
        if candidate.frames & primary.frames:
            continue
        candidates.append(candidate)

    if len(candidates) > limit:
        truncated = True
    return context, CandidateTracks(
        primary=primary,
        candidates=tuple(candidates[:limit]),
        truncated=truncated,
    )
