import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import task_access
from app.api.v1.tasks._shared import (
    _load_task_or_404,
)
from app.deps import get_current_user, get_db
from app.schemas.scene import (
    SceneTimelineFrameSummary,
    SceneTimelineResponse,
    SceneTimelineTrackOccurrence,
)
from app.services.labeling_scene import (
    get_scene_timeline_annotation_summaries,
    get_scene_timeline_window,
)
from app.services.labeling_scene_read import read_scene_data
from app.services.project_access import get_workspace_project
from app.services.identity_contracts import IdentityActorFacts


router = APIRouter()
_MAX_WINDOW_FRAMES = 200


@router.get(
    "/{task_id}/scene-timeline",
    response_model=SceneTimelineResponse,
)
async def get_task_scene_timeline(
    task_id: uuid.UUID,
    start_frame: int = Query(..., ge=0),
    end_frame: int = Query(..., ge=0),
    track_id: str | None = Query(default=None, min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """返回当前 task 所属 3D Scene 的只读窗口摘要。"""
    if end_frame < start_frame:
        raise HTTPException(
            status_code=422,
            detail="end_frame must be greater than or equal to start_frame",
        )
    if end_frame - start_frame + 1 > _MAX_WINDOW_FRAMES:
        raise HTTPException(
            status_code=422,
            detail=f"scene timeline window cannot exceed {_MAX_WINDOW_FRAMES} frames",
        )

    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    window = await get_scene_timeline_window(
        db, task=task, start_frame=start_frame, end_frame=end_frame
    )
    if window is None:
        return SceneTimelineResponse()

    project = await get_workspace_project(db, actor_id=None, project_id=task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Task not found")
    all_task_ids = {entry.task_id for entry in window.frame_tasks.values()}
    visible_task_ids = await task_access.visible_task_ids(
        db, project, current_user.id, list(all_task_ids)
    )
    annotation_summaries = await get_scene_timeline_annotation_summaries(
        db, task_ids=visible_task_ids, track_id=track_id
    )
    selected_intervals = []
    if track_id:
        track_data = await read_scene_data(
            db,
            "load_timeline_track",
            project_id=task.project_id,
            scene_id=window.scene_id,
            track_id=track_id,
        )
        if track_data.track is not None:
            selected_intervals = track_data.intervals

    frames: list[SceneTimelineFrameSummary] = []
    for frame_index in range(start_frame, end_frame + 1):
        selected_track_present = (
            any(
                interval.start_frame <= frame_index
                and (interval.end_frame is None or frame_index <= interval.end_frame)
                for interval in selected_intervals
            )
            if track_id
            else None
        )
        frame_task = window.frame_tasks.get(frame_index)
        if frame_task is None:
            frames.append(
                SceneTimelineFrameSummary(
                    frame_index=frame_index,
                    state="missing",
                    selected_track_present=selected_track_present,
                )
            )
            continue
        if frame_task.task_id not in visible_task_ids:
            frames.append(
                SceneTimelineFrameSummary(
                    frame_index=frame_index,
                    state="unavailable",
                    selected_track_present=None,
                )
            )
            continue

        summary = annotation_summaries.get(frame_task.task_id)
        occurrence = None
        if summary and summary.selected_annotation_id is not None:
            occurrence = SceneTimelineTrackOccurrence(
                annotation_id=summary.selected_annotation_id,
                source=summary.selected_source or "manual",
                class_name=summary.selected_class_name or "",
                temporal_role=summary.selected_temporal_role or "sample",
            )
        frames.append(
            SceneTimelineFrameSummary(
                frame_index=frame_index,
                state="available",
                task_id=frame_task.task_id,
                task_status=frame_task.status,
                annotation_count=summary.annotation_count if summary else 0,
                selected_track=occurrence,
                selected_track_present=selected_track_present,
            )
        )

    return SceneTimelineResponse(
        scene_id=window.scene_id,
        scene_name=window.scene_name,
        current_frame_index=window.current_frame_index,
        scene_start_frame=window.scene_start_frame,
        scene_end_frame=window.scene_end_frame,
        populated_frame_count=window.populated_frame_count,
        window_start_frame=start_frame,
        window_end_frame=end_frame,
        frames=frames,
    )
