import base64
import uuid
from datetime import datetime
from typing import NoReturn
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    get_db,
    get_current_user,
    get_gpu_dispatch_context_factory,
    get_gpu_shadow_session_factory,
    require_scopes,
)
from app.db.models.user import User
from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import AnnotationOperation
from app.db.models.task import Task
from app.schemas.annotation import (
    AnnotationCreate,
    AnnotationListPage,
    AnnotationOut,
    AnnotationUpdate,
    InterpolateRangeRequest,
    InterpolateRangeResponse,
    NeighborAnnotationsResponse,
    NeighborFrameAnnotations,
    PropagateBatchItem,
    PropagateBatchRequest,
    PropagateBatchResponse,
    PropagateRequest,
    PropagateResponse,
    SecondaryInferenceRequest,
    SecondaryInferenceResponse,
    VideoTrackCompositionRequest,
    VideoTrackCompositionResponse,
    VideoTrackConvertToBboxesRequest,
    VideoTrackConvertToBboxesResponse,
)
from app.schemas.annotation_conversion import (
    AnnotationConversionDryRunRequest,
    AnnotationConversionDryRunResponse,
    AnnotationConversionExecuteRequest,
    AnnotationConversionExecuteResponse,
)
from app.services.annotation import (
    ANNOTATION_CREATE_OPERATION_KIND,
    AnnotationService,
    annotation_create_request_digest,
    validate_geometry_type_transition,
)
from app.services.audit import AuditAction, AuditService
from app.services.annotation_conversion import (
    AnnotationConversionError,
    AnnotationConversionService,
)
from app.services.gpu_arbitration.contracts import (
    GPUDispatchContextFactory,
    GPUShadowSessionFactory,
)
from app.services.ml_backend import MLBackendService
from app.services.pipeline_validation import check_capability_violations
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    prepare_mask_geometry_for_annotation_write,
)
from app.services.secondary_inference import run_secondary_inference
from app.services.task_lock import TaskLockService
from app.db.models.project import Project
from app.services.video_collaboration import (
    assert_video_annotation_write_scope,
    collaboration_enabled,
    heartbeat_task_lock_for_legacy_video,
)


from app.api.v1.tasks._shared import (
    _assert_task_editable,
    _load_task_or_404,
    _assert_task_visible,
    _visible_task_ids,
    _video_frame_count,
    _resolve_task_access,
    require_task_annotation_write,
    require_task_annotation_write_strict,
)
from app.services.project_access import ProjectAccess
from app.services.annotation_evidence import record_annotation_actor

router = APIRouter()


def _raise_conversion_error(exc: AnnotationConversionError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post(
    "/{task_id}/annotation-conversions:dry-run",
    response_model=AnnotationConversionDryRunResponse,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def dry_run_annotation_conversion(
    task_id: uuid.UUID,
    data: AnnotationConversionDryRunRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    try:
        result = await AnnotationConversionService(db).dry_run(
            task=task,
            actor=current_user,
            payload=data,
            access=access,
        )
    except AnnotationConversionError as exc:
        _raise_conversion_error(exc)
    except RasterMaskContractError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    await db.commit()
    return result


@router.post(
    "/{task_id}/annotation-conversions:execute",
    response_model=AnnotationConversionExecuteResponse,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def execute_annotation_conversion(
    task_id: uuid.UUID,
    data: AnnotationConversionExecuteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    try:
        result = await AnnotationConversionService(db).execute(
            task_id=task_id,
            actor=current_user,
            payload=data,
            request=request,
            access=access,
        )
    except AnnotationConversionError as exc:
        _raise_conversion_error(exc)
    except RasterMaskContractError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    await db.commit()
    return result


@router.get(
    "/{task_id}/neighbor-annotations",
    response_model=NeighborAnnotationsResponse,
    dependencies=[Depends(require_scopes("annotations:read"))],
)
async def get_neighbor_annotations(
    task_id: uuid.UUID,
    k: int = Query(1, ge=1, le=20),
    track_id: str | None = Query(
        None,
        description="给定则服务端只回该 track(scope=selected);省略回全部(scope=all)",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """v0.15.17 · 中心 task 前后 k 帧的邻帧标注,一次性返回。

    替代前端「对 2k 个邻帧 task 各发一条 getAnnotations + client 端按 track_id 过滤」。
    v0.21.2 · ADR-0045 · 跨帧链按 track_id(原 group_id)过滤。
    非 scene / 历史未 backfill 的 task → 200 + frames=[](与 neighbors 端点一致,不报错)。

    v0.15.26 · 可见性:与被替代的旧链路一致,邻帧逐 task 复核 batch 可见性 / 分派状态
    (`_visible_task_ids`)。对调用者不可见的邻帧(如分派给别人 / 状态非可见的 batch)
    仍返回 frame 占位但 `annotations=[]`,不外泄他人 batch 的框几何。
    """
    from app.services.scene import (
        SceneFrameIndexInconsistent,
        get_neighbors_for_task,
    )

    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)

    try:
        neighbors = await get_neighbors_for_task(db, task_id=task_id, k=k)
    except SceneFrameIndexInconsistent as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if neighbors is None or neighbors.scene_id is None:
        return NeighborAnnotationsResponse(scene_id=None, frame_index=None, frames=[])

    frame_by_task = {
        n.task_id: n.frame_index for n in (*neighbors.prev, *neighbors.next)
    }
    if not frame_by_task:
        return NeighborAnnotationsResponse(
            scene_id=neighbors.scene_id,
            frame_index=neighbors.frame_index,
            frames=[],
        )

    # v0.15.26 · 逐邻帧复核可见性,不可见的邻帧 task 不取其 annotation(下面 by_task 占位为空)。
    from app.db.models.project import Project

    project = await db.get(Project, task.project_id)
    visible_ids = await _visible_task_ids(
        db, project, current_user, list(frame_by_task.keys())
    )

    svc = AnnotationService(db)
    anns = (
        await svc.list_by_tasks(list(visible_ids), track_id=track_id)
        if visible_ids
        else []
    )

    by_task: dict[uuid.UUID, list[AnnotationOut]] = {tid: [] for tid in frame_by_task}
    for a in anns:
        by_task[a.task_id].append(AnnotationOut.model_validate(a))

    # 按距中心帧远近排序(与 neighbors prev/next 顺序一致),便于前端就近渲染
    ordered = [*neighbors.prev, *neighbors.next]
    frames = [
        NeighborFrameAnnotations(
            task_id=n.task_id,
            frame_index=n.frame_index,
            annotations=by_task.get(n.task_id, []),
        )
        for n in ordered
    ]
    return NeighborAnnotationsResponse(
        scene_id=neighbors.scene_id,
        frame_index=neighbors.frame_index,
        frames=frames,
    )


@router.get(
    "/{task_id}/annotations",
    response_model=list[AnnotationOut],
    dependencies=[Depends(require_scopes("annotations:read"))],
)
async def get_annotations(
    task_id: uuid.UUID,
    video_segment_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    project = await db.get(Project, task.project_id)
    if collaboration_enabled(project) and video_segment_id is None:
        raise HTTPException(
            status_code=422, detail={"reason": "video_segment_required"}
        )
    svc = AnnotationService(db)
    return await svc.list_by_task(task_id, video_segment_id=video_segment_id)


@router.get(
    "/{task_id}/annotations/page",
    response_model=AnnotationListPage,
    dependencies=[Depends(require_scopes("annotations:read"))],
)
async def get_annotations_paged(
    task_id: uuid.UUID,
    limit: int = 200,
    cursor: str | None = None,
    video_segment_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """v0.7.6 · keyset 分页变体。单 task 1000+ 框场景下避免一次性大列表阻塞。

    cursor 编码：base64(`{ts_isoformat}|{annotation_id}`)，与 audit_logs 端点一致。
    """
    from uuid import UUID as _UUID

    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    project = await db.get(Project, task.project_id)
    if collaboration_enabled(project) and video_segment_id is None:
        raise HTTPException(
            status_code=422, detail={"reason": "video_segment_required"}
        )
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be in [1, 1000]")

    decoded: tuple[datetime, uuid.UUID] | None = None
    if cursor:
        try:
            payload = base64.urlsafe_b64decode(cursor.encode()).decode()
            ts_str, id_str = payload.rsplit("|", 1)
            decoded = (datetime.fromisoformat(ts_str), _UUID(id_str))
        except Exception:
            raise HTTPException(status_code=400, detail="invalid cursor")

    svc = AnnotationService(db)
    items, next_cursor_tuple = await svc.list_by_task_keyset(
        task_id,
        limit=limit,
        cursor=decoded,
        video_segment_id=video_segment_id,
    )
    next_cursor: str | None = None
    if next_cursor_tuple is not None:
        ts, aid = next_cursor_tuple
        next_cursor = base64.urlsafe_b64encode(
            f"{ts.isoformat()}|{aid}".encode()
        ).decode()
    return AnnotationListPage(
        items=[AnnotationOut.model_validate(a) for a in items],
        next_cursor=next_cursor,
    )


@router.post(
    "/{task_id}/annotations",
    response_model=AnnotationOut,
    status_code=201,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def create_annotation(
    task_id: uuid.UUID,
    data: AnnotationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Optional durable creation identity (1–128 characters). Reuse only with the same request.",
    ),
):
    # Every create, including an idempotent replay, is serialized by the task
    # row.  The lock must be acquired before authorization and the ledger lookup
    # so a handoff/deactivation cannot race a retry into a stale authorization
    # decision, and concurrent tabs cannot both miss the same receipt.
    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    await assert_video_annotation_write_scope(
        db,
        task=task,
        user=current_user,
        segment_id=data.video_segment_id,
        geometry=data.geometry.model_dump(),
    )

    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 128:
            raise HTTPException(
                status_code=400,
                detail="Idempotency-Key must contain 1 to 128 characters",
            )
        request_digest = annotation_create_request_digest(data.model_dump(mode="json"))
        existing = await db.scalar(
            select(AnnotationOperation).where(
                AnnotationOperation.task_id == task_id,
                AnnotationOperation.actor_id == current_user.id,
                AnnotationOperation.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if (
                existing.kind != ANNOTATION_CREATE_OPERATION_KIND
                or existing.request_digest != request_digest
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "reason": "idempotency_conflict",
                        "message": "idempotency key was already used with another request",
                        "operation_id": str(existing.id),
                    },
                )
            return AnnotationOut.model_validate(existing.response_json)

    svc = AnnotationService(db)
    try:
        annotation = await svc.create(
            task_id=task_id,
            user_id=current_user.id,
            annotation_type=data.annotation_type,
            tool_unit_id=data.tool_unit_id,
            class_name=data.class_name,
            geometry=data.geometry.model_dump(),
            confidence=data.confidence,
            parent_prediction_id=data.parent_prediction_id,
            parent_annotation_id=data.parent_annotation_id,
            lead_time=data.lead_time,
            attributes=data.attributes,
            video_segment_id=data.video_segment_id,
        )
    except RasterMaskContractError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    await heartbeat_task_lock_for_legacy_video(db, task, current_user.id)

    if idempotency_key is not None:
        # ``AnnotationService.create`` flushes the annotation and task stats;
        # refresh server defaults before persisting the historical response.
        await db.refresh(annotation)
        annotation_response = AnnotationOut.model_validate(annotation)
        db.add(
            AnnotationOperation(
                task_id=task_id,
                actor_id=current_user.id,
                kind=ANNOTATION_CREATE_OPERATION_KIND,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                scope_fingerprint=request_digest,
                source_versions={},
                result_versions={},
                report={},
                response_json=annotation_response.model_dump(mode="json"),
            )
        )

    # v0.7.2 · annotation 编辑历史可追溯
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_CREATE,
        target_type="annotation",
        target_id=str(annotation.id),
        request=request,
        status_code=201,
        detail={
            "task_id": str(task_id),
            "class_name": annotation.class_name,
            "annotation_type": annotation.annotation_type,
            "source": annotation.source,
        },
    )
    await db.commit()
    await db.refresh(annotation)
    return annotation


@router.post(
    "/{task_id}/annotations/{annotation_id}/secondary-inference",
    response_model=SecondaryInferenceResponse,
    status_code=201,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def secondary_inference(
    task_id: uuid.UUID,
    annotation_id: uuid.UUID,
    data: SecondaryInferenceRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    shadow_session_factory: GPUShadowSessionFactory = Depends(
        get_gpu_shadow_session_factory
    ),
    dispatch_context_factory: GPUDispatchContextFactory = Depends(
        get_gpu_dispatch_context_factory
    ),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write_strict),
):
    """选中框单框二次推理: 在选中框 ROI 上同步跑一个能力, 产物落库。

    属性型写回原框 (origin=ai)、几何型建子框 (parent=选中框)。复用批量 pipeline 下游
    阶段的 crop 投递 + 产物归位, 不走 worker。

    仅超级管理员、项目管理员和标注员可调用, 仍需通过任务可见性及可编辑状态校验。
    审核员调用返回 403; 人工审核修改仍使用普通标注接口。
    """
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)

    annotation = await db.get(Annotation, annotation_id)
    if annotation is None or annotation.task_id != task_id or not annotation.is_active:
        raise HTTPException(status_code=404, detail="annotation not found")
    await assert_video_annotation_write_scope(
        db,
        task=task,
        user=current_user,
        segment_id=annotation.video_segment_id,
        geometry=annotation.geometry,
    )

    # v0.20.9 一层父子约束: 选中框如果已经是子框 (parent 非空), geometry 型二次推理会
    # 建"孙子"框 (子框的子框), 违反一层深度。前置到端点, 不再等 ML predict 跑完 10-30s
    # 才在 AnnotationService.create 里 400, 省一次 backend 调用。属性型无此问题, 放过。
    if data.write_target == "geometry" and annotation.parent_annotation_id is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "选中框已是子框, geometry 型二次推理会破坏一层父子约束 "
                "(不能建孙子框); 请选顶层父框后再跑, 或改 write_target=attributes"
            ),
        )

    ml_svc = MLBackendService(db)
    backend = await ml_svc.get(data.ml_backend_id)
    if not backend or not await ml_svc.is_enabled(task.project_id, data.ml_backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")

    # 与批量 pipeline 保持同一能力判据 SSOT (services/pipeline_validation): batchable=false
    # (交互 backend) 与「写属性但模型不产 class」都在 predict 前 422 硬挡, 避免选错模型
    # 返 201 空产物 (与"跑完无检出"混淆)。判据本身「显式自报才拦, 缺省放过」, 对老 backend 零退化。
    from app.api.v1.projects import _stage_model

    _writes_attributes = data.write_target == "attributes"
    violations = check_capability_violations(
        _stage_model(backend, data.model_id),
        where="选中框二次推理",
        model_id=data.model_id,
        writes_attributes=_writes_attributes,
    )
    if violations:
        raise HTTPException(status_code=422, detail=violations[0].detail)

    # 释放当前只读事务再进入 10-30s 的远程 predict, 避免 async 连接池在高并发下饥饿
    # (expire_on_commit=False, annotation / task / backend 对象保持 attached 可继续使用)。
    await db.commit()

    updated, children = await run_secondary_inference(
        db,
        annotation=annotation,
        task=task,
        backend=backend,
        write_target=data.write_target,
        write_keys=data.write_keys,
        label=data.label,
        model_id=data.model_id,
        model_variants=data.model_variants,
        params=data.params,
        task_type=data.task_type,
        prompt=data.prompt,
        class_filter=data.class_filter,
        pad=data.pad,
        user_id=current_user.id,
        shadow_session_factory=shadow_session_factory,
        dispatch_context_factory=dispatch_context_factory,
    )
    await heartbeat_task_lock_for_legacy_video(db, task, current_user.id)
    await record_annotation_actor(db, task, current_user.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_UPDATE,
        target_type="annotation",
        target_id=str(annotation_id),
        request=request,
        status_code=201,
        detail={
            "task_id": str(task_id),
            "secondary_inference": True,
            "ml_backend_id": str(data.ml_backend_id),
            # v0.23.3 ADR-0050 §5.4 · audit 记 pool + instance 双 ID。
            "ml_backend_pool_id": str(
                await ml_svc.pool_id_for_registry(data.ml_backend_id)
            ),
            "write_target": data.write_target,
            "created_children": len(children),
        },
    )
    await db.commit()
    await db.refresh(updated)
    for c in children:
        await db.refresh(c)
    return SecondaryInferenceResponse(
        annotation=AnnotationOut.model_validate(updated),
        created_children=[AnnotationOut.model_validate(c) for c in children],
    )


@router.post(
    "/{task_id}/annotations/{annotation_id}/propagate-to-task",
    response_model=PropagateResponse,
    status_code=201,
)
async def propagate_annotation_to_task(
    task_id: uuid.UUID,
    annotation_id: uuid.UUID,
    data: PropagateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    """v0.14.1 · 跨帧目标延续: 把源 annotation 复制到 target_task(同 project 同 scene)。

    源 task 需对当前用户可见; 目标 task 需可见且可写(未进 review/completed 锁态)。
    复制 geometry/class/attributes + 共享 track_id 跨帧链; box_3d 的 convention_at_create
    取目标 dataset 的 axis_convention(详 service.propagate)。
    """
    source_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, source_task, current_user, access=access)

    # 归属校验: annotation 必须属于 URL 里的源 task, 否则越权(借可见 task_id
    # 复制同 project 内不可见 batch 的他人草稿)。
    src_annotation = await db.get(Annotation, annotation_id)
    if src_annotation is None or src_annotation.task_id != task_id:
        raise HTTPException(status_code=404, detail="source annotation not found")

    target_task = await _load_task_or_404(db, data.target_task_id)
    target_access = await _resolve_task_access(db, target_task, current_user)
    await _assert_task_visible(db, target_task, current_user, access=target_access)
    _assert_task_editable(target_task, current_user, access=target_access)

    svc = AnnotationService(db)
    new_annotation, motion_compensated = await svc.propagate(
        source_annotation_id=annotation_id,
        target_task_id=data.target_task_id,
        user_id=current_user.id,
        override_psr=data.override_psr,
    )
    await TaskLockService(db).heartbeat(data.target_task_id, current_user.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_CREATE,
        target_type="annotation",
        target_id=str(new_annotation.id),
        request=request,
        status_code=201,
        detail={
            "task_id": str(data.target_task_id),
            "source_task_id": str(task_id),
            "source_annotation_id": str(annotation_id),
            "propagated": True,
            "motion_compensated": motion_compensated,
            "track_id": new_annotation.track_id,
            "class_name": new_annotation.class_name,
        },
    )
    await db.commit()
    await db.refresh(new_annotation)
    return PropagateResponse(
        annotation=AnnotationOut.model_validate(new_annotation),
        motion_compensated=motion_compensated,
    )


@router.post(
    "/{task_id}/annotations/propagate-batch",
    response_model=PropagateBatchResponse,
    status_code=201,
)
async def propagate_annotations_batch(
    task_id: uuid.UUID,
    data: PropagateBatchRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    """v0.15.1 · 多目标批量跨帧延续: 把源 task 的多个(或全部)box_3d 一次
    运动补偿 propagate 到目标 task。整批一个事务,任一失败全部回滚。
    """
    source_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, source_task, current_user, access=access)

    target_task = await _load_task_or_404(db, data.target_task_id)
    target_access = await _resolve_task_access(db, target_task, current_user)
    await _assert_task_visible(db, target_task, current_user, access=target_access)
    _assert_task_editable(target_task, current_user, access=target_access)

    svc = AnnotationService(db)
    results, motion_compensated = await svc.propagate_batch(
        source_task_id=task_id,
        target_task_id=data.target_task_id,
        annotation_ids=data.annotation_ids,
        user_id=current_user.id,
    )
    await TaskLockService(db).heartbeat(data.target_task_id, current_user.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_CREATE,
        target_type="task",
        target_id=str(data.target_task_id),
        request=request,
        status_code=201,
        detail={
            "source_task_id": str(task_id),
            "propagated_batch": True,
            "motion_compensated": motion_compensated,
            "count": len(results),
            "created_annotation_ids": [str(ann.id) for _, ann in results],
        },
    )
    await db.commit()
    for _, ann in results:
        await db.refresh(ann)
    return PropagateBatchResponse(
        items=[
            PropagateBatchItem(
                source_annotation_id=src_id,
                annotation=AnnotationOut.model_validate(ann),
            )
            for src_id, ann in results
        ],
        motion_compensated=motion_compensated,
    )


@router.post(
    "/{task_id}/annotations/interpolate-range",
    response_model=InterpolateRangeResponse,
    status_code=201,
)
async def interpolate_annotations_range(
    task_id: uuid.UUID,
    data: InterpolateRangeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    """v0.15.1 · 关键帧区间插值: 路径 task = 区间起点帧,body.to_task_id =
    终点帧;同 track_id 链两端各有一个 box_3d,中间帧自动生成插值框
    (source="interpolated")。中间帧已有同 track 标注 → 幂等跳过。
    v0.21.2 · ADR-0045 · 按 track_id 标识跨帧链。
    """
    from_task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, from_task, current_user, access=access)

    to_task = await _load_task_or_404(db, data.to_task_id)
    to_access = await _resolve_task_access(db, to_task, current_user)
    await _assert_task_visible(db, to_task, current_user, access=to_access)

    svc = AnnotationService(db)
    created, motion_compensated, skipped_frames = await svc.interpolate_range(
        track_id=data.track_id,
        from_task_id=task_id,
        to_task_id=data.to_task_id,
        user_id=current_user.id,
        assert_task_editable=lambda t: _assert_task_editable(
            t, current_user, access=access
        ),
        assert_task_visible=lambda t: _assert_task_visible(
            db, t, current_user, access=access
        ),
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_CREATE,
        target_type="task",
        target_id=str(task_id),
        request=request,
        status_code=201,
        detail={
            "interpolate_range": True,
            "track_id": data.track_id,
            "to_task_id": str(data.to_task_id),
            "motion_compensated": motion_compensated,
            "created": len(created),
            "created_annotation_ids": [str(a.id) for a in created],
            "skipped_frames": skipped_frames,
        },
    )
    await db.commit()
    for ann in created:
        await db.refresh(ann)
    return InterpolateRangeResponse(
        annotations=[AnnotationOut.model_validate(a) for a in created],
        motion_compensated=motion_compensated,
        skipped_frames=skipped_frames,
    )


@router.patch(
    "/{task_id}/annotations/{annotation_id}",
    response_model=AnnotationOut,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def update_annotation(
    task_id: uuid.UUID,
    annotation_id: uuid.UUID,
    data: AnnotationUpdate,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    # Match atomic Mask mutations: Task must be the first database row lock.
    # This also serializes class-only changes that alter a same-class Mask scope.
    _task = (
        await db.execute(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if _task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await _assert_task_visible(db, _task, current_user, access=access)
    _assert_task_editable(_task, current_user, access=access)
    svc = AnnotationService(db)
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    # 早 load 一次：用于 If-Match 校验 + 字段级审计 diff（attributes 变更）
    # Read metadata without a row lock first.  For Mask geometry, content and
    # upload locks must be acquired before the Annotation row lock; the Task row
    # above prevents another compliant Mask writer from changing this object in
    # between.
    existing = (
        await db.execute(select(Annotation).where(Annotation.id == annotation_id))
    ).scalar_one_or_none()
    if existing is None or not existing.is_active:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if existing.task_id != task_id:
        raise HTTPException(
            status_code=400, detail="Annotation does not belong to this task"
        )
    await assert_video_annotation_write_scope(
        db,
        task=_task,
        user=current_user,
        segment_id=existing.video_segment_id,
        geometry=fields.get("geometry", existing.geometry),
    )
    if "geometry" in fields and existing.is_locked:
        raise HTTPException(
            status_code=409,
            detail={"reason": "annotation_locked"},
        )

    # Reject missing/malformed/stale preconditions before any object-storage I/O.
    # The Task row lock serializes Mask writers, and the same version is checked
    # again after the Annotation row lock below.
    if_match = request.headers.get("If-Match", "").strip()
    expected_v: int | None = None
    if "geometry" in fields:
        previous_type = str((existing.geometry or {}).get("type") or "")
        proposed_geometry = fields["geometry"]
        proposed_type = str((proposed_geometry or {}).get("type") or "")
        requires_precondition = previous_type != proposed_type or proposed_type in {
            "raster_mask",
            "video_mask",
        }
        if requires_precondition and not if_match:
            raise HTTPException(
                status_code=428,
                detail={"reason": "if_match_required"},
            )
    if if_match:
        expected_version = if_match.removeprefix('W/"').removesuffix('"')
        try:
            expected_v = int(expected_version)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid If-Match format")
        if existing.version != expected_v:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "version_mismatch",
                    "current_version": existing.version,
                },
            )

    mask_payload_prepared = False
    next_geometry = fields.get("geometry")
    if isinstance(next_geometry, dict):
        validate_geometry_type_transition(
            tool_unit_id=existing.tool_unit_id,
            previous_geometry=existing.geometry,
            next_geometry=next_geometry,
        )
    if isinstance(next_geometry, dict) and next_geometry.get("type") in {
        "raster_mask",
        "video_mask",
        "video_track_mask",
    }:
        try:
            await prepare_mask_geometry_for_annotation_write(db, _task, next_geometry)
        except RasterMaskContractError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        mask_payload_prepared = True

    # Lock before checking If-Match. Under READ COMMITTED a waiter observes the
    # committed version from the preceding writer, so two requests carrying the
    # same ETag cannot both pass and publish version N+1.
    existing = (
        await db.execute(
            select(Annotation)
            .where(Annotation.id == annotation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if existing is None or not existing.is_active or existing.task_id != task_id:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if "geometry" in fields and existing.is_locked:
        raise HTTPException(
            status_code=409,
            detail={"reason": "annotation_locked"},
        )

    before_attributes: dict | None = None
    if "attributes" in fields:
        before_attributes = dict(existing.attributes or {})

    # 乐观并发控制：行锁后再次校验相同 ETag。
    if expected_v is not None:
        if existing.version != expected_v:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "version_mismatch",
                    "current_version": existing.version,
                },
            )

    try:
        annotation = await svc.update(
            annotation_id,
            mask_payload_prepared=mask_payload_prepared,
            **fields,
        )
    except RasterMaskContractError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if not annotation:
        raise HTTPException(status_code=404, detail="Annotation not found")
    await record_annotation_actor(db, _task, current_user.id)
    await heartbeat_task_lock_for_legacy_video(db, _task, current_user.id)
    _audit_action = (
        AuditAction.TASK_REVIEWER_EDIT
        if _task.status == "review"
        and (access.is_manager or access.project_role == "reviewer")
        else AuditAction.ANNOTATION_UPDATE
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=_audit_action,
        target_type="annotation",
        target_id=str(annotation.id),
        request=request,
        status_code=200,
        detail={"task_id": str(task_id), "fields": list(fields.keys())},
    )
    # 字段级审计：每个变更的 attribute key 单独记一行，便于 GIN 索引按 field_key 过滤
    # v0.6.3 Q-2：N 个属性变更 → 一次 add_all + 一次 flush（原本 N 次 flush）
    if before_attributes is not None:
        after_attributes = dict(annotation.attributes or {})
        all_keys = set(before_attributes.keys()) | set(after_attributes.keys())
        change_items: list[dict] = []
        for key in sorted(all_keys):
            before_v = before_attributes.get(key)
            after_v = after_attributes.get(key)
            if before_v == after_v:
                continue
            change_items.append(
                {
                    "target_id": str(annotation.id),
                    "detail": {
                        "task_id": str(task_id),
                        "field_key": key,
                        "before": before_v,
                        "after": after_v,
                    },
                }
            )
        if change_items:
            await AuditService.log_many(
                db,
                actor=current_user,
                action=AuditAction.ANNOTATION_ATTRIBUTE_CHANGE,
                target_type="annotation",
                request=request,
                status_code=200,
                items=change_items,
            )
    await db.commit()
    await db.refresh(annotation)
    response.headers["ETag"] = f'W/"{annotation.version}"'
    return annotation


@router.post(
    "/{task_id}/annotations/video/track-compositions",
    response_model=VideoTrackCompositionResponse,
)
async def compose_video_tracks(
    task_id: uuid.UUID,
    data: VideoTrackCompositionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    if not data.annotation_ids:
        raise HTTPException(status_code=400, detail="annotation_ids is required")
    if len(set(data.annotation_ids)) != len(data.annotation_ids):
        raise HTTPException(status_code=400, detail="annotation_ids must be unique")

    annotations: list[Annotation] = []
    for annotation_id in data.annotation_ids:
        ann = await db.get(Annotation, annotation_id)
        if ann is None or not ann.is_active:
            raise HTTPException(status_code=404, detail="Annotation not found")
        if ann.task_id != task_id:
            raise HTTPException(
                status_code=400, detail="Annotation does not belong to this task"
            )
        annotations.append(ann)
        await assert_video_annotation_write_scope(
            db,
            task=task,
            user=current_user,
            segment_id=ann.video_segment_id,
            geometry=ann.geometry,
        )

    svc = AnnotationService(db)
    try:
        updated, created, deleted_ids = await svc.compose_video_tracks(
            task=task,
            annotations=annotations,
            user_id=current_user.id,
            operation=data.operation,
            frame_index=data.frame_index,
            delete_sources=data.delete_sources,
            gap_mode=data.gap_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await heartbeat_task_lock_for_legacy_video(db, task, current_user.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_UPDATE,
        target_type="annotation",
        request=request,
        status_code=200,
        detail={
            "task_id": str(task_id),
            "operation": f"video_track.{data.operation}",
            "annotation_ids": [
                str(annotation_id) for annotation_id in data.annotation_ids
            ],
            "frame_index": data.frame_index,
            "created_count": len(created),
            "updated_count": len(updated),
            "deleted_count": len(deleted_ids),
        },
    )
    await db.commit()
    for ann in [*updated, *created]:
        await db.refresh(ann)
    return VideoTrackCompositionResponse(
        operation=data.operation,
        updated_annotations=[
            AnnotationOut.model_validate(ann, from_attributes=True) for ann in updated
        ],
        created_annotations=[
            AnnotationOut.model_validate(ann, from_attributes=True) for ann in created
        ],
        deleted_annotation_ids=deleted_ids,
    )


@router.post(
    "/{task_id}/annotations/{annotation_id}/video/convert-to-bboxes",
    response_model=VideoTrackConvertToBboxesResponse,
)
async def convert_video_track_to_bboxes(
    task_id: uuid.UUID,
    annotation_id: uuid.UUID,
    data: VideoTrackConvertToBboxesRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    annotation = await db.get(Annotation, annotation_id)
    if annotation is None or not annotation.is_active:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if annotation.task_id != task_id:
        raise HTTPException(
            status_code=400, detail="Annotation does not belong to this task"
        )
    await assert_video_annotation_write_scope(
        db,
        task=task,
        user=current_user,
        segment_id=annotation.video_segment_id,
        geometry=annotation.geometry,
    )
    if (annotation.geometry or {}).get("type") != "video_track_bbox":
        raise HTTPException(
            status_code=400, detail="Annotation is not a video_track_bbox"
        )

    svc = AnnotationService(db)
    try:
        (
            source,
            created,
            deleted_source,
            removed_frame_indexes,
        ) = await svc.convert_video_track_to_bboxes(
            task=task,
            annotation=annotation,
            user_id=current_user.id,
            operation=data.operation,
            scope=data.scope,
            frame_index=data.frame_index,
            frame_mode=data.frame_mode,
            frame_count=await _video_frame_count(db, task),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await heartbeat_task_lock_for_legacy_video(db, task, current_user.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_UPDATE,
        target_type="annotation",
        target_id=str(annotation_id),
        request=request,
        status_code=200,
        detail={
            "task_id": str(task_id),
            "operation": "video_track.convert_to_bboxes",
            "convert_operation": data.operation,
            "scope": data.scope,
            "frame_mode": data.frame_mode,
            "frame_index": data.frame_index,
            "created_count": len(created),
            "deleted_source": deleted_source,
        },
    )
    await db.commit()
    for ann in created:
        await db.refresh(ann)
    if source is not None:
        await db.refresh(source)
    return VideoTrackConvertToBboxesResponse(
        source_annotation=(
            AnnotationOut.model_validate(source, from_attributes=True)
            if source is not None
            else None
        ),
        created_annotations=[
            AnnotationOut.model_validate(ann, from_attributes=True) for ann in created
        ],
        deleted_source=deleted_source,
        removed_frame_indexes=removed_frame_indexes,
    )


@router.delete(
    "/{task_id}/annotations/{annotation_id}",
    status_code=204,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def delete_annotation(
    task_id: uuid.UUID,
    annotation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    # Keep the same Task -> Annotation lock order as task-scoped atomic Mask
    # mutations. Otherwise DELETE can hold Annotation while waiting for Task,
    # forming a deadlock cycle with a concurrent atomic mutation.
    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    # 先取一份 detail 供 audit 用（soft delete 之后字段仍能读，但安全起见提前）
    pre = await db.get(Annotation, annotation_id)
    if pre is None or pre.task_id != task_id or not pre.is_active:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if pre.is_locked:
        raise HTTPException(
            status_code=409,
            detail={"reason": "annotation_locked"},
        )
    await assert_video_annotation_write_scope(
        db,
        task=task,
        user=current_user,
        segment_id=pre.video_segment_id,
        geometry=pre.geometry,
    )
    pre_class = pre.class_name
    svc = AnnotationService(db)
    ok = await svc.delete(annotation_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Annotation not found")
    await record_annotation_actor(db, task, current_user.id)
    await heartbeat_task_lock_for_legacy_video(db, task, current_user.id)
    # v0.7.2 · annotation 编辑历史可追溯
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_DELETE,
        target_type="annotation",
        target_id=str(annotation_id),
        request=request,
        status_code=204,
        detail={"task_id": str(task_id), "soft": True, "class_name": pre_class},
    )
    await db.commit()
