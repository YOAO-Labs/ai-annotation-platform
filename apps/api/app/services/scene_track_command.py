"""Previewed, idempotent and reversible commands for 3D Scene Tracks."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.scene_track_command_contract import (
    SceneTrackCommandFailure,
    SceneTrackCommandOperation,
    SceneTrackCommandUnavailable,
    scene_track_command,
)
from aap_labeling.scene_track_command_data import SceneTrackCommandDataClient

from app.db.models.annotation import Annotation
from app.db.models.scene_track import (
    SceneTrack,
    SceneTrackInterval,
)
from app.db.models.task import Task
from app.schemas.scene_track import SceneTrackCommandRequest
from app.services.annotation_evidence import record_annotation_actors_for_tasks
from app.services.track_operation import SceneTrackContext, resolve_scene_track_context


MAX_COMMAND_MEMBERS = 5000
_SCENE_TRACK_COMMAND_STEP = text(
    "SELECT aap_labeling_v1.scene_track_command(CAST(:request AS jsonb))"
)


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


@dataclass(frozen=True)
class SceneTrackOperationReceipt:
    id: uuid.UUID
    kind: str
    status: str
    created_at: datetime
    completed_at: datetime
    response_json: dict
    before_state: dict | None
    after_state: dict | None
    task_ids: tuple[uuid.UUID, ...]


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
) -> LoadedCommandTrack:
    query = (
        select(SceneTrack)
        .where(SceneTrack.project_id == context.project_id)
        .where(SceneTrack.scene_id == context.scene_id)
        .where(SceneTrack.track_id == track_id)
    )
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
) -> PreparedSceneTrackCommand:
    context = await resolve_scene_track_context(db, anchor_task)
    primary = await _load_track(
        db,
        context=context,
        track_id=request.track_id,
    )
    secondary = None
    if request.secondary_track_id is not None:
        secondary = await _load_track(
            db,
            context=context,
            track_id=request.secondary_track_id,
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


def _write_snapshot(prepared: PreparedSceneTrackCommand) -> list[dict]:
    tracks = [prepared.primary]
    if prepared.secondary is not None:
        tracks.append(prepared.secondary)
    return [
        {
            "id": track.track.id,
            "track_id": track.track.track_id,
            "revision": int(track.track.revision or 1),
            "presence_mode": track.track.presence_mode,
            "retired_at": track.track.retired_at,
            "intervals": [_interval_state(row) for row in track.intervals],
            "members": [_member_state(row) for row in track.members],
        }
        for track in tracks
    ]


def _interval_sets(prepared: PreparedSceneTrackCommand) -> list[dict]:
    return [
        {
            "track_id": track_id,
            "intervals": [
                {
                    "start_frame": row.start_frame,
                    "end_frame": row.end_frame,
                    "source": row.source,
                }
                for row in specs
            ],
        }
        for track_id, specs in prepared.after_intervals.items()
    ]


def _apply_native_request(
    prepared: PreparedSceneTrackCommand,
    *,
    expected_snapshot_token: str,
    actor_id: uuid.UUID,
    idempotency_key: str,
):
    return scene_track_command(
        "apply",
        project_id=prepared.context.project_id,
        scene_id=prepared.context.scene_id,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        request_digest=request_digest(prepared.request),
        expected_snapshot_token=expected_snapshot_token,
        command=prepared.request.model_dump(mode="json"),
        preview=preview_payload(prepared),
        snapshot=_write_snapshot(prepared),
        after_intervals=_interval_sets(prepared),
        created_track_id=prepared.created_track_id,
        target_task_id=next(
            (
                task_id
                for task_id, frame in prepared.context.task_to_frame.items()
                if frame == prepared.request.resume_frame
            ),
            None,
        ),
        created_track_attributes=prepared.primary.track.attributes or {},
        created_track_attributes_meta=prepared.primary.track.attributes_meta or {},
    )


async def _scene_track_command_data(
    db: AsyncSession, request, *, autoflush: bool = True
):
    if autoflush and db.autoflush:
        await db.flush()

    async def execute(request_json: str):
        return await db.scalar(_SCENE_TRACK_COMMAND_STEP, {"request": request_json})

    try:
        return await SceneTrackCommandDataClient(execute).command(request)
    except SceneTrackCommandFailure as exc:
        raise _error(exc.status_code, exc.reason, exc.message) from exc
    except SceneTrackCommandUnavailable:
        raise HTTPException(
            status_code=503,
            detail={"reason": "scene_track_command_unavailable"},
        ) from None


async def lock_scene_track_command(
    db: AsyncSession,
    *,
    context: SceneTrackContext,
    request: SceneTrackCommandRequest,
) -> None:
    track_ids = [request.track_id]
    if request.secondary_track_id is not None:
        track_ids.append(request.secondary_track_id)
    result = await _scene_track_command_data(
        db,
        scene_track_command(
            "lock",
            project_id=context.project_id,
            scene_id=context.scene_id,
            track_ids=track_ids,
            resume_frame=request.resume_frame,
            target_task_id=next(
                (
                    task_id
                    for task_id, frame in context.task_to_frame.items()
                    if frame == request.resume_frame
                ),
                None,
            ),
        ),
        autoflush=False,
    )
    if result.locked is not True:
        raise HTTPException(
            status_code=503,
            detail={"reason": "scene_track_command_unavailable"},
        )


async def replay_scene_track_command(
    db: AsyncSession,
    *,
    scene_id: uuid.UUID,
    actor_id: uuid.UUID,
    idempotency_key: str,
    digest: str,
):
    return await _scene_track_command_data(
        db,
        scene_track_command(
            "replay",
            scene_id=scene_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_digest=digest,
        ),
    )


async def inspect_scene_track_operation(
    db: AsyncSession, *, scene_id: uuid.UUID, operation_id: uuid.UUID
):
    return await _scene_track_command_data(
        db,
        scene_track_command("inspect", scene_id=scene_id, operation_id=operation_id),
    )


async def list_scene_track_operations(
    db: AsyncSession,
    *,
    scene_id: uuid.UUID,
    track_id: str,
    limit: int,
    committed_only: bool = False,
    exclude_reverts: bool = False,
):
    result = await _scene_track_command_data(
        db,
        scene_track_command(
            "list",
            scene_id=scene_id,
            track_id=track_id,
            limit=limit,
            committed_only=committed_only,
            exclude_reverts=exclude_reverts,
        ),
    )
    assert result.operations is not None
    return result.operations


def _operation_receipt(
    operation: SceneTrackCommandOperation,
    *,
    before_state: dict | None = None,
    after_state: dict | None = None,
) -> SceneTrackOperationReceipt:
    return SceneTrackOperationReceipt(
        id=operation.id,
        kind=operation.kind,
        status=operation.status,
        created_at=operation.created_at,
        completed_at=operation.completed_at,
        response_json=operation.response_json,
        before_state=before_state,
        after_state=after_state,
        task_ids=tuple(operation.task_ids),
    )


def _sync_scene_track_command_cache(db: AsyncSession, result) -> None:
    before_state = result.before_state or {}
    after_state = result.after_state or {}
    before_tracks = before_state.get("tracks", {})
    tracks = after_state.get("tracks", {})
    before_members = {
        member["id"]: member
        for state in before_tracks.values()
        for member in state.get("members", [])
    }
    members = {
        member["id"]: member
        for state in tracks.values()
        for member in state.get("members", [])
    }
    replaced_tracks = set(result.replaced_track_ids)

    for instance in list(db.sync_session.identity_map.values()):
        if isinstance(instance, SceneTrack):
            state = tracks.get(instance.track_id)
            previous = before_tracks.get(instance.track_id)
            if state is None or previous is None or str(instance.id) != state["id"]:
                continue
            changed_fields = []
            for field in (
                "class_name",
                "presence_mode",
                "attributes",
                "attributes_meta",
                "revision",
                "retired_at",
            ):
                if previous.get(field) == state.get(field):
                    continue
                changed_fields.append(field)
            if changed_fields:
                db.sync_session.expire(instance, [*changed_fields, "updated_at"])
        elif isinstance(instance, Annotation):
            state = members.get(str(instance.id))
            previous = before_members.get(str(instance.id))
            if state is None or previous is None:
                continue
            changed_fields = []
            for field in (
                "track_id",
                "scene_track_id",
                "class_name",
                "is_active",
                "is_hidden",
                "temporal_role",
                "version",
            ):
                if previous.get(field) == state.get(field):
                    continue
                changed_fields.append(field)
            if changed_fields:
                db.sync_session.expire(instance, [*changed_fields, "updated_at"])
        elif isinstance(instance, SceneTrackInterval):
            if instance.scene_track_id in replaced_tracks:
                db.sync_session.expunge(instance)


async def apply_scene_track_command(
    db: AsyncSession,
    *,
    prepared: PreparedSceneTrackCommand,
    expected_snapshot_token: str,
    actor_id: uuid.UUID,
    idempotency_key: str,
) -> tuple[SceneTrackOperationReceipt, dict]:
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

    result = await _scene_track_command_data(
        db,
        _apply_native_request(
            prepared,
            expected_snapshot_token=expected_snapshot_token,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        ),
    )
    _sync_scene_track_command_cache(db, result)
    await db.flush()
    if not result.found:
        await record_annotation_actors_for_tasks(
            db, result.task_ids, actor_id, nowait=True
        )
        await db.flush()
    if result.operation is None or result.response_json is None:
        raise HTTPException(
            status_code=503,
            detail={"reason": "scene_track_command_unavailable"},
        )
    return (
        _operation_receipt(
            result.operation,
            before_state=result.before_state,
            after_state=result.after_state,
        ),
        result.response_json,
    )


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
    scene_id: uuid.UUID,
    operation_id: uuid.UUID,
    actor_id: uuid.UUID,
    idempotency_key: str,
) -> tuple[SceneTrackOperationReceipt, dict]:
    result = await _scene_track_command_data(
        db,
        scene_track_command(
            "revert",
            scene_id=scene_id,
            operation_id=operation_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        ),
    )
    _sync_scene_track_command_cache(db, result)
    await db.flush()
    if not result.found:
        await record_annotation_actors_for_tasks(
            db, result.task_ids, actor_id, nowait=True
        )
        await db.flush()
    if result.operation is None or result.response_json is None:
        raise HTTPException(
            status_code=503,
            detail={"reason": "scene_track_command_unavailable"},
        )
    return (
        _operation_receipt(
            result.operation,
            before_state=result.before_state,
            after_state=result.after_state,
        ),
        result.response_json,
    )
