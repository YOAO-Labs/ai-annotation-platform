"""Project-manager API for immutable training datasets."""

from copy import deepcopy
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.deps import (
    get_current_user,
    get_db,
    get_training_authority,
    get_training_dataset_dispatcher,
    require_project_owner,
    require_scopes,
)
from app.schemas.training_dataset import (
    TrainingDatasetCreate,
    TrainingDatasetOut,
    TrainingDatasetPage,
    TrainingDatasetPreflight,
    TrainingDatasetSelection,
)
from app.services import training_datasets as service
from app.services import project_task_dataset as task_datasets
from app.services import training_dataset_data as data
from app.services import training_storage_cleanup as cleanup
from app.services import training_dataset_storage as storage_owners
from app.services import training_storage_io
from app.services.training_storage_adoption import CAPACITY as _adoption_capacity
from app.services.training_list_filters import (
    ISODate,
    created_date_bounds,
    literal_search_pattern,
)
from yoao_task_contract import VisualTask
from app.services.identity_contracts import IdentityActorFacts
from app.services.project_write_guard import lock_project_manager
from app.services.training_dataset_bridge import TrainingDatasetError, content_digest
from app.services.training_authority_contract import TrainingAuthorityPort
from app.services.training_dataset_dispatch import TrainingDatasetDispatcher
from app.services.content_snapshot import ContentError
from app.services.original_request import connected_snapshot

router = APIRouter(
    prefix="/projects/{project_id}/training-datasets",
    dependencies=[Depends(require_scopes("annotations:read", "datasets:read"))],
)


def fail(exc: TrainingDatasetError):
    raise HTTPException(
        status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
    ) from exc


async def load_version(db, project, version_id, *, lock=False):
    version = await data.read_version(db, version_id, project_id=project.id, lock=lock)
    if version is None:
        raise HTTPException(status_code=404, detail="训练数据版本不存在")
    return version


async def load_version_output(db, project_id, version_id, *, include_manifest=True):
    version = await data.read_summary(
        db, project_id, version_id, include_manifest=include_manifest
    )
    if version is None:
        raise HTTPException(status_code=404, detail="训练数据版本不存在")
    if not include_manifest:
        version["manifest"] = {}
    return version


async def connected_preparation(request, operation):
    prepared = await connected_snapshot(request, operation, close_result=False)
    if prepared is None:
        raise HTTPException(499, {"code": "client_disconnected"})
    return prepared


@router.post(":preflight", response_model=TrainingDatasetPreflight)
async def preflight(
    body: TrainingDatasetSelection,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        prepared = await connected_preparation(
            request,
            service.prepare_selection(
                db,
                project,
                body,
                actor_id=user.id,
                api_key_id=getattr(request.state, "api_key_id", None),
                http_request=request,
            ),
        )
        return prepared.report
    except TrainingDatasetError as exc:
        fail(exc)
    except ContentError as exc:
        raise HTTPException(exc.status, {"code": exc.code}) from None


@router.post("", response_model=TrainingDatasetOut, status_code=202)
async def create_version(
    body: TrainingDatasetCreate,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dataset_dispatcher: TrainingDatasetDispatcher = Depends(
        get_training_dataset_dispatcher
    ),
):
    request_digest = content_digest(body.canonical_payload())
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project.id), 0)))
    )
    await lock_project_manager(db, user.id, project.id)
    existing = await data.read_request(db, project.id, body.request_id)
    if existing:
        if existing.request_digest != request_digest:
            raise HTTPException(
                status_code=409, detail="同一请求标识不能用于不同数据版本"
            )
        try:
            await data.ensure_membership(db, project.id, existing.id)
        except TrainingDatasetError as exc:
            fail(exc)
        await db.commit()
        return await load_version_output(db, project.id, existing.id)
    await db.commit()
    try:
        prepared = await connected_preparation(
            request,
            service.prepare_selection(
                db,
                project,
                service.selection_of(body),
                actor_id=user.id,
                api_key_id=getattr(request.state, "api_key_id", None),
                http_request=request,
            ),
        )
        report = prepared.report
        if report["digest"] != body.preflight_digest:
            raise HTTPException(status_code=409, detail="数据已变化，请重新预检")
        if not report["ready"]:
            raise HTTPException(status_code=422, detail=report)
        await db.execute(
            select(
                func.pg_advisory_xact_lock(func.hashtextextended(str(project.id), 0))
            )
        )
        await lock_project_manager(db, user.id, project.id)
        existing = await data.read_request(db, project.id, body.request_id, lock=True)
        if existing:
            if existing.request_digest != request_digest:
                raise HTTPException(409, "同一请求标识不能用于不同数据版本")
            await data.ensure_membership(db, project.id, existing.id)
            service.check_prepared_clock(prepared)
            await db.commit()
            return await load_version_output(db, project.id, existing.id)
        snapshot, report = await service.finalize_selection(db, prepared)
        version = await data.insert_version(
            db,
            data.NewVersion(
                project_id=project.id,
                creator_id=user.id,
                parent_id=body.parent_id,
                request_id=body.request_id,
                request_digest=request_digest,
                name=body.name.strip(),
                snapshot=snapshot,
                snapshot_digest=report["digest"],
                preparation_report=service.frozen_preparation_report(
                    report["digest"], report
                ),
            ),
        )
        await storage_owners.allocate(db, version)
        await task_datasets.ensure_membership(db, version)
        version = await service.enqueue_dataset(db, version)
        service.check_prepared_clock(prepared)
        if await request.is_disconnected():
            raise HTTPException(499, {"code": "client_disconnected"})
        await db.commit()
        await service.dispatch_dataset(db, version, dispatcher=dataset_dispatcher)
        return await load_version_output(db, project.id, version.id)
    except TrainingDatasetError as exc:
        fail(exc)
    except ContentError as exc:
        raise HTTPException(exc.status, {"code": exc.code}) from None


@router.get("", response_model=TrainingDatasetPage)
async def list_versions(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str | None = Query(
        None, pattern="^(pending|running|ready|failed|cancelled)$"
    ),
    task: VisualTask | None = None,
    task_dataset_id: uuid.UUID | None = None,
    q: str = Query("", max_length=120),
    created_from: ISODate | None = Query(
        None, description="创建日期下界，UTC 日期（含）"
    ),
    created_to: ISODate | None = Query(
        None, description="创建日期上界，UTC 日期（含整天）"
    ),
):
    start, end = created_date_bounds(created_from, created_to)
    if task_dataset_id is not None:
        stream = (
            await db.execute(select(data.stream_rows(project.id, task_dataset_id)))
        ).one_or_none()
        if stream is None:
            raise HTTPException(status_code=404, detail="任务数据集不存在")
        if task is not None and task != stream.task:
            raise HTTPException(
                status_code=422,
                detail={"code": "task_mismatch", "message": "任务与任务数据集不一致"},
            )
        task = stream.task
    version = data.dataset_lineage_row.c
    stmt = data.version_summaries().where(version.project_id == project.id)
    if status is not None:
        stmt = stmt.where(version.status == status)
    pattern = literal_search_pattern(q)
    if pattern is not None:
        stmt = stmt.where(version.name.ilike(pattern, escape="\\"))
    if start is not None:
        stmt = stmt.where(version.created_at >= start)
    if end is not None:
        stmt = stmt.where(version.created_at <= end)
    if task is not None:
        stmt = stmt.where(version.recognized_task == task)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        (
            await db.execute(
                stmt.order_by(version.created_at.desc(), version.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .mappings()
        .all()
    )
    return {"items": rows, "total": total}


@router.get("/{version_id}", response_model=TrainingDatasetOut)
async def get_version(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    include_manifest: bool = True,
):
    return await load_version_output(
        db, project.id, version_id, include_manifest=include_manifest
    )


@router.post("/{version_id}/retry", response_model=TrainingDatasetOut, status_code=202)
async def retry_version(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dataset_dispatcher: TrainingDatasetDispatcher = Depends(
        get_training_dataset_dispatcher
    ),
    training_authority: TrainingAuthorityPort = Depends(get_training_authority),
):
    await lock_project_manager(db, user.id, project.id)
    try:
        await service.lock_dataset_execution(db, version_id)
    except TrainingDatasetError as exc:
        fail(exc)
    version = await load_version(db, project, version_id, lock=True)
    if version.status not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="仅失败或已取消的版本可重试")
    try:
        await service.dataset_authority(
            db, version, training_authority=training_authority
        )
        storage_owners.resolve(version)
    except TrainingDatasetError as exc:
        fail(exc)
    version = await service.enqueue_dataset(db, version)
    await db.commit()
    await service.dispatch_dataset(db, version, dispatcher=dataset_dispatcher)
    return await load_version_output(db, project.id, version.id)


@router.get("/{version_id}/download")
async def download_version(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    await lock_project_manager(db, user.id, project.id)
    version = await load_version(db, project, version_id)
    if version.status != "ready" or not version.package_key:
        raise HTTPException(status_code=409, detail="训练数据版本尚未就绪")
    try:
        owner = storage_owners.resolve(version)
        owner.contains(version.package_key)
    except TrainingDatasetError as exc:
        fail(exc)

    def download(bound):
        if not training_storage_io.verify_upload(
            bound, version.package_key, bucket=owner.bucket
        ):
            raise HTTPException(
                status_code=409, detail="训练包不可用，请联系管理员检查持久存储"
            )
        return training_storage_io.sign_download(
            bound, version.package_key, bucket=owner.bucket, expires_in=600
        )

    try:
        url = await training_storage_io.read(
            db,
            owner,
            "training_dataset_download",
            download,
            object_key=version.package_key,
            sha256=version.package_sha256,
            size_bytes=version.size_bytes,
        )
    except TrainingDatasetError as exc:
        fail(exc)
    return {"url": url, "sha256": version.package_sha256}


def _adoption_state(version):
    return {
        **{
            name: deepcopy(getattr(version, name))
            for name in [
                "status",
                "snapshot",
                "manifest",
                "snapshot_digest",
                "package_key",
                "package_sha256",
                "size_bytes",
            ]
        },
        **{
            name: str(getattr(version, name))
            for name in ["id", "project_id", "creator_id", "attempt_id", "async_job_id"]
        },
    }


def _verify_retained_operation(owner, state):
    from app.services.managed_original_creation import create_client
    from app.services.training_storage_adoption import reject, verify

    client = create_client()
    try:
        return verify(client, owner, state)
    finally:
        try:
            client.close()
        except BaseException:
            reject("legacy_storage_close_unsettled", 503)


@router.post(
    "/{version_id}/storage-pin",
    response_model=TrainingDatasetOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def pin_legacy_storage(
    version_id: uuid.UUID,
    request: Request,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services import storage_access
    from app.services.dataset_content import key_authority
    from app.services.storage_registry_data import get_location

    await lock_project_manager(db, user.id, project.id)
    key_id = getattr(request.state, "api_key_id", None)

    async def proof():
        return [
            await key_authority(db, user, key_id, scope)
            for scope in ["annotations:read", "datasets:read", "datasets:write"]
        ]

    initial_authority = await proof()
    try:
        await service.lock_dataset_execution(db, version_id)
        version = await load_version(db, project, version_id)
        if version.storage_ref is not None:
            storage_owners.resolve(version)
            return await load_version_output(db, project.id, version.id)
        if version.status not in {"ready", "failed", "cancelled"}:
            raise TrainingDatasetError(
                "legacy_storage_version_active",
                "请先取消旧构建并等待读取写入收尾",
                status_code=409,
            )
        candidate = await storage_owners.adoption_candidate(db, version)
        owner = storage_owners.resolve_ref(candidate)
        state = _adoption_state(version)
        if not _adoption_capacity.acquire(blocking=False):
            raise TrainingDatasetError(
                "legacy_storage_verification_busy",
                "本节点已有旧副本核验，请稍后重试",
                status_code=429,
            )
        acquired_revision = None

        async def acquired(permit):
            nonlocal acquired_revision
            acquired_revision = permit.revision

        unsettled = False
        try:
            evidence = await storage_access.run(
                db,
                uuid.UUID(candidate["location_id"]),
                "training_storage_adoption",
                _verify_retained_operation,
                owner,
                state,
                on_acquired=acquired,
            )
        except BaseException as exc:
            unsettled = getattr(exc, "storage_operation_unsettled", False)
            raise
        finally:
            if not unsettled:
                _adoption_capacity.release()
        await lock_project_manager(db, user.id, project.id)
        if await proof() != initial_authority:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        await get_location(db, uuid.UUID(candidate["location_id"]), lock="share")
        version = await load_version(db, project, version_id, lock=True)
        if (
            version.storage_ref is not None
            or content_digest(_adoption_state(version)) != content_digest(state)
            or candidate != await storage_owners.adoption_candidate(db, version)
        ):
            raise TrainingDatasetError(
                "legacy_storage_context_changed",
                "版本或绑定已变化，请重新核验",
                status_code=409,
            )
        await storage_access.check(
            db,
            uuid.UUID(candidate["location_id"]),
            expected_revision=acquired_revision,
            lock=True,
        )
        await data.set_storage(
            db,
            version,
            {
                **candidate,
                "evidence": {
                    **evidence,
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                    "actor_id": str(user.id),
                    "api_key_id": str(key_id) if key_id else None,
                },
            },
        )
        await db.commit()
        return await load_version_output(db, project.id, version.id)
    except TrainingDatasetError as exc:
        fail(exc)
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None


@router.delete(
    "/{version_id}",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def delete_version(
    version_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project.id), 0)))
    )
    await lock_project_manager(db, user.id, project.id)
    try:
        await service.lock_dataset_execution(db, version_id)
    except TrainingDatasetError as exc:
        fail(exc)
    version = await load_version(db, project, version_id, lock=True)
    from app.services.active_learning_references import guard_source

    await guard_source(db, version_id=version.id)
    from app.services.evaluation_data import dataset_has_evaluation
    from app.services.learning_selection_data import dataset_references
    from app.services.training_run_data import dataset_run_dependencies

    references = await dataset_references(db, version.id)
    if references.has_study:
        raise HTTPException(
            status_code=409, detail="数据版本被参数搜索的冻结计划引用，不能删除"
        )

    if await dataset_has_evaluation(db, project.id, version.id):
        raise HTTPException(
            status_code=409, detail="数据版本被冻结样本评估引用，请先删除已结束的评估"
        )

    if references.has_policy:
        raise HTTPException(
            status_code=409, detail="数据被自动策略或触发记录引用，请先移除对应依赖"
        )
    if (
        version.status in {"pending", "running"}
        or await db.scalar(
            select(data.dataset_lineage_row.c.id)
            .where(data.dataset_lineage_row.c.parent_id == version.id)
            .limit(1)
        )
        or await dataset_run_dependencies(db, project.id, version.id)
    ):
        raise HTTPException(
            status_code=409,
            detail="运行中或被后续版本、训练实验、未清理增强预览引用的数据版本不能删除",
        )
    try:
        intent_id = await cleanup.stage_deletion(
            db, version, f"projects/{project.id}/datasets/{version.id}/"
        )
        await data.delete_version(db, version)
    except TrainingDatasetError as exc:
        fail(exc)
    await db.commit()
    await cleanup.purge_committed(db, intent_id)
