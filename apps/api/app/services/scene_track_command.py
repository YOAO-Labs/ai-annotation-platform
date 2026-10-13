"""Previewed, idempotent and reversible commands for 3D Scene Tracks."""

from __future__ import annotations

from collections import Counter
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.scene_track import (
    SceneTrack,
    SceneTrackInterval,
    SceneTrackOperation,
)
from app.db.models.task import Task
from app.schemas.scene_track import SceneTrackCommandRequest
from app.services.annotation_evidence import (
    lock_tasks_for_evidence,
    record_annotation_actors_for_tasks,
)
from app.services.track_operation import SceneTrackContext, resolve_scene_track_context


MAX_COMMAND_MEMBERS = 5000


def _error(status_code: int, reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"reason": reason, "message": message},
    )


@dataclass(frozen=True)
class IntervalSpec:
    start_frame: int
    end_frame: int | None
    source: str = "manual"


@dataclass(frozen=True)
class LoadedCommandTrack:
    track: SceneTrack
    intervals: tuple[SceneTrackInterval, ...]
    members: tuple[Annotation, ...]


@dataclass(frozen=True)
class PreparedSceneTrackCommand:
    context: SceneTrackContext
    request: SceneTrackCommandRequest
    primary: LoadedCommandTrack
    secondary: LoadedCommandTrack | None
    snapshot_token: str
    source_revisions: dict[str, int]
    before_intervals: dict[str, tuple[IntervalSpec, ...]]
    after_intervals: dict[str, tuple[IntervalSpec, ...]]
    affected_members: tuple[Annotation, ...]
    impact_frames: tuple[int, ...]
    impact_by_role: dict[str, int]
    requires_confirmation: bool
    created_track_id: str | None

    @property
    def task_ids(self) -> frozenset[uuid.UUID]:
        task_ids = {row.task_id for row in self.primary.members}
        if self.secondary is not None:
            task_ids.update(row.task_id for row in self.secondary.members)
        if self.request.kind == "resume" and self.request.resume_frame is not None:
            task_id = next(
                (
                    task_id
                    for task_id, frame in self.context.task_to_frame.items()
                    if frame == self.request.resume_frame
                ),
                None,
            )
            if task_id is not None:
                task_ids.add(task_id)
        return frozenset(task_ids)


def _canonical_request(request: SceneTrackCommandRequest) -> dict:
    return request.model_dump(mode="json", exclude_none=True)


def _snapshot_request(request: SceneTrackCommandRequest) -> dict:
    return request.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"confirm_member_deactivation"},
    )


def request_digest(request: SceneTrackCommandRequest) -> str:
    encoded = json.dumps(
        _canonical_request(request), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _frame_for(context: SceneTrackContext, annotation: Annotation) -> int:
    frame = context.task_to_frame.get(annotation.task_id)
    if frame is None:
        raise _error(
            409,
            "track_crosses_scene",
            "Scene Track contains a member outside the current Scene",
        )
    return frame


def _contains(spec: IntervalSpec, frame: int) -> bool:
    return spec.start_frame <= frame and (
        spec.end_frame is None or frame <= spec.end_frame
    )


def _end_value(value: int | None) -> int:
    return value if value is not None else 2**31 - 1


def _normalize(specs: list[IntervalSpec]) -> tuple[IntervalSpec, ...]:
    ordered = sorted(
        specs, key=lambda row: (row.start_frame, _end_value(row.end_frame))
    )
    normalized: list[IntervalSpec] = []
    for spec in ordered:
        if spec.start_frame < 0 or (
            spec.end_frame is not None and spec.end_frame < spec.start_frame
        ):
            raise _error(422, "interval_invalid", "Scene Track interval is invalid")
        if not normalized:
            normalized.append(spec)
            continue
        previous = normalized[-1]
        previous_end = _end_value(previous.end_frame)
        if spec.start_frame <= previous_end + 1:
            merged_end = max(previous_end, _end_value(spec.end_frame))
            normalized[-1] = IntervalSpec(
                previous.start_frame,
                None if merged_end == 2**31 - 1 else merged_end,
                previous.source,
            )
        else:
            normalized.append(spec)
    return tuple(normalized)


def _subtract(
    specs: tuple[IntervalSpec, ...], start: int, end: int | None
) -> tuple[IntervalSpec, ...]:
    removal_end = _end_value(end)
    result: list[IntervalSpec] = []
    for spec in specs:
        spec_end = _end_value(spec.end_frame)
        if spec_end < start or spec.start_frame > removal_end:
            result.append(spec)
            continue
        if spec.start_frame < start:
            result.append(IntervalSpec(spec.start_frame, start - 1, spec.source))
        if spec_end > removal_end and end is not None:
            result.append(IntervalSpec(end + 1, spec.end_frame, spec.source))
    return _normalize(result)


def _interval_specs(track: LoadedCommandTrack) -> tuple[IntervalSpec, ...]:
    return tuple(
        IntervalSpec(row.start_frame, row.end_frame, row.source)
        for row in track.intervals
    )


async def _load_track(
    db: AsyncSession,
    *,
    context: SceneTrackContext,
    track_id: str,
    for_update: bool,
) -> LoadedCommandTrack:
    query = (
        select(SceneTrack)
        .where(SceneTrack.project_id == context.project_id)
        .where(SceneTrack.scene_id == context.scene_id)
        .where(SceneTrack.track_id == track_id)
    )
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    track = (await db.execute(query)).scalar_one_or_none()
    if track is None:
        raise _error(404, "track_not_found", "3D Scene Track was not found")
    if track.retired_at is not None:
        raise _error(409, "track_retired", "retired Scene Track cannot be changed")

    interval_query = (
        select(SceneTrackInterval)
        .where(SceneTrackInterval.scene_track_id == track.id)
        .order_by(SceneTrackInterval.start_frame, SceneTrackInterval.id)
    )
    member_query = (
        select(Annotation)
        .where(Annotation.scene_track_id == track.id)
        .where(Annotation.was_cancelled.is_(False))
        .order_by(Annotation.id)
        .limit(MAX_COMMAND_MEMBERS + 1)
    )
    if for_update:
        interval_query = interval_query.with_for_update().execution_options(
            populate_existing=True
        )
        member_query = member_query.with_for_update().execution_options(
            populate_existing=True
        )
    intervals = tuple((await db.execute(interval_query)).scalars())
    members = tuple((await db.execute(member_query)).scalars())
    if len(members) > MAX_COMMAND_MEMBERS:
        raise _error(
            422,
            "track_member_limit_exceeded",
            f"Scene Track command cannot exceed {MAX_COMMAND_MEMBERS} members",
        )
    for member in members:
        if member.track_id != track.track_id or member.class_name != track.class_name:
            raise _error(
                409,
                "member_identity_mismatch",
                "Scene Track member identity does not match its Track",
            )
        _frame_for(context, member)
    if any(member.is_locked for member in members):
        raise _error(
            409,
            "annotation_locked",
            "unlock every Scene Track member before changing its lifecycle",
        )
    return LoadedCommandTrack(track=track, intervals=intervals, members=members)


def _snapshot_token(
    *,
    context: SceneTrackContext,
    request: SceneTrackCommandRequest,
    tracks: list[LoadedCommandTrack],
) -> str:
    payload = {
        "contract_version": 1,
        "scene_id": str(context.scene_id),
        "request": _snapshot_request(request),
        "tracks": [
            {
                "id": str(track.track.id),
                "track_id": track.track.track_id,
                "revision": track.track.revision,
                "presence_mode": track.track.presence_mode,
                "retired_at": (
                    track.track.retired_at.isoformat()
                    if track.track.retired_at is not None
                    else None
                ),
                "intervals": [
                    {
                        "id": str(row.id),
                        "start": row.start_frame,
                        "end": row.end_frame,
                        "version": row.version,
                    }
                    for row in track.intervals
                ],
                "members": [
                    {
                        "id": str(row.id),
                        "version": int(row.version or 1),
                        "task_id": str(row.task_id),
                        "track_id": row.track_id,
                        "scene_track_id": str(row.scene_track_id),
                        "class_name": row.class_name,
                        "is_active": row.is_active,
                        "temporal_role": row.temporal_role,
                        "sensor_role": row.sensor_role,
                    }
                    for row in track.members
                ],
            }
            for track in tracks
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _active_members(track: LoadedCommandTrack) -> list[Annotation]:
    return [row for row in track.members if row.is_active]


def _active_primary_members(track: LoadedCommandTrack) -> list[Annotation]:
    return [
        row
        for row in track.members
        if row.is_active
        and row.sensor_role is None
        and (row.geometry or {}).get("type") == "box_3d"
    ]


def _validate_single_active_member_per_frame(
    context: SceneTrackContext, rows: list[Annotation]
) -> None:
    seen: set[int] = set()
    for row in rows:
        frame = _frame_for(context, row)
        if frame in seen:
            raise _error(
                409,
                "track_duplicate_frame",
                "Scene Track has more than one active member on a frame",
            )
        seen.add(frame)


async def prepare_scene_track_command(
    db: AsyncSession,
    *,
    anchor_task: Task,
    request: SceneTrackCommandRequest,
    for_update: bool = False,
) -> PreparedSceneTrackCommand:
    context = await resolve_scene_track_context(db, anchor_task)
    if for_update:
        # Keep the global Task -> SceneTrack/Annotation lock order. Lock the
        # union of every involved track's member tasks in one stable order
        # before any Track row lock, so swapped primary/secondary merges cannot
        # deadlock.
        involved_track_ids = [request.track_id]
        if request.secondary_track_id is not None:
            involved_track_ids.append(request.secondary_track_id)
        member_task_ids = (
            (
                await db.execute(
                    select(Annotation.task_id)
                    .join(SceneTrack, SceneTrack.id == Annotation.scene_track_id)
                    .where(
                        SceneTrack.scene_id == context.scene_id,
                        SceneTrack.track_id.in_(involved_track_ids),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        await lock_tasks_for_evidence(db, member_task_ids, nowait=True)
    primary = await _load_track(
        db,
        context=context,
        track_id=request.track_id,
        for_update=for_update,
    )
    secondary = None
    if request.secondary_track_id is not None:
        secondary = await _load_track(
            db,
            context=context,
            track_id=request.secondary_track_id,
            for_update=for_update,
        )

    tracks = [primary, *([secondary] if secondary is not None else [])]
    snapshot = _snapshot_token(context=context, request=request, tracks=tracks)
    primary_specs = _interval_specs(primary)
    before = {primary.track.track_id: primary_specs}
    after = dict(before)
    affected: list[Annotation] = []
    impact_frames: list[int] = []
    created_track_id: str | None = None

    if request.kind == "split":
        assert request.frame_index is not None
        if request.frame_index != context.anchor_frame:
            raise _error(
                422,
                "split_anchor_mismatch",
                "frame_index must equal the anchor task frame",
            )
        active = _active_primary_members(primary)
        _validate_single_active_member_per_frame(context, active)
        tail = [
            row
            for row in primary.members
            if _frame_for(context, row) > request.frame_index
        ]
        if not any(
            row.is_active
            and row.sensor_role is None
            and (row.geometry or {}).get("type") == "box_3d"
            for row in tail
        ):
            raise _error(
                409,
                "split_tail_missing",
                "the selected Track has no active members after the anchor frame",
            )
        created_track_id = (
            f"trk_{hashlib.sha256(('split:' + snapshot).encode()).hexdigest()[:32]}"
        )
        head_specs = _subtract(primary_specs, request.frame_index + 1, None)
        tail_specs: list[IntervalSpec] = []
        for spec in primary_specs:
            spec_end = _end_value(spec.end_frame)
            if spec_end <= request.frame_index:
                continue
            tail_specs.append(
                IntervalSpec(
                    max(spec.start_frame, request.frame_index + 1),
                    spec.end_frame,
                    spec.source,
                )
            )
        after = {
            primary.track.track_id: head_specs,
            created_track_id: _normalize(tail_specs),
        }
        affected = list(primary.members)
        impact_frames = [_frame_for(context, row) for row in tail]

    elif request.kind == "merge":
        assert secondary is not None
        if primary.track.class_name != secondary.track.class_name:
            raise _error(
                409,
                "track_class_conflict",
                "only Scene Tracks with the same class can be merged",
            )
        primary_active = _active_primary_members(primary)
        secondary_active = _active_primary_members(secondary)
        _validate_single_active_member_per_frame(context, primary_active)
        _validate_single_active_member_per_frame(context, secondary_active)
        primary_frames = {_frame_for(context, row) for row in primary_active}
        secondary_frames = {_frame_for(context, row) for row in secondary_active}
        if primary_frames & secondary_frames:
            raise _error(
                409,
                "track_frame_conflict",
                "Tracks with an active member on the same frame cannot be merged",
            )
        primary_camera_keys = {
            (_frame_for(context, row), row.sensor_role)
            for row in _active_members(primary)
            if row.sensor_role is not None
        }
        secondary_camera_keys = {
            (_frame_for(context, row), row.sensor_role)
            for row in _active_members(secondary)
            if row.sensor_role is not None
        }
        if primary_camera_keys & secondary_camera_keys:
            raise _error(
                409,
                "track_camera_member_conflict",
                "Tracks with an active member for the same frame and camera cannot be merged",
            )
        secondary_specs = _interval_specs(secondary)
        before[secondary.track.track_id] = secondary_specs
        after = {
            primary.track.track_id: _normalize([*primary_specs, *secondary_specs]),
            secondary.track.track_id: (),
        }
        affected = [*primary.members, *secondary.members]
        impact_frames = [_frame_for(context, row) for row in secondary.members]

    elif request.kind == "mark_absent":
        assert request.frame_index is not None
        if not any(_contains(spec, request.frame_index) for spec in primary_specs):
            raise _error(
                409,
                "track_already_absent",
                "Scene Track is already absent on the selected frame",
            )
        if (
            request.resume_frame is not None
            and request.resume_frame <= request.frame_index
        ):
            raise _error(
                422,
                "resume_frame_invalid",
                "resume_frame must be after the absence start frame",
            )
        removal_end = (
            request.resume_frame - 1 if request.resume_frame is not None else None
        )
        after = {
            primary.track.track_id: _subtract(
                primary_specs, request.frame_index, removal_end
            )
        }
        affected = [
            row
            for row in _active_members(primary)
            if _frame_for(context, row) >= request.frame_index
            and (removal_end is None or _frame_for(context, row) <= removal_end)
        ]
        impact_frames = [_frame_for(context, row) for row in affected]

    elif request.kind == "terminate":
        assert request.frame_index is not None
        if not any(_contains(spec, request.frame_index) for spec in primary_specs):
            raise _error(
                409,
                "track_not_present",
                "Scene Track is not present on the selected frame",
            )
        after = {
            primary.track.track_id: _subtract(
                primary_specs, request.frame_index + 1, None
            )
        }
        affected = [
            row
            for row in _active_members(primary)
            if _frame_for(context, row) > request.frame_index
        ]
        impact_frames = [_frame_for(context, row) for row in affected]

    else:
        assert request.kind == "resume"
        assert request.resume_frame is not None
        assert request.source_annotation_id is not None
        if any(_contains(spec, request.resume_frame) for spec in primary_specs):
            raise _error(
                409,
                "track_already_present",
                "Scene Track is already present on the resume frame",
            )
        target_task_id = next(
            (
                task_id
                for task_id, frame in context.task_to_frame.items()
                if frame == request.resume_frame
            ),
            None,
        )
        if target_task_id is None:
            raise _error(
                422,
                "resume_frame_unavailable",
                "resume frame does not have a task in this Scene",
            )
        source = next(
            (
                row
                for row in primary.members
                if row.id == request.source_annotation_id
                and row.sensor_role is None
                and (row.geometry or {}).get("type") == "box_3d"
            ),
            None,
        )
        if source is None or not source.is_active:
            raise _error(
                409,
                "resume_source_unavailable",
                "resume source must be an active member of this Track",
            )
        if any(
            row.is_active
            and row.task_id == target_task_id
            and row.sensor_role is None
            and (row.geometry or {}).get("type") == "box_3d"
            for row in primary.members
        ):
            raise _error(
                409,
                "resume_member_exists",
                "resume frame already has an active member for this Track",
            )
        after = {
            primary.track.track_id: _normalize(
                [
                    *primary_specs,
                    IntervalSpec(
                        request.resume_frame,
                        next(
                            (
                                spec.start_frame - 1
                                for spec in primary_specs
                                if spec.start_frame > request.resume_frame
                            ),
                            max(context.task_to_frame.values()),
                        ),
                    ),
                ]
            )
        }
        affected = [source]
        impact_frames = [request.resume_frame]

    impact_by_role = dict(
        sorted(Counter(row.temporal_role for row in affected).items())
    )
    requires_confirmation = request.kind in {"mark_absent", "terminate"} and any(
        row.temporal_role in {"keyframe", "sample"} for row in affected
    )
    return PreparedSceneTrackCommand(
        context=context,
        request=request,
        primary=primary,
        secondary=secondary,
        snapshot_token=snapshot,
        source_revisions={
            item.track.track_id: int(item.track.revision or 1) for item in tracks
        },
        before_intervals=before,
        after_intervals=after,
        affected_members=tuple(affected),
        impact_frames=tuple(sorted(set(impact_frames))),
        impact_by_role=impact_by_role,
        requires_confirmation=requires_confirmation,
        created_track_id=created_track_id,
    )


def _interval_state(row: SceneTrackInterval) -> dict:
    return {
        "id": str(row.id),
        "start_frame": row.start_frame,
        "end_frame": row.end_frame,
        "source": row.source,
        "version": int(row.version or 1),
    }


def _member_state(row: Annotation) -> dict:
    return {
        "id": str(row.id),
        "task_id": str(row.task_id),
        "track_id": row.track_id,
        "scene_track_id": str(row.scene_track_id) if row.scene_track_id else None,
        "class_name": row.class_name,
        "is_active": row.is_active,
        "is_hidden": row.is_hidden,
        "temporal_role": row.temporal_role,
        "sensor_role": row.sensor_role,
        "version": int(row.version or 1),
    }


async def _capture_state(
    db: AsyncSession, *, context: SceneTrackContext, track_ids: set[str]
) -> dict:
    tracks = list(
        (
            await db.execute(
                select(SceneTrack)
                .where(SceneTrack.project_id == context.project_id)
                .where(SceneTrack.scene_id == context.scene_id)
                .where(SceneTrack.track_id.in_(sorted(track_ids)))
                .order_by(SceneTrack.track_id)
            )
        ).scalars()
    )
    result: dict[str, dict] = {}
    for track in tracks:
        intervals = list(
            (
                await db.execute(
                    select(SceneTrackInterval)
                    .where(SceneTrackInterval.scene_track_id == track.id)
                    .order_by(SceneTrackInterval.start_frame, SceneTrackInterval.id)
                )
            ).scalars()
        )
        members = list(
            (
                await db.execute(
                    select(Annotation)
                    .where(Annotation.scene_track_id == track.id)
                    .where(Annotation.was_cancelled.is_(False))
                    .order_by(Annotation.id)
                )
            ).scalars()
        )
        result[track.track_id] = {
            "id": str(track.id),
            "track_id": track.track_id,
            "class_name": track.class_name,
            "presence_mode": track.presence_mode,
            "attributes": copy.deepcopy(track.attributes or {}),
            "attributes_meta": copy.deepcopy(track.attributes_meta or {}),
            "revision": int(track.revision or 1),
            "retired_at": track.retired_at.isoformat() if track.retired_at else None,
            "intervals": [_interval_state(row) for row in intervals],
            "members": [_member_state(row) for row in members],
        }
    return {"tracks": result}


async def _replace_intervals(
    db: AsyncSession,
    *,
    track: SceneTrack,
    specs: tuple[IntervalSpec, ...],
    actor_id: uuid.UUID,
    operation_id: uuid.UUID,
) -> None:
    current = list(
        (
            await db.execute(
                select(SceneTrackInterval)
                .where(SceneTrackInterval.scene_track_id == track.id)
                .with_for_update()
            )
        ).scalars()
    )
    for row in current:
        await db.delete(row)
    await db.flush()
    for spec in specs:
        db.add(
            SceneTrackInterval(
                id=uuid.uuid4(),
                scene_track_id=track.id,
                start_frame=spec.start_frame,
                end_frame=spec.end_frame,
                source=spec.source,
                version=1,
                created_by=actor_id,
                operation_id=operation_id,
            )
        )
    await db.flush()


def _copy_resume_member(
    *, source: Annotation, target_task_id: uuid.UUID, actor_id: uuid.UUID
) -> Annotation:
    return Annotation(
        id=uuid.uuid4(),
        task_id=target_task_id,
        project_id=source.project_id,
        user_id=actor_id,
        source="manual",
        annotation_type=source.annotation_type,
        tool_unit_id=source.tool_unit_id,
        class_name=source.class_name,
        geometry=copy.deepcopy(source.geometry),
        track_id=source.track_id,
        scene_track_id=source.scene_track_id,
        temporal_role="keyframe",
        attributes=copy.deepcopy(source.attributes or {}),
        attributes_meta=copy.deepcopy(source.attributes_meta or {}),
        z_order=source.z_order,
        is_active=True,
        is_hidden=False,
        version=1,
    )


async def apply_scene_track_command(
    db: AsyncSession,
    *,
    prepared: PreparedSceneTrackCommand,
    expected_snapshot_token: str,
    actor_id: uuid.UUID,
    idempotency_key: str,
) -> tuple[SceneTrackOperation, dict]:
    digest = request_digest(prepared.request)
    existing = (
        await db.execute(
            select(SceneTrackOperation)
            .where(SceneTrackOperation.scene_id == prepared.context.scene_id)
            .where(SceneTrackOperation.actor_id == actor_id)
            .where(SceneTrackOperation.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_digest != digest:
            raise _error(
                409,
                "idempotency_key_reused",
                "idempotency key was already used for a different command",
            )
        return existing, copy.deepcopy(existing.response_json)

    if prepared.snapshot_token != expected_snapshot_token:
        raise _error(
            409,
            "track_snapshot_stale",
            "Scene Track changed after preview; preview the command again",
        )
    if (
        prepared.requires_confirmation
        and not prepared.request.confirm_member_deactivation
    ):
        raise _error(
            409,
            "member_deactivation_confirmation_required",
            "keyframe or sample members require explicit confirmation",
        )

    operation_id = uuid.uuid4()
    before_ids = set(prepared.before_intervals)
    before_state = await _capture_state(
        db, context=prepared.context, track_ids=before_ids
    )
    operation = SceneTrackOperation(
        id=operation_id,
        scene_id=prepared.context.scene_id,
        actor_id=actor_id,
        kind=prepared.request.kind,
        idempotency_key=idempotency_key,
        request_digest=digest,
        snapshot_token=prepared.snapshot_token,
        source_revisions=prepared.source_revisions,
        result_revisions={},
        before_state=before_state,
        after_state={},
        inverse_payload={"restore": before_state},
        response_json={},
        status="committed",
    )
    db.add(operation)
    await db.flush()

    primary_track = prepared.primary.track
    affected_track_ids = set(prepared.after_intervals)
    created_track: SceneTrack | None = None
    resume_task_id: uuid.UUID | None = None
    if prepared.request.kind == "split":
        assert prepared.created_track_id is not None
        created_track = SceneTrack(
            id=uuid.uuid4(),
            project_id=primary_track.project_id,
            scene_id=primary_track.scene_id,
            track_id=prepared.created_track_id,
            class_name=primary_track.class_name,
            presence_mode="explicit",
            attributes=copy.deepcopy(primary_track.attributes or {}),
            attributes_meta=copy.deepcopy(primary_track.attributes_meta or {}),
            revision=1,
            created_by=actor_id,
        )
        db.add(created_track)
        await db.flush()
        assert prepared.request.frame_index is not None
        for row in prepared.primary.members:
            if _frame_for(prepared.context, row) > prepared.request.frame_index:
                row.track_id = created_track.track_id
                row.scene_track_id = created_track.id
            row.version = int(row.version or 1) + 1
        primary_track.revision = int(primary_track.revision or 1) + 1
        primary_track.presence_mode = "explicit"
        await _replace_intervals(
            db,
            track=primary_track,
            specs=prepared.after_intervals[primary_track.track_id],
            actor_id=actor_id,
            operation_id=operation_id,
        )
        await _replace_intervals(
            db,
            track=created_track,
            specs=prepared.after_intervals[created_track.track_id],
            actor_id=actor_id,
            operation_id=operation_id,
        )

    elif prepared.request.kind == "merge":
        assert prepared.secondary is not None
        secondary_track = prepared.secondary.track
        for row in prepared.primary.members:
            row.version = int(row.version or 1) + 1
        for row in prepared.secondary.members:
            row.track_id = primary_track.track_id
            row.scene_track_id = primary_track.id
            row.version = int(row.version or 1) + 1
        primary_track.revision = int(primary_track.revision or 1) + 1
        primary_track.presence_mode = "explicit"
        secondary_track.revision = int(secondary_track.revision or 1) + 1
        secondary_track.presence_mode = "explicit"
        secondary_track.retired_at = datetime.now(timezone.utc)
        await _replace_intervals(
            db,
            track=primary_track,
            specs=prepared.after_intervals[primary_track.track_id],
            actor_id=actor_id,
            operation_id=operation_id,
        )
        await _replace_intervals(
            db,
            track=secondary_track,
            specs=(),
            actor_id=actor_id,
            operation_id=operation_id,
        )

    elif prepared.request.kind in {"mark_absent", "terminate"}:
        for row in prepared.affected_members:
            row.is_active = False
            row.is_hidden = True
            row.version = int(row.version or 1) + 1
        primary_track.revision = int(primary_track.revision or 1) + 1
        primary_track.presence_mode = "explicit"
        await _replace_intervals(
            db,
            track=primary_track,
            specs=prepared.after_intervals[primary_track.track_id],
            actor_id=actor_id,
            operation_id=operation_id,
        )

    else:
        assert prepared.request.kind == "resume"
        assert prepared.request.resume_frame is not None
        target_task_id = next(
            task_id
            for task_id, frame in prepared.context.task_to_frame.items()
            if frame == prepared.request.resume_frame
        )
        source = prepared.affected_members[0]
        resume_task_id = target_task_id
        db.add(
            _copy_resume_member(
                source=source, target_task_id=target_task_id, actor_id=actor_id
            )
        )
        primary_track.revision = int(primary_track.revision or 1) + 1
        primary_track.presence_mode = "explicit"
        await _replace_intervals(
            db,
            track=primary_track,
            specs=prepared.after_intervals[primary_track.track_id],
            actor_id=actor_id,
            operation_id=operation_id,
        )

    await db.flush()
    # A2 · every scene-track member write is annotation-phase work by the actor.
    scene_affected_tasks = {member.task_id for member in prepared.affected_members}
    scene_affected_tasks |= {member.task_id for member in prepared.primary.members}
    if prepared.secondary is not None:
        scene_affected_tasks |= {
            member.task_id for member in prepared.secondary.members
        }
    if resume_task_id is not None:
        scene_affected_tasks.add(resume_task_id)
    await record_annotation_actors_for_tasks(
        db, scene_affected_tasks, actor_id, nowait=True
    )
    result_revisions: dict[str, int] = {}
    tracks_for_result = [primary_track]
    if prepared.secondary is not None:
        tracks_for_result.append(prepared.secondary.track)
    if created_track is not None:
        tracks_for_result.append(created_track)
    for track in tracks_for_result:
        result_revisions[track.track_id] = int(track.revision or 1)
    after_state = await _capture_state(
        db,
        context=prepared.context,
        track_ids=affected_track_ids,
    )
    response = {
        "contract_version": 1,
        "kind": prepared.request.kind,
        "scene_id": str(prepared.context.scene_id),
        "scene_name": prepared.context.scene_name,
        "track_id": primary_track.track_id,
        "secondary_track_id": prepared.request.secondary_track_id,
        "frame_index": prepared.request.frame_index,
        "resume_frame": prepared.request.resume_frame,
        "source_revisions": prepared.source_revisions,
        "before_intervals": interval_specs_as_response(prepared.before_intervals),
        "after_intervals": interval_specs_as_response(prepared.after_intervals),
        "affected_members": {
            "total": len(prepared.affected_members),
            "by_temporal_role": prepared.impact_by_role,
            "frames": list(prepared.impact_frames),
            "requires_confirmation": prepared.requires_confirmation,
        },
        "snapshot_token": prepared.snapshot_token,
        "operation_id": str(operation.id),
        "status": "committed",
        "created_track_id": created_track.track_id if created_track else None,
        "result_revisions": result_revisions,
    }
    operation.result_revisions = result_revisions
    operation.after_state = after_state
    operation.response_json = response
    await db.flush()
    return operation, response


def interval_specs_as_response(
    mapping: dict[str, tuple[IntervalSpec, ...]],
) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for track_id, specs in mapping.items():
        result[track_id] = [
            {
                "id": str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"scene-track-preview:{track_id}:{row.start_frame}:{row.end_frame}:{row.source}",
                    )
                ),
                "start_frame": row.start_frame,
                "end_frame": row.end_frame,
                "source": row.source,
                "version": 1,
            }
            for row in specs
        ]
    return result


def preview_payload(prepared: PreparedSceneTrackCommand) -> dict:
    return {
        "kind": prepared.request.kind,
        "scene_id": prepared.context.scene_id,
        "scene_name": prepared.context.scene_name,
        "track_id": prepared.primary.track.track_id,
        "secondary_track_id": prepared.request.secondary_track_id,
        "frame_index": prepared.request.frame_index,
        "resume_frame": prepared.request.resume_frame,
        "source_revisions": prepared.source_revisions,
        "before_intervals": interval_specs_as_response(prepared.before_intervals),
        "after_intervals": interval_specs_as_response(prepared.after_intervals),
        "affected_members": {
            "total": len(prepared.affected_members),
            "by_temporal_role": prepared.impact_by_role,
            "frames": list(prepared.impact_frames),
            "requires_confirmation": prepared.requires_confirmation,
        },
        "snapshot_token": prepared.snapshot_token,
    }


async def revert_scene_track_operation(
    db: AsyncSession,
    *,
    operation: SceneTrackOperation,
    actor_id: uuid.UUID,
    idempotency_key: str,
) -> tuple[SceneTrackOperation, dict]:
    existing = (
        await db.execute(
            select(SceneTrackOperation)
            .where(SceneTrackOperation.scene_id == operation.scene_id)
            .where(SceneTrackOperation.actor_id == actor_id)
            .where(SceneTrackOperation.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.kind != "revert" or existing.request_digest != str(operation.id):
            raise _error(
                409,
                "idempotency_key_reused",
                "idempotency key was already used for a different command",
            )
        return existing, copy.deepcopy(existing.response_json)
    if operation.kind == "revert":
        raise _error(
            409, "revert_of_revert_unsupported", "revert operations cannot be reverted"
        )
    if operation.status != "committed":
        raise _error(
            409, "operation_already_reverted", "operation was already reverted"
        )

    before_tracks: dict[str, dict] = operation.before_state.get("tracks", {})
    after_tracks: dict[str, dict] = operation.after_state.get("tracks", {})
    # Keep Task -> SceneTrack/Annotation order: lock the member task rows before
    # locking the Track rows below.
    revert_task_ids: set[uuid.UUID] = set()
    for tracks in (before_tracks, after_tracks):
        for state in tracks.values():
            for member in state.get("members", []):
                raw = member.get("task_id")
                if not raw:
                    continue
                try:
                    revert_task_ids.add(uuid.UUID(str(raw)))
                except (ValueError, AttributeError, TypeError):
                    continue
    await lock_tasks_for_evidence(db, revert_task_ids, nowait=True)
    all_track_ids = set(before_tracks) | set(after_tracks)
    current_tracks = list(
        (
            await db.execute(
                select(SceneTrack)
                .where(SceneTrack.scene_id == operation.scene_id)
                .where(SceneTrack.track_id.in_(sorted(all_track_ids)))
                .order_by(SceneTrack.track_id)
                .with_for_update()
            )
        ).scalars()
    )
    current_by_key = {row.track_id: row for row in current_tracks}
    for track_id, expected_revision in operation.result_revisions.items():
        track = current_by_key.get(track_id)
        if track is None or int(track.revision or 1) != int(expected_revision):
            raise _error(
                409,
                "operation_revert_stale",
                "Scene Track changed after this operation and cannot be reverted safely",
            )
    expected_members = {
        member["id"]: member
        for state in after_tracks.values()
        for member in state.get("members", [])
    }
    member_ids = [uuid.UUID(value) for value in expected_members]
    current_members = (
        list(
            (
                await db.execute(
                    select(Annotation)
                    .where(Annotation.id.in_(member_ids))
                    .order_by(Annotation.id)
                    .with_for_update()
                )
            ).scalars()
        )
        if member_ids
        else []
    )
    current_member_by_id = {str(row.id): row for row in current_members}
    for member_id, expected in expected_members.items():
        row = current_member_by_id.get(member_id)
        if row is None or int(row.version or 1) != int(expected["version"]):
            raise _error(
                409,
                "operation_revert_stale",
                "Scene Track member changed after this operation",
            )

    revert_id = uuid.uuid4()
    revert = SceneTrackOperation(
        id=revert_id,
        scene_id=operation.scene_id,
        actor_id=actor_id,
        kind="revert",
        idempotency_key=idempotency_key,
        request_digest=str(operation.id),
        snapshot_token=operation.snapshot_token,
        source_revisions=operation.result_revisions,
        result_revisions={},
        before_state=operation.after_state,
        after_state={},
        inverse_payload={"operation_id": str(operation.id)},
        response_json={},
        status="committed",
    )
    db.add(revert)
    await db.flush()

    # Restore or recreate the Tracks that existed before the command.
    restored_by_key: dict[str, SceneTrack] = {}
    for track_id, state in before_tracks.items():
        track = current_by_key.get(track_id)
        if track is None:
            raise _error(
                409,
                "operation_revert_track_missing",
                "a required Scene Track was deleted after the operation",
            )
        track.class_name = state["class_name"]
        track.presence_mode = state.get("presence_mode", "inferred")
        track.attributes = copy.deepcopy(state.get("attributes") or {})
        track.attributes_meta = copy.deepcopy(state.get("attributes_meta") or {})
        track.retired_at = (
            datetime.fromisoformat(state["retired_at"])
            if state.get("retired_at")
            else None
        )
        track.revision = int(track.revision or 1) + 1
        restored_by_key[track_id] = track

    # A split-created Track has no before state. Remove its intervals after members move back,
    # then retire it instead of deleting audit-visible identity.
    for track_id, track in current_by_key.items():
        if track_id not in before_tracks:
            track.revision = int(track.revision or 1) + 1
            track.retired_at = datetime.now(timezone.utc)

    before_member_states = {
        member["id"]: member
        for state in before_tracks.values()
        for member in state.get("members", [])
    }
    all_member_ids = set(before_member_states) | set(expected_members)
    all_rows = (
        list(
            (
                await db.execute(
                    select(Annotation)
                    .where(
                        Annotation.id.in_(
                            [uuid.UUID(value) for value in all_member_ids]
                        )
                    )
                    .order_by(Annotation.id)
                    .with_for_update()
                )
            ).scalars()
        )
        if all_member_ids
        else []
    )
    by_member_id = {str(row.id): row for row in all_rows}
    for member_id, state in before_member_states.items():
        row = by_member_id.get(member_id)
        if row is None:
            raise _error(
                409,
                "operation_revert_member_missing",
                "a required Scene Track member was deleted after the operation",
            )
        target_track = restored_by_key[state["track_id"]]
        row.track_id = state["track_id"]
        row.scene_track_id = target_track.id
        row.class_name = state["class_name"]
        row.is_active = bool(state["is_active"])
        row.is_hidden = bool(state["is_hidden"])
        row.temporal_role = state["temporal_role"]
        row.version = int(row.version or 1) + 1

    # Resume creates one member absent from before_state; keep it as inactive history.
    for member_id in set(expected_members) - set(before_member_states):
        row = by_member_id.get(member_id)
        if row is not None:
            row.is_active = False
            row.is_hidden = True
            row.version = int(row.version or 1) + 1

    for track_id, track in current_by_key.items():
        state = before_tracks.get(track_id)
        specs = (
            tuple(
                IntervalSpec(item["start_frame"], item.get("end_frame"), item["source"])
                for item in state.get("intervals", [])
            )
            if state is not None
            else ()
        )
        await _replace_intervals(
            db,
            track=track,
            specs=specs,
            actor_id=actor_id,
            operation_id=revert_id,
        )

    operation.status = "reverted"
    operation.reverted_by_operation_id = revert.id
    result_revisions = {
        track_id: int(track.revision or 1) for track_id, track in current_by_key.items()
    }
    context_track = next(iter(current_by_key.values()))
    context = SceneTrackContext(
        project_id=context_track.project_id,
        scene_id=operation.scene_id,
        scene_name=None,
        anchor_frame=0,
        task_to_frame={},
    )
    after_state = await _capture_state(
        db, context=context, track_ids=set(current_by_key)
    )
    original_response = copy.deepcopy(operation.response_json)
    response = {
        **original_response,
        "kind": "revert",
        "operation_id": str(revert.id),
        "status": "committed",
        "created_track_id": None,
        "source_revisions": operation.result_revisions,
        "result_revisions": result_revisions,
        "before_intervals": original_response.get("after_intervals", {}),
        "after_intervals": original_response.get("before_intervals", {}),
    }
    revert.result_revisions = result_revisions
    revert.after_state = after_state
    revert.response_json = response
    await record_annotation_actors_for_tasks(
        db, {row.task_id for row in all_rows}, actor_id, nowait=True
    )
    await db.flush()
    return revert, response
