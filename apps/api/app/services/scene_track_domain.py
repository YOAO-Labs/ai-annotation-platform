"""Scene Track identity, presence intervals, member binding and diagnostics."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.scene_track import SceneTrack, SceneTrackInterval
from app.db.models.task import Task
from app.services.labeling_scene import (
    get_scene_frame_task_map,
    resolve_task_scene_frames,
)


TEMPORAL_ROLES = frozenset({"keyframe", "derived", "sample"})
INTERVAL_SOURCES = frozenset({"legacy_envelope", "manual", "imported", "derived"})


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


async def _load_or_create_track(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    scene_id: uuid.UUID,
    track_id: str,
    class_name: str,
    actor_id: uuid.UUID | None,
) -> tuple[SceneTrack, bool]:
    created_id = uuid.uuid4()
    result = await db.execute(
        insert(SceneTrack)
        .values(
            id=created_id,
            project_id=project_id,
            scene_id=scene_id,
            track_id=track_id,
            class_name=class_name,
            attributes={},
            attributes_meta={},
            revision=1,
            created_by=actor_id,
        )
        .on_conflict_do_nothing(index_elements=["project_id", "scene_id", "track_id"])
        .returning(SceneTrack.id)
    )
    inserted_id = result.scalar_one_or_none()
    track = (
        await db.execute(
            select(SceneTrack)
            .where(SceneTrack.project_id == project_id)
            .where(SceneTrack.scene_id == scene_id)
            .where(SceneTrack.track_id == track_id)
            .with_for_update()
        )
    ).scalar_one()
    if track.class_name != class_name:
        raise SceneTrackIntegrityError(
            "track_class_conflict",
            "Scene Track class does not match the annotation class",
        )
    if track.retired_at is not None:
        raise SceneTrackIntegrityError(
            "track_retired",
            "retired Scene Track cannot accept active members",
        )
    return track, inserted_id == created_id


async def _load_intervals_for_update(
    db: AsyncSession, scene_track_id: uuid.UUID
) -> list[SceneTrackInterval]:
    return list(
        (
            await db.execute(
                select(SceneTrackInterval)
                .where(SceneTrackInterval.scene_track_id == scene_track_id)
                .order_by(SceneTrackInterval.start_frame, SceneTrackInterval.id)
                .with_for_update()
            )
        ).scalars()
    )


def _contains(interval: SceneTrackInterval, frame_index: int) -> bool:
    return interval.start_frame <= frame_index and (
        interval.end_frame is None or frame_index <= interval.end_frame
    )


async def _expand_inferred_envelope(
    db: AsyncSession,
    *,
    track: SceneTrack,
    intervals: list[SceneTrackInterval],
    frames: set[int],
    source: str,
    actor_id: uuid.UUID | None,
) -> bool:
    """Keep inferred presence as one conservative member envelope."""
    starts = [row.start_frame for row in intervals]
    starts.extend(frames)
    finite_ends = [row.end_frame for row in intervals if row.end_frame is not None]
    finite_ends.extend(frames)
    next_start = min(starts)
    next_end = (
        None if any(row.end_frame is None for row in intervals) else max(finite_ends)
    )

    if not intervals:
        db.add(
            SceneTrackInterval(
                id=uuid.uuid4(),
                scene_track_id=track.id,
                start_frame=next_start,
                end_frame=next_end,
                source=source,
                version=1,
                created_by=actor_id,
            )
        )
        return True

    keeper = intervals[0]
    changed = (
        len(intervals) != 1
        or keeper.start_frame != next_start
        or keeper.end_frame != next_end
    )
    if not changed:
        return False
    for row in intervals[1:]:
        await db.delete(row)
    await db.flush()
    keeper.start_frame = next_start
    keeper.end_frame = next_end
    keeper.version = int(keeper.version or 1) + 1
    return True


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
    if not track_id or len(track_id) > 64:
        raise SceneTrackIntegrityError(
            "track_id_invalid", "Scene Track requires a non-empty track_id"
        )
    if not frames or min(frames) < 0:
        raise SceneTrackIntegrityError(
            "track_frames_invalid", "Scene Track requires non-negative member frames"
        )
    if interval_source not in INTERVAL_SOURCES:
        raise SceneTrackIntegrityError(
            "interval_source_invalid", "unsupported Scene Track interval source"
        )

    track, created = await _load_or_create_track(
        db,
        project_id=project_id,
        scene_id=scene_id,
        track_id=track_id,
        class_name=class_name,
        actor_id=actor_id,
    )
    intervals = await _load_intervals_for_update(db, track.id)
    if track.presence_mode == "explicit":
        outside = [
            frame_index
            for frame_index in sorted(frames)
            if not any(_contains(interval, frame_index) for interval in intervals)
        ]
        if outside:
            raise SceneTrackIntegrityError(
                "track_frame_absent",
                "explicit Scene Track presence must be resumed before writing an absent frame",
            )
        interval_changed = False
    else:
        interval_changed = await _expand_inferred_envelope(
            db,
            track=track,
            intervals=intervals,
            frames=frames,
            source=interval_source,
            actor_id=actor_id,
        )
    if not created and (member_change or interval_changed):
        track.revision = int(track.revision or 1) + 1
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
    track = (
        await db.execute(
            select(SceneTrack)
            .where(SceneTrack.id == annotation.scene_track_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if track is None:
        raise SceneTrackIntegrityError(
            "track_missing", "Annotation references a missing Scene Track"
        )
    if (
        track.track_id != annotation.track_id
        or track.class_name != annotation.class_name
    ):
        raise SceneTrackIntegrityError(
            "member_identity_mismatch",
            "Annotation identity does not match its Scene Track",
        )
    track.revision = int(track.revision or 1) + 1
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
    track = (
        await db.execute(
            select(SceneTrack)
            .where(SceneTrack.id == annotation.scene_track_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if track is None:
        raise SceneTrackIntegrityError(
            "track_missing", "Annotation references a missing Scene Track"
        )
    member_count = await db.scalar(
        select(func.count())
        .select_from(Annotation)
        .where(Annotation.scene_track_id == track.id)
        .where(Annotation.is_active.is_(True))
        .where(Annotation.was_cancelled.is_(False))
        .where(Annotation.sensor_role.is_(None))
    )
    if int(member_count or 0) != 1:
        raise SceneTrackIntegrityError(
            "track_class_change_requires_scope",
            "change the class through a whole-track operation",
        )
    track.class_name = class_name
    camera_members = list(
        (
            await db.execute(
                select(Annotation)
                .where(Annotation.scene_track_id == track.id)
                .where(Annotation.sensor_role.is_not(None))
                .where(Annotation.was_cancelled.is_(False))
                .with_for_update()
            )
        ).scalars()
    )
    for member in camera_members:
        member.class_name = class_name
        member.version = int(member.version or 1) + 1
    track.revision = int(track.revision or 1) + 1
    return True


async def diagnose_scene_tracks(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    scene_id: uuid.UUID,
    limit: int = 200,
) -> SceneTrackDiagnosticReport:
    tracks = list(
        (
            await db.execute(
                select(SceneTrack)
                .where(SceneTrack.project_id == project_id)
                .where(SceneTrack.scene_id == scene_id)
                .where(SceneTrack.retired_at.is_(None))
                .order_by(SceneTrack.track_id)
            )
        ).scalars()
    )
    intervals = list(
        (
            await db.execute(
                select(SceneTrackInterval)
                .join(SceneTrack, SceneTrack.id == SceneTrackInterval.scene_track_id)
                .where(SceneTrack.project_id == project_id)
                .where(SceneTrack.scene_id == scene_id)
                .order_by(
                    SceneTrackInterval.scene_track_id,
                    SceneTrackInterval.start_frame,
                )
            )
        ).scalars()
    )
    frame_tasks = await get_scene_frame_task_map(db, scene_id)
    task_frames = {task_id: frame for frame, task_id in frame_tasks.items()}
    members = list(
        (
            await db.execute(
                select(Annotation)
                .where(Annotation.project_id == project_id)
                .where(Annotation.task_id.in_(task_frames))
                .where(Annotation.annotation_type == "box_3d")
                .where(Annotation.track_id.is_not(None))
                .where(Annotation.is_active.is_(True))
                .where(Annotation.was_cancelled.is_(False))
                .order_by(Annotation.track_id, Annotation.task_id, Annotation.id)
            )
        ).scalars()
    )

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
        frame_index = task_frames[member.task_id]
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
