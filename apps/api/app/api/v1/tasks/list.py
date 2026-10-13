import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, or_, and_, exists
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    get_db,
    get_current_user,
    assert_project_visible,
)
from app.db.models.user import User
from app.db.models.task import Task
from app.db.models.annotation import Annotation
from app.schemas.task import (
    TaskOut,
    TaskListResponse,
)
from app.schemas.scene import NeighborsResponse
from app.services.project_access import (
    ProjectAccess,
    ProjectCapability,
    resolve_project_access,
)
from app.services.scheduler import (
    get_next_task,
    task_visibility_clause,
)
from app.services.user_brief import resolve_briefs
from app.db.models.task_batch import TaskBatch


from app.api.v1.tasks._shared import (
    _load_task_or_404,
    _assert_task_visible,
    _encode_task_cursor,
    _decode_task_cursor,
    _task_with_url,
    _attach_dimensions,
    _attach_dimensions_batch,
    _attach_image_pyramids_batch,
    _SEQ_NULL_SENTINEL,
)

router = APIRouter()


def _build_task_query(
    *,
    project_id: uuid.UUID,
    project,
    user: User,
    access: ProjectAccess,
    status: str | None,
    assignee_id: uuid.UUID | None,
    batch_id: uuid.UUID | None,
    unbatched: bool,
    reject_reason_type: str | None,
    class_name: str | None,
):
    """Build the one authorized task relation used by page and count queries."""
    query = select(Task).where(Task.project_id == project_id)

    # B-16: retain the scheduler's exact non-privileged batch visibility policy.
    if not access.is_manager:
        query = query.outerjoin(TaskBatch, Task.batch_id == TaskBatch.id).where(
            task_visibility_clause(user, project_role=access.project_role)
        )

    if status:
        query = query.where(Task.status == status)
    if assignee_id:
        query = query.where(Task.assignee_id == assignee_id)
    if reject_reason_type:
        query = query.where(Task.reject_reason_type == reject_reason_type)
    if class_name:
        query = query.where(
            exists().where(
                Annotation.task_id == Task.id,
                Annotation.class_name == class_name,
                Annotation.is_active.is_(True),
            )
        )

    # Unbatched keeps precedence over an explicitly supplied batch_id.
    if unbatched:
        query = query.where(Task.batch_id.is_(None))
    elif batch_id:
        query = query.where(Task.batch_id == batch_id)
    return query


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    project_id: uuid.UUID = Query(...),
    status: str | None = None,
    assignee_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    unbatched: bool = False,
    reject_reason_type: str | None = None,
    class_name: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, user)
    access = await resolve_project_access(db, user=user, project=project)
    filtered = _build_task_query(
        project_id=project_id,
        project=project,
        user=user,
        access=access,
        status=status,
        assignee_id=assignee_id,
        batch_id=batch_id,
        unbatched=unbatched,
        reject_reason_type=reject_reason_type,
        class_name=class_name,
    )
    count_q = select(func.count()).select_from(filtered.order_by(None).subquery())

    # v0.6.8 B-15：首屏与游标分支统一排序，并都产出 next_cursor，修前端 useInfiniteQuery
    # 因首屏拿不到 next_cursor 而判定 hasNextPage=false 卡在 100 条的 BUG。
    # 排序主键改为 sequence_order(点云 scene 按帧时序分包时,同 scene 的 task 是同一刻批量
    # 创建的、created_at 全相同,旧的 (created_at, id) 排序退化为按随机 UUID id 乱序)。
    # 非序列任务 sequence_order 为 NULL → coalesce 到哨兵,排序退回 (created_at, id),行为不变。
    q = filtered
    seq_key = func.coalesce(Task.sequence_order, _SEQ_NULL_SENTINEL)
    if cursor:
        last_seq, last_ts, last_id = _decode_task_cursor(cursor)
        q = q.where(
            or_(
                seq_key > last_seq,
                and_(seq_key == last_seq, Task.created_at > last_ts),
                and_(
                    seq_key == last_seq, Task.created_at == last_ts, Task.id > last_id
                ),
            )
        )

    q = q.order_by(seq_key, Task.created_at, Task.id).limit(limit)
    if not cursor and offset:
        q = q.offset(offset)
    tasks = list((await db.execute(q)).scalars().all())
    # v0.11.30 · 仅首页(无 cursor 且 offset=0)做精确全表 COUNT；后续页(cursor 翻页)
    # 返回 None，前端复用首页值，避免无限滚动逐页重复全表 COUNT(大表 O(N) 放大)。
    is_first_page = cursor is None and offset == 0
    total = ((await db.execute(count_q)).scalar() or 0) if is_first_page else None
    dims = await _attach_dimensions_batch(db, tasks)
    pyramids = await _attach_image_pyramids_batch(db, tasks, dims)
    # v0.7.2 · 一次 IN 查询解析所有 assignee_id / reviewer_id → UserBrief
    user_ids = {t.assignee_id for t in tasks if t.assignee_id} | {
        t.reviewer_id for t in tasks if t.reviewer_id
    }
    briefs = await resolve_briefs(db, user_ids) if user_ids else {}
    next_cursor = (
        _encode_task_cursor(
            tasks[-1].sequence_order, tasks[-1].created_at, tasks[-1].id
        )
        if len(tasks) == limit
        else None
    )
    return TaskListResponse(
        items=[
            _task_with_url(
                t,
                *dims.get(t.id, (None, None, None, None, None)),
                image_pyramid=pyramids.get(t.id),
                briefs=briefs,
            )
            for t in tasks
        ],
        total=total,
        limit=limit,
        offset=0 if cursor else offset,
        next_cursor=next_cursor,
    )


@router.get("/next", response_model=TaskOut | None)
async def next_task(
    project_id: uuid.UUID = Query(...),
    batch_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = await assert_project_visible(project_id, db, current_user)
    access = await resolve_project_access(db, user=current_user, project=project)
    if not access.is_manager and not access.has(
        ProjectCapability.ANNOTATION_WRITE.value
    ):
        raise HTTPException(status_code=403, detail="缺少项目权限: annotation.write")
    task = await get_next_task(
        current_user,
        project_id,
        db,
        batch_id=batch_id,
        project_role=access.project_role,
    )
    if not task:
        return None
    await db.commit()
    w, h, thumb, bh, video_metadata = await _attach_dimensions(db, task)
    dimensions = {task.id: (w, h, thumb, bh, video_metadata)}
    pyramids = await _attach_image_pyramids_batch(db, [task], dimensions)
    briefs = await resolve_briefs(db, [task.assignee_id, task.reviewer_id])
    return _task_with_url(
        task,
        w,
        h,
        thumb,
        bh,
        video_metadata,
        image_pyramid=pyramids.get(task.id),
        briefs=briefs,
    )


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    w, h, thumb, bh, video_metadata = await _attach_dimensions(db, task)
    dimensions = {task.id: (w, h, thumb, bh, video_metadata)}
    pyramids = await _attach_image_pyramids_batch(db, [task], dimensions)
    briefs = await resolve_briefs(db, [task.assignee_id, task.reviewer_id])
    return _task_with_url(
        task,
        w,
        h,
        thumb,
        bh,
        video_metadata,
        image_pyramid=pyramids.get(task.id),
        briefs=briefs,
    )


@router.get(
    "/{task_id}/neighbors",
    response_model=NeighborsResponse,
)
async def get_task_neighbors(
    task_id: uuid.UUID,
    k: int = Query(1, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """v0.14.0 · 返回 task 在所属 scene 内的前后 k 个邻居 task。

    历史未 backfill / 无 scene_id 的 task → 200 + 空 prev/next(与首末帧一致)。
    scene_id 非空但 frame_index NULL(异常)→ 409。
    """
    from app.services.scene import (
        SceneFrameIndexInconsistent,
        get_neighbors_for_task,
    )

    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)

    try:
        result = await get_neighbors_for_task(db, task_id=task_id, k=k)
    except SceneFrameIndexInconsistent as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if result is None:
        # 与"首末帧"对调用方一致:返回空 prev/next 不要 404,避免前端做无用区分
        return NeighborsResponse(
            scene_id=None,
            scene_name=None,
            frame_index=None,
            scene_total_frames=0,
            prev=[],
            next=[],
        )
    return result
