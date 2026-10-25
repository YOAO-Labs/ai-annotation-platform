"""Scene Track identity, presence intervals, member binding and diagnostics."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes, make_transient_to_detached
from sqlalchemy.orm.util import identity_key

from aap_labeling.scene_track_domain_contract import (
    SCENE_TRACK_DOMAIN_FUNCTION,
    SceneTrackDomainFailure,
    SceneTrackDomainUnavailable,
    SceneTrackRecord,
    scene_track_command,
)
from aap_labeling.scene_track_domain_data import SceneTrackDomainDataClient

from app.db.models.annotation import Annotation
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task import Task
from app.services.labeling_scene import (
    resolve_task_scene_frames,
)
from app.services.labeling_scene_read import read_scene_data


TEMPORAL_ROLES = frozenset({"keyframe", "derived", "sample"})
_SCENE_TRACK_STEP = text(
    f"SELECT {SCENE_TRACK_DOMAIN_FUNCTION.partition('(')[0]}(CAST(:request AS jsonb))"
)


class SceneTrackIntegrityError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SceneTrackBinding:
    track: SceneTrack
    scene_id: uuid.UUID
    frame_index: int


@dataclass(frozen=True)
class SceneTrackDiagnostic:
    code: str
    track_id: str | None
    annotation_id: uuid.UUID | None = None
    frame_index: int | None = None


@dataclass(frozen=True)
class SceneTrackDiagnosticReport:
    scene_id: uuid.UUID
    track_count: int
    linked_member_count: int
    issue_counts: dict[str, int]
    issues: tuple[SceneTrackDiagnostic, ...]
    truncated: bool


def temporal_role_for_write(*, source: str, user_confirmed: bool) -> str:
    if user_confirmed:
        return "keyframe"
    if source in {"interpolated", "propagated", "tracked", "cross_frame"}:
        return "derived"
    return "sample"


def _contains(interval: SceneTrackInterval, frame_index: int) -> bool:
    return interval.start_frame <= frame_index and (
        interval.end_frame is None or frame_index <= interval.end_frame
    )


async def _scene_track_command(db: AsyncSession, action: str, **values):
    command = scene_track_command(action, **values)

    async def execute(request_json: str):
        return await db.scalar(_SCENE_TRACK_STEP, {"request": request_json})

    if db.autoflush:
        await db.flush()
    try:
        return await SceneTrackDomainDataClient(execute).command(command)
    except SceneTrackDomainFailure as exc:
        raise SceneTrackIntegrityError(exc.code, exc.message) from exc
    except SceneTrackDomainUnavailable:
        raise HTTPException(status_code=503, detail="场景轨迹服务暂不可用") from None


async def _attach_scene_track(
    db: AsyncSession,
    record: SceneTrackRecord,
    *,
    changed_fields: tuple[str, ...] = (),
) -> SceneTrack:
    key = identity_key(SceneTrack, ident=record.id)
    instance = db.sync_session.identity_map.get(key)
    values = record.model_dump(mode="python")
    if instance is not None:
        expired = attributes.instance_state(instance).expired_attributes
        for name, value in values.items():
            if name in expired or name in changed_fields:
                attributes.set_committed_value(instance, name, value)
        return instance

    detached = SceneTrack(**values)
    make_transient_to_detached(detached)
    return await db.merge(detached, load=False)


def _sync_camera_member_cache(db: AsyncSession, members) -> None:
    for member in members:
        instance = db.sync_session.identity_map.get(
            identity_key(Annotation, ident=member.id)
        )
        if instance is not None:
            attributes.set_committed_value(instance, "class_name", member.class_name)
            attributes.set_committed_value(instance, "version", member.version)
            attributes.set_committed_value(instance, "updated_at", member.updated_at)


async def ensure_scene_track(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    scene_id: uuid.UUID,
    track_id: str,
    class_name: str,
    frames: set[int],
    actor_id: uuid.UUID | None,
    interval_source: str,
    member_change: bool = True,
) -> SceneTrack:
    result = await _scene_track_command(
        db,
        "ensure",
        project_id=project_id,
        scene_id=scene_id,
        track_id=track_id,
        class_name=class_name,
        frames=sorted(frames),
        actor_id=actor_id,
        interval_source=interval_source,
        member_change=member_change,
    )
    if result.track is None:
        raise SceneTrackDomainUnavailable()
    track = await _attach_scene_track(
        db,
        result.track,
        changed_fields=("revision", "updated_at") if result.changed else (),
    )
    await db.flush()
    return track


async def bind_annotation_to_scene_track(
    db: AsyncSession,
    *,
    annotation: Annotation,
    task: Task,
    temporal_role: str,
    interval_source: str,
    actor_id: uuid.UUID | None,
) -> SceneTrackBinding | None:
    if (annotation.geometry or {}).get("type") != "box_3d":
        return None
    if annotation.track_id is None:
        return None
    if temporal_role not in TEMPORAL_ROLES:
        raise SceneTrackIntegrityError(
            "temporal_role_invalid", "unsupported Annotation temporal role"
        )
    frame = (await resolve_task_scene_frames(db, [task.id]))[task.id]
    if frame.scene_id is None or frame.frame_index is None:
        return None
    track = await ensure_scene_track(
        db,
        project_id=task.project_id,
        scene_id=frame.scene_id,
        track_id=annotation.track_id,
        class_name=annotation.class_name,
        frames={frame.frame_index},
        actor_id=actor_id,
        interval_source=interval_source,
    )
    annotation.scene_track_id = track.id
    annotation.temporal_role = temporal_role
    return SceneTrackBinding(
        track=track,
        scene_id=frame.scene_id,
        frame_index=frame.frame_index,
    )


async def touch_annotation_scene_track(
    db: AsyncSession,
    annotation: Annotation,
    *,
    make_keyframe: bool,
) -> None:
    if annotation.scene_track_id is None:
        return
    result = await _scene_track_command(
        db,
        "touch",
        annotation_id=annotation.id,
        scene_track_id=annotation.scene_track_id,
        track_id=annotation.track_id,
        class_name=annotation.class_name,
    )
    if result.track is not None:
        await _attach_scene_track(
            db, result.track, changed_fields=("revision", "updated_at")
        )
    if make_keyframe:
        annotation.temporal_role = "keyframe"


async def reclassify_single_member_scene_track(
    db: AsyncSession,
    *,
    annotation: Annotation,
    class_name: str,
) -> bool:
    """Change Track class only when the selected member is its sole active member."""
    if annotation.scene_track_id is None or annotation.class_name == class_name:
        return False
    result = await _scene_track_command(
        db,
        "reclassify",
        annotation_id=annotation.id,
        scene_track_id=annotation.scene_track_id,
        track_id=annotation.track_id,
        expected_class_name=annotation.class_name,
        class_name=class_name,
    )
    if result.track is not None:
        await _attach_scene_track(
            db,
            result.track,
            changed_fields=("class_name", "revision", "updated_at")
            if result.changed
            else (),
        )
    _sync_camera_member_cache(db, result.camera_members)
    return result.changed


async def diagnose_scene_tracks(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    scene_id: uuid.UUID,
    limit: int = 200,
) -> SceneTrackDiagnosticReport:
    result = await read_scene_data(
        db,
        "load_scene_diagnostics",
        project_id=project_id,
        scene_id=scene_id,
    )
    tracks = result.tracks
    intervals = result.intervals
    members = result.members
    tracks_by_id = {track.id: track for track in tracks}
    intervals_by_track: dict[uuid.UUID, list[SceneTrackInterval]] = defaultdict(list)
    for interval in intervals:
        intervals_by_track[interval.scene_track_id].append(interval)
    members_by_track: Counter[uuid.UUID] = Counter(
        member.scene_track_id for member in members if member.scene_track_id is not None
    )

    issues: list[SceneTrackDiagnostic] = []

    def add(
        code: str,
        *,
        track_id: str | None,
        annotation_id: uuid.UUID | None = None,
        frame_index: int | None = None,
    ) -> None:
        issues.append(
            SceneTrackDiagnostic(
                code=code,
                track_id=track_id,
                annotation_id=annotation_id,
                frame_index=frame_index,
            )
        )

    for track in tracks:
        rows = intervals_by_track[track.id]
        if not rows:
            add("track_without_interval", track_id=track.track_id)
        if members_by_track[track.id] == 0:
            add("track_without_member", track_id=track.track_id)
        for left, right in zip(rows, rows[1:], strict=False):
            if left.end_frame is None or left.end_frame >= right.start_frame:
                add("interval_overlap", track_id=track.track_id)
            elif left.end_frame + 1 == right.start_frame:
                add("interval_adjacent", track_id=track.track_id)

    frame_identity_counts: Counter[tuple[uuid.UUID, int]] = Counter()
    for member in members:
        frame_index = member.frame_index
        if frame_index is None:
            raise RuntimeError("Scene read omitted a diagnostic member frame")
        if member.scene_track_id is None:
            add(
                "member_unlinked",
                track_id=member.track_id,
                annotation_id=member.id,
                frame_index=frame_index,
            )
            continue
        track = tracks_by_id.get(member.scene_track_id)
        if track is None:
            add(
                "member_track_outside_scene",
                track_id=member.track_id,
                annotation_id=member.id,
                frame_index=frame_index,
            )
            continue
        if track.track_id != member.track_id or track.class_name != member.class_name:
            add(
                "member_identity_mismatch",
                track_id=member.track_id,
                annotation_id=member.id,
                frame_index=frame_index,
            )
        if not any(
            _contains(interval, frame_index)
            for interval in intervals_by_track[track.id]
        ):
            add(
                "member_outside_interval",
                track_id=member.track_id,
                annotation_id=member.id,
                frame_index=frame_index,
            )
        frame_identity_counts[(track.id, frame_index)] += 1

    for (scene_track_id, frame_index), count in frame_identity_counts.items():
        if count > 1:
            add(
                "duplicate_frame_member",
                track_id=tracks_by_id[scene_track_id].track_id,
                frame_index=frame_index,
            )

    issue_counts = dict(sorted(Counter(issue.code for issue in issues).items()))
    return SceneTrackDiagnosticReport(
        scene_id=scene_id,
        track_count=len(tracks),
        linked_member_count=sum(members_by_track.values()),
        issue_counts=issue_counts,
        issues=tuple(issues[:limit]),
        truncated=len(issues) > limit,
    )
