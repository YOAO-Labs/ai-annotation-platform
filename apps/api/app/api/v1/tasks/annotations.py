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
from app.db.models.annotation import Annotation
from app.schemas.annotation import (
    AnnotationCreate,
    AnnotationListPage,
    AnnotationOut,
    AnnotationUpdate,
    InterpolateRangeRequest,
    InterpolateRangeResponse,
    NeighborAnnotationsResponse,
    NeighborFrameAnnotations,
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
from app.application.annotation_create import _AnnotationCreateSqlAdapter
from app.application.annotation_cross_frame import _AnnotationCrossFrameSqlAdapter
from app.application.annotation_mutation import (
    _AnnotationMutationSqlAdapter,
    validate_annotation_update_preconditions,
)
from app.application.annotation_secondary_inference import (
    _AnnotationSecondaryInferenceSqlAdapter,
)
from app.services.annotation import (
    AnnotationService,
    annotation_create_request_digest,
)
from app.services.annotation_create_contract import (
    AnnotationCreateCommand,
    AnnotationCreateError,
)
from app.services.annotation_mutation_contract import (
    AnnotationDeleteCommand,
    AnnotationMutationError,
    AnnotationUpdateCommand,
)
from app.services.annotation_secondary_inference_contract import (
    SecondaryInferenceWritePort,
)
from app.services.audit import AuditAction, AuditService, audit_origin_from_request
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
    preflight_mask_payload_for_write,
)
from app.services.labeling_write_scope import (
    capture_task_source_binding,
    task_source_binding_matches,
)
from app.services.secondary_inference import run_secondary_inference
from app.services.video_collaboration import (
    assert_video_annotation_write_scope,
    collaboration_enabled,
    heartbeat_task_lock_for_legacy_video,
)
from app.services.project_access import get_workspace_project


from app.services import task_access
from app.api.v1.tasks._shared import (
    _load_task_or_404,
    _video_frame_count,
    require_task_annotation_write,
    require_task_annotation_write_strict,
)
from aap_workspace_contract.access import ProjectAccess
from app.services.labeling_write_scope import capture_labeling_write_scope
from app.services.identity_contracts import IdentityActorFacts
from app.services.labeling_core_read_relations import (
    annotation_row,
    get_core_row,
    lock_task_for_core_read,
)

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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """v0.15.17 · 中心 task 前后 k 帧的邻帧标注,一次性返回。

    替代前端「对 2k 个邻帧 task 各发一条 getAnnotations + client 端按 track_id 过滤」。
    v0.21.2 · ADR-0045 · 跨帧链按 track_id(原 group_id)过滤。
    非 scene / 历史未 backfill 的 task → 200 + frames=[](与 neighbors 端点一致,不报错)。

    v0.15.26 · 可见性:与被替代的旧链路一致,邻帧逐 task 复核 batch 可见性 / 分派状态
    (`task_access.visible_task_ids`)。对调用者不可见的邻帧(如分派给别人 / 状态非可见的 batch)
    仍返回 frame 占位但 `annotations=[]`,不外泄他人 batch 的框几何。
    """
    from app.services.labeling_scene import (
        SceneFrameIndexInconsistent,
        get_neighbors_for_task,
    )

    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)

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
    project = await get_workspace_project(db, actor_id=None, project_id=task.project_id)
    visible_ids = await task_access.visible_task_ids(
        db, project, current_user.id, list(frame_by_task.keys())
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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    project = await get_workspace_project(db, actor_id=None, project_id=task.project_id)
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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """v0.7.6 · keyset 分页变体。单 task 1000+ 框场景下避免一次性大列表阻塞。

    cursor 编码：base64(`{ts_isoformat}|{annotation_id}`)，与 audit_logs 端点一致。
    """
    from uuid import UUID as _UUID

    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    project = await get_workspace_project(db, actor_id=None, project_id=task.project_id)
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Optional durable creation identity (1–128 characters). Reuse only with the same request.",
    ),
):
    task = await _load_task_or_404(db, task_id)
    source_binding = capture_task_source_binding(task)
    preflight_access = await task_access.resolve_task_access(db, task, current_user.id)
    await task_access.assert_task_visible(
        db, task, current_user.id, access=preflight_access
    )
    task_access.assert_annotation_write_allowed(task, preflight_access)
    task_access.assert_task_editable(task, current_user.id, access=preflight_access)
    await task_access.assert_review_adjustment_evidence(
        db, task, current_user.id, preflight_access
    )
    await assert_video_annotation_write_scope(
        db,
        task=task,
        user=current_user,
        segment_id=data.video_segment_id,
        geometry=data.geometry.model_dump(),
    )

    normalized_idempotency_key = idempotency_key
    has_receipt = False
    if idempotency_key is not None:
        normalized_idempotency_key = idempotency_key.strip()
        if not normalized_idempotency_key or len(normalized_idempotency_key) > 128:
            raise HTTPException(
                status_code=400,
                detail="Idempotency-Key must contain 1 to 128 characters",
            )
        has_receipt = (
            await AnnotationService(db).annotation_create_receipt_response(
                task_id=task_id,
                actor_id=current_user.id,
                idempotency_key=normalized_idempotency_key,
                request_digest=annotation_create_request_digest(
                    data.model_dump(mode="json")
                ),
            )
        ) is not None
    try:
        verified_mask_content = (
            None
            if has_receipt
            else await preflight_mask_payload_for_write(
                db, task, data.geometry.model_dump()
            )
        )
    except RasterMaskContractError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    # Fence current membership before the Task lock; object verification must
    # not extend either lock's lifetime.
    access = await task_access.resolve_task_access(
        db, task, current_user.id, lock_membership=True
    )
    task = await lock_task_for_core_read(db, task_id, populate_existing=True)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_annotation_write_allowed(task, access)
    task_access.assert_task_editable(task, current_user.id, access=access)
    await task_access.assert_review_adjustment_evidence(
        db, task, current_user.id, access
    )
    if not task_source_binding_matches(task, source_binding):
        raise HTTPException(status_code=409, detail={"reason": "source_changed"})
    await assert_video_annotation_write_scope(
        db,
        task=task,
        user=current_user,
        segment_id=data.video_segment_id,
        geometry=data.geometry.model_dump(),
    )

    try:
        return await _AnnotationCreateSqlAdapter(
            db,
            task=task,
            actor=current_user,
            verified_mask_content=verified_mask_content,
        ).create(
            AnnotationCreateCommand(
                task_id=task.id,
                actor_id=current_user.id,
                data=data,
                idempotency_key=normalized_idempotency_key,
                audit_origin=audit_origin_from_request(request),
            )
        )
    except AnnotationCreateError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers=exc.headers
        ) from exc
    except RasterMaskContractError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write_strict),
):
    """选中框单框二次推理: 在选中框 ROI 上同步跑一个能力, 产物落库。

    属性型写回原框 (origin=ai)、几何型建子框 (parent=选中框)。复用批量 pipeline 下游
    阶段的 crop 投递 + 产物归位, 不走 worker。

    仅超级管理员、项目管理员和标注员可调用, 仍需通过任务可见性及可编辑状态校验。
    审核员调用返回 403; 人工审核修改仍使用普通标注接口。
    """
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)

    annotation = await get_core_row(db, annotation_row, annotation_id)
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

    write_scope = capture_labeling_write_scope(
        task,
        actor_id=current_user.id,
        actor_known=True,
        operation="secondary_inference",
        execution_scope=None,
        execution_scope_known=True,
        job_id=None,
        job_id_known=True,
        source_annotation=annotation,
    )

    # 释放当前只读事务再进入 10-30s 的远程 predict, 避免 async 连接池在高并发下饥饿
    # (expire_on_commit=False, annotation / task / backend 对象保持 attached 可继续使用)。
    await db.commit()

    inference_result = await run_secondary_inference(
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
        visual_task=data.task,
        prompt_type=data.prompt_type,
        prompt=data.prompt,
        class_filter=data.class_filter,
        pad=data.pad,
        shadow_session_factory=shadow_session_factory,
        dispatch_context_factory=dispatch_context_factory,
    )
    write_port: SecondaryInferenceWritePort = _AnnotationSecondaryInferenceSqlAdapter(
        db,
        task=task,
        annotation=annotation,
        actor=current_user,
        ml_service=ml_svc,
    )
    try:
        return await write_port.apply_secondary_inference(
            write_scope=write_scope,
            result=inference_result,
            ml_backend_id=data.ml_backend_id,
            write_target=data.write_target,
            audit_origin=audit_origin_from_request(request),
        )
    except AnnotationMutationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=exc.headers,
        ) from exc


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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    """v0.14.1 · 跨帧目标延续: 把源 annotation 复制到 target_task(同 project 同 scene)。

    源 task 与目标 task 均需可见且可编辑；审核态调整沿用权限与证据冻结规则。
    复制 geometry/class/attributes + 共享 track_id 跨帧链; box_3d 的 convention_at_create
    取目标 dataset 的 axis_convention(详 service.propagate)。
    """
    source_task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(
        db, source_task, current_user.id, access=access
    )
    task_access.assert_task_editable(source_task, current_user.id, access=access)

    try:
        return await _AnnotationCrossFrameSqlAdapter(
            db,
            source_task=source_task,
            actor=current_user,
            access=access,
        ).propagate(
            source_task_id=source_task.id,
            annotation_id=annotation_id,
            actor_id=current_user.id,
            payload=data,
            audit_origin=audit_origin_from_request(request),
        )
    except AnnotationMutationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=exc.headers,
        ) from exc


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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    """v0.15.1 · 多目标批量跨帧延续: 把源 task 的多个(或全部)box_3d 一次
    运动补偿 propagate 到目标 task。整批一个事务,任一失败全部回滚。
    """
    source_task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(
        db, source_task, current_user.id, access=access
    )
    task_access.assert_task_editable(source_task, current_user.id, access=access)

    try:
        return await _AnnotationCrossFrameSqlAdapter(
            db,
            source_task=source_task,
            actor=current_user,
            access=access,
        ).propagate_batch(
            source_task_id=source_task.id,
            actor_id=current_user.id,
            payload=data,
            audit_origin=audit_origin_from_request(request),
        )
    except AnnotationMutationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=exc.headers,
        ) from exc


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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    """v0.15.1 · 关键帧区间插值: 路径 task = 区间起点帧,body.to_task_id =
    终点帧;同 track_id 链两端各有一个 box_3d,中间帧自动生成插值框
    (source="interpolated")。中间帧已有同 track 标注 → 幂等跳过。
    v0.21.2 · ADR-0045 · 按 track_id 标识跨帧链。
    """
    from_task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, from_task, current_user.id, access=access)
    task_access.assert_task_editable(from_task, current_user.id, access=access)

    try:
        return await _AnnotationCrossFrameSqlAdapter(
            db,
            source_task=from_task,
            actor=current_user,
            access=access,
        ).interpolate_range(
            from_task_id=from_task.id,
            actor_id=current_user.id,
            payload=data,
            audit_origin=audit_origin_from_request(request),
        )
    except AnnotationMutationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=exc.headers,
        ) from exc


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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    _task = await _load_task_or_404(db, task_id)
    if _task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    source_binding = capture_task_source_binding(_task)
    preflight_access = await task_access.resolve_task_access(db, _task, current_user.id)
    await task_access.assert_task_visible(
        db, _task, current_user.id, access=preflight_access
    )
    task_access.assert_annotation_write_allowed(_task, preflight_access)
    task_access.assert_task_editable(_task, current_user.id, access=preflight_access)
    await task_access.assert_review_adjustment_evidence(
        db, _task, current_user.id, preflight_access
    )
    verified_mask_content = None
    geometry = data.model_dump(exclude_unset=True).get("geometry")
    if isinstance(geometry, dict):
        preflight_annotation = await db.scalar(
            select(annotation_row)
            .where(annotation_row.id == annotation_id)
            .execution_options(populate_existing=True)
        )
        validate_annotation_update_preconditions(
            preflight_annotation,
            task_id=task_id,
            fields=data.model_dump(exclude_unset=True),
            if_match=request.headers.get("If-Match"),
        )
        try:
            verified_mask_content = await preflight_mask_payload_for_write(
                db, _task, geometry
            )
        except RasterMaskContractError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    # Recheck current membership before the Task lock after object verification.
    access = await task_access.resolve_task_access(
        db, _task, current_user.id, lock_membership=True
    )
    _task = await lock_task_for_core_read(db, task_id, populate_existing=True)
    if _task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await task_access.assert_task_visible(db, _task, current_user.id, access=access)
    task_access.assert_annotation_write_allowed(_task, access)
    task_access.assert_task_editable(_task, current_user.id, access=access)
    await task_access.assert_review_adjustment_evidence(
        db, _task, current_user.id, access
    )
    if not task_source_binding_matches(_task, source_binding):
        raise HTTPException(status_code=409, detail={"reason": "source_changed"})
    try:
        annotation = await _AnnotationMutationSqlAdapter(
            db,
            task=_task,
            actor=current_user,
            access=access,
            verified_mask_content=verified_mask_content,
        ).update(
            AnnotationUpdateCommand(
                task_id=_task.id,
                annotation_id=annotation_id,
                actor_id=current_user.id,
                data=data,
                if_match=request.headers.get("If-Match"),
                audit_origin=audit_origin_from_request(request),
            )
        )
    except AnnotationMutationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=exc.headers,
        ) from exc
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
    if not data.annotation_ids:
        raise HTTPException(status_code=400, detail="annotation_ids is required")
    if len(set(data.annotation_ids)) != len(data.annotation_ids):
        raise HTTPException(status_code=400, detail="annotation_ids must be unique")

    annotations: list[Annotation] = []
    for annotation_id in data.annotation_ids:
        ann = await get_core_row(db, annotation_row, annotation_id)
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
    annotation = await get_core_row(db, annotation_row, annotation_id)
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    # Keep the same Task -> Annotation lock order as task-scoped atomic Mask
    # mutations. Otherwise DELETE can hold Annotation while waiting for Task,
    # forming a deadlock cycle with a concurrent atomic mutation.
    task = await lock_task_for_core_read(db, task_id, populate_existing=True)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
    try:
        deleted = await _AnnotationMutationSqlAdapter(
            db,
            task=task,
            actor=current_user,
            access=access,
        ).delete(
            AnnotationDeleteCommand(
                task_id=task.id,
                annotation_id=annotation_id,
                actor_id=current_user.id,
                audit_origin=audit_origin_from_request(request),
            )
        )
    except AnnotationMutationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=exc.headers,
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Annotation not found")
