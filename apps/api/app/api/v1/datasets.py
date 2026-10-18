import uuid

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.datastructures import FormData, UploadFile as StarletteUploadFile
from starlette.requests import parse_options_header
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_source.contract import (
    SourceDeletionRequest,
    UploadCompleteRequest,
    UploadInitRequest,
)
from aap_assets_source.import_contract import ImportLimits
from aap_assets_source.ingest_contract import (
    IngestCommand,
    IngestReceipt,
    ZIP_MAX_BYTES,
)
from aap_assets_source.connectors.base import SourcePathError
from aap_assets_source.connectors.host_guard import ConnectorHostDenied
from aap_assets_source.connectors.registry import (
    target_host as connector_target_host,
    validate_source_path as validate_connector_source_path,
)
from aap_assets_catalog import (
    CreateDatasetRequest,
    DatasetPatch,
    GetDatasetRequest,
    ListDatasetsRequest,
    ListItemsRequest,
    UpdateDatasetRequest,
)
from aap_assets_catalog.associations_contract import ListDatasetProjectsRequest
from app.deps import (
    get_db,
    get_current_user,
    get_media_job_dispatcher,
    require_project_owner,
    require_roles,
    require_scopes,
)
from app.db.enums import UserRole
from app.db.models.project import Project
from app.db.models.user import User
from app.services.project_access import resolve_project_access_by_id
from app.schemas.dataset import (
    DatasetCreate,
    DatasetUpdate,
    DatasetOut,
    DatasetListResponse,
    DatasetItemListResponse,
    DatasetLinkRequest,
    DatasetUploadInitRequest,
    DatasetUploadInitResponse,
    DatasetImportFromConnectionRequest,
    DatasetImportFromConnectionResponse,
    SniffAxisConventionResponse,
)
from app.schemas.project import ProjectOut
from app.services.audit import AuditAction, AuditService
from app.services.dataset import DatasetService
from app.services.assets_catalog import AssetsCatalogGateway
from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.application.dataset_deletion import (
    unlink_dataset_from_project,
    preview_dataset_unlink,
)
from app.services.assets_source import (
    AssetsSourceGateway,
    encode_ingest_public_result,
)
from app.services.assets_import import DatasetImportCapabilities
from app.services.media_job_dispatch import MediaJobDispatcher
from app.services.storage import storage_service
from app.services.system_settings_service import SystemSettingsService
from aap_audit_contract import ProjectDeletionAuditContext

router = APIRouter()

_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)

# Allow room for multipart boundaries, per-part headers, and the closing delimiter.
_ZIP_MULTIPART_OVERHEAD_BYTES = 64 * 1024


class _ZipMultipartBodyTooLarge(MultiPartException):
    pass


def _zip_too_large_error() -> HTTPException:
    return HTTPException(
        413,
        detail=(
            f"ZIP 包超过 {ZIP_MAX_BYTES // 1024 // 1024}MB 限制。"
            "浏览器向导只适合单个原生 scene；多 scene / nuScenes 等大包请使用转换脚本。"
            "格式说明见 docs-site/user-guide/datasets/import-formats.md"
        ),
    )


async def _parse_bounded_zip_form(request: Request):
    content_type, _ = parse_options_header(request.headers.get("content-type"))
    if content_type != b"multipart/form-data":
        return FormData()

    max_body_bytes = ZIP_MAX_BYTES + _ZIP_MULTIPART_OVERHEAD_BYTES
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdecimal():
        normalized_length = content_length.lstrip("0") or "0"
        if (
            len(normalized_length) > len(str(max_body_bytes))
            or int(normalized_length) > max_body_bytes
        ):
            raise _zip_too_large_error()

    async def bounded_stream():
        body_bytes = 0
        async for chunk in request.stream():
            body_bytes += len(chunk)
            if body_bytes > max_body_bytes:
                raise _ZipMultipartBodyTooLarge("ZIP multipart request is too large")
            yield chunk

    parser = MultiPartParser(
        request.headers,
        bounded_stream(),
        max_files=1,
        max_fields=0,
    )
    try:
        try:
            return await parser.parse()
        except BaseException:
            # Starlette closes these files for MultiPartException only; disconnects
            # and task cancellation can otherwise leave the partially parsed ZIP open.
            for file in parser._files_to_close_on_error:
                try:
                    file.close()
                except BaseException:
                    pass
            raise
    except _ZipMultipartBodyTooLarge as exc:
        raise _zip_too_large_error() from exc
    except MultiPartException as exc:
        raise HTTPException(400, detail=exc.message) from exc


def _deletion_audit_context(request: Request) -> ProjectDeletionAuditContext:
    from ipaddress import ip_address

    from app.middleware.request_id import request_id_var
    from app.services.audit import audit_origin_from_request

    origin = audit_origin_from_request(request)
    try:
        ip = str(ip_address(origin.ip)) if origin.ip else None
    except ValueError:
        ip = None
    return ProjectDeletionAuditContext(
        method=origin.method,
        path=origin.path,
        ip=ip,
        request_id=request_id_var.get()[:36] or None,
    )


@router.get(
    "",
    response_model=DatasetListResponse,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def list_datasets(
    search: str | None = None,
    data_type: str | None = Query(default=None, max_length=30),
    has_scenes: bool | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=9_223_372_036_854_775_807),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await AssetsCatalogGateway(db).list(
        ListDatasetsRequest(
            actor_id=current_user.id,
            search=search,
            data_type=data_type,
            has_scenes=has_scenes,
            limit=limit,
            offset=offset,
        )
    )
    return DatasetListResponse(
        items=result.items,
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.post(
    "",
    response_model=DatasetOut,
    status_code=201,
    dependencies=[Depends(require_scopes("datasets:write"))],
    responses={
        201: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to replay the committed creation.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def create_dataset(
    data: DatasetCreate,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    operation_id = idempotency_key or uuid.uuid4()
    result = await AssetsCatalogGateway(db).create(
        CreateDatasetRequest(
            operation_id=operation_id,
            actor_id=current_user.id,
            name=data.name,
            description=data.description,
            data_type=data.data_type,
            is_temporal=data.is_temporal,
            axis_convention=data.axis_convention,
        )
    )
    response.headers["Idempotency-Key"] = str(operation_id)
    return result.dataset


@router.get(
    "/{dataset_id}",
    response_model=DatasetOut,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def get_dataset(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await AssetsCatalogGateway(db).get(
        GetDatasetRequest(actor_id=current_user.id, dataset_id=dataset_id)
    )
    return result.dataset


@router.put(
    "/{dataset_id}",
    response_model=DatasetOut,
    dependencies=[Depends(require_scopes("datasets:write"))],
    responses={
        200: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to replay the committed update.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def update_dataset(
    dataset_id: uuid.UUID,
    data: DatasetUpdate,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    operation_id = idempotency_key or uuid.uuid4()
    patch = DatasetPatch.model_validate(
        data.model_dump(exclude_unset=True), strict=True
    )
    result = await AssetsCatalogGateway(db).update(
        UpdateDatasetRequest(
            operation_id=operation_id,
            actor_id=current_user.id,
            dataset_id=dataset_id,
            patch=patch,
        )
    )
    response.headers["Idempotency-Key"] = str(operation_id)
    return result.dataset


@router.post(
    "/{dataset_id}/sniff-axis-convention",
    response_model=SniffAxisConventionResponse,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def sniff_axis_convention(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
):
    from aap_assets_catalog.spatial_contract import SniffAxisRequest
    from app.services.assets_spatial import (
        AssetsSpatialGateway,
        AssetsSpatialGatewayError,
    )

    try:
        result = await AssetsSpatialGateway(db).sniff_axis(
            SniffAxisRequest(actor_id=current_user.id, dataset_id=dataset_id)
        )
    except AssetsSpatialGatewayError as exc:
        if exc.code == "dataset_missing":
            raise HTTPException(status_code=404, detail="Dataset not found") from exc
        raise
    if result.result is None:
        return SniffAxisConventionResponse()
    return SniffAxisConventionResponse(
        best=result.result.best,
        score=result.result.score,
        candidates=result.result.candidates,
        source=result.result.source,
        camera_role=result.result.camera_role,
        camera_item_id=result.result.camera_item_id,
        per_camera=result.result.per_camera,
        agreement=result.result.agreement,
    )


@router.delete(
    "/{dataset_id}",
    status_code=204,
    dependencies=[Depends(require_scopes("datasets:write"))],
    responses={
        204: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to replay the committed deletion.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def delete_dataset(
    dataset_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    gateway = AssetsSourceGateway(db)
    receipt = await gateway.delete_source(
        SourceDeletionRequest(
            operation_id=idempotency_key or uuid.uuid4(),
            actor_id=current_user.id,
            dataset_id=dataset_id,
            audit_context=_deletion_audit_context(request),
        )
    )
    if not gateway.is_remote:
        await db.commit()
    response.headers["Idempotency-Key"] = str(receipt.operation_id)


# ── Items ───────────────────────────────────────────────────────────────────


@router.get(
    "/{dataset_id}/items",
    response_model=DatasetItemListResponse,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def list_dataset_items(
    dataset_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=9_223_372_036_854_775_807),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await AssetsCatalogGateway(db).items(
        ListItemsRequest(
            actor_id=current_user.id,
            dataset_id=dataset_id,
            limit=limit,
            offset=offset,
        )
    )
    return DatasetItemListResponse(
        items=[item.public_payload() for item in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.post(
    "/{dataset_id}/import-from-connection",
    response_model=DatasetImportFromConnectionResponse,
    status_code=202,
    dependencies=[Depends(require_scopes("datasets:write"))],
    responses={
        202: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to replay the accepted import.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def import_from_connection(
    dataset_id: uuid.UUID,
    payload: DatasetImportFromConnectionRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    from app.services import connector_guard

    capabilities = DatasetImportCapabilities(db)
    context = await capabilities.prepare(
        actor_id=current_user.id,
        dataset_id=dataset_id,
        connection_id=payload.connection_id,
    )
    try:
        await connector_guard.assert_connection_target_allowed(
            db,
            connector_target_host(context.connection_kind, context.connection_config),
        )
        validate_connector_source_path(
            context.connection_kind, context.connection_config, payload.source_path
        )
    except ConnectorHostDenied as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except SourcePathError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    clean_globs = [
        pattern.strip()
        for pattern in (payload.include_globs or [])
        if pattern and pattern.strip()
    ]
    # Capture both import budgets in the same committed request snapshot.  The
    # worker must never re-read global settings while enumerating files: queued
    # jobs retain the limits accepted here even after an admin changes them.
    import_limits = await SystemSettingsService.get_import_limits_snapshot(db)
    limits = ImportLimits.model_validate(import_limits, strict=True)
    operation_id = idempotency_key or uuid.uuid4()
    accepted = await capabilities.accept(
        operation_id=operation_id,
        actor_id=current_user.id,
        dataset_id=dataset_id,
        connection_id=payload.connection_id,
        request={
            "source_path": payload.source_path,
            "recursive": payload.recursive,
            "include_globs": clean_globs,
        },
        limits=limits,
        audit_context=_deletion_audit_context(request).model_dump(mode="json"),
    )
    await db.commit()
    response.headers["Idempotency-Key"] = str(operation_id)
    return DatasetImportFromConnectionResponse(job_id=accepted.job_id)


@router.post(
    "/{dataset_id}/items/upload-init",
    response_model=DatasetUploadInitResponse,
    dependencies=[Depends(require_scopes("datasets:write"))],
    responses={
        200: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to replay upload initialization.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def upload_init(
    dataset_id: uuid.UUID,
    data: DatasetUploadInitRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    gateway = AssetsSourceGateway(db)
    result = await gateway.init_upload(
        UploadInitRequest(
            item_id=idempotency_key or uuid.uuid4(),
            actor_id=current_user.id,
            dataset_id=dataset_id,
            file_name=data.file_name,
            content_type=data.content_type,
        )
    )
    if not gateway.is_remote:
        await db.commit()
    response.headers["Idempotency-Key"] = str(result.item_id)
    return DatasetUploadInitResponse(
        item_id=result.item_id,
        upload_url=result.upload_url,
        expires_in=result.expires_in,
    )


@router.post(
    "/{dataset_id}/items/upload-complete/{item_id}",
    dependencies=[Depends(require_scopes("datasets:write"))],
    responses={
        200: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to replay upload completion.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def upload_complete(
    dataset_id: uuid.UUID,
    item_id: uuid.UUID,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    media_dispatcher: MediaJobDispatcher = Depends(get_media_job_dispatcher),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    request = UploadCompleteRequest(
        operation_id=idempotency_key or uuid.uuid4(),
        actor_id=current_user.id,
        dataset_id=dataset_id,
        item_id=item_id,
    )
    gateway = AssetsSourceGateway(db)
    outcome = await gateway.complete_upload(request)
    if not gateway.is_remote:
        await db.commit()
    response.headers["Idempotency-Key"] = str(request.operation_id)
    if outcome.duplicate_of is not None:
        raise HTTPException(
            409,
            {
                "msg": "文件已存在（内容重复）",
                "duplicate_of": str(outcome.duplicate_of),
            },
        )
    if outcome.result is None:
        raise HTTPException(400, "File not found in storage")
    if outcome.file_type == "image":
        media_dispatcher.dispatch_thumbnail(item_id)
    elif outcome.file_type == "video":
        media_dispatcher.dispatch_video_metadata(item_id)
    return {"status": "ok", **outcome.result.model_dump(mode="json")}


async def _ingest_public_response(
    db: AsyncSession,
    gateway: AssetsSourceGateway,
    receipt: IngestReceipt,
    operation_id: uuid.UUID,
) -> Response:
    try:
        body = encode_ingest_public_result(receipt)
        if not gateway.is_remote:
            await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(503, detail="Assets source service is unavailable") from exc
    except Exception:
        await db.rollback()
        raise
    return Response(
        content=body,
        media_type="application/json",
        headers={"Idempotency-Key": str(operation_id)},
    )


@router.post(
    "/{dataset_id}/items/upload-zip",
    dependencies=[Depends(require_scopes("datasets:write"))],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
    responses={
        200: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to reconcile a lost response.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def upload_zip(
    dataset_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    """同步导入一个 ZIP，返回新增/去重/跳过、逐文件失败和场景推断结果。

    压缩包最多 200 MiB、5000 个文件；单文件最多 100 MiB，压缩比最多 100。
    响应不确定时使用相同文件和 Idempotency-Key 重放已完成回执。
    """
    operation_id = idempotency_key or uuid.uuid4()
    command = IngestCommand(
        operation_id=operation_id,
        actor_id=current_user.id,
        dataset_id=dataset_id,
    )
    try:
        gateway = AssetsSourceGateway(db)
        form = await _parse_bounded_zip_form(request)
        try:
            file = form.get("file")
            if not isinstance(file, StarletteUploadFile):
                raise HTTPException(422, detail="Field required: file")
            if file.size is not None and file.size > ZIP_MAX_BYTES:
                raise _zip_too_large_error()
            receipt = await gateway.ingest_zip(command, file.file)
        finally:
            await form.close()
    except Exception:
        await db.rollback()
        raise
    return await _ingest_public_response(db, gateway, receipt, operation_id)


@router.post(
    "/{dataset_id}/items/scan",
    dependencies=[Depends(require_scopes("datasets:write"))],
    responses={
        200: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to reconcile a lost response.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def scan_items(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    """同步扫描数据集旧存储前缀，只导入新文件。"""
    operation_id = idempotency_key or uuid.uuid4()
    command = IngestCommand(
        operation_id=operation_id,
        actor_id=current_user.id,
        dataset_id=dataset_id,
    )
    try:
        gateway = AssetsSourceGateway(db)
        receipt = await gateway.ingest_scan(command)
    except Exception:
        await db.rollback()
        raise
    return await _ingest_public_response(db, gateway, receipt, operation_id)


@router.post(
    "/{dataset_id}/backfill-dimensions",
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def backfill_dimensions(
    dataset_id: uuid.UUID,
    batch: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
):
    """为存量 image 类型 dataset_items 回填 width/height。

    一次最多处理 batch 条（默认 50），多次调用直到 processed == 0 视为完成。
    """
    from sqlalchemy import select as sa_select
    from app.db.models.dataset import DatasetItem

    rows = await db.execute(
        sa_select(DatasetItem)
        .where(
            DatasetItem.dataset_id == dataset_id,
            DatasetItem.file_type == "image",
            DatasetItem.width.is_(None),
        )
        .limit(batch)
    )
    items = list(rows.scalars().all())
    processed = 0
    failed = 0
    svc = DatasetService(db)
    for item in items:
        dims = storage_service.read_image_dimensions(
            item.file_path,
            bucket=storage_service.datasets_bucket,
        )
        if dims:
            await svc.set_item_dimensions(item.id, dims)
            processed += 1
        else:
            failed += 1
    await db.commit()
    return {
        "processed": processed,
        "failed": failed,
        "remaining_hint": len(items) == batch,
    }


@router.post(
    "/{dataset_id}/backfill-media",
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def backfill_media_endpoint(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
):
    """异步触发存量图像缩略图 / 视频元数据与 poster 回填。"""
    svc = DatasetService(db)
    ds = await svc.get(dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    from app.workers.media import backfill_media

    backfill_media.delay(str(dataset_id))
    return {"status": "queued", "dataset_id": str(dataset_id)}


@router.post(
    "/{dataset_id}/scenes/backfill",
    dependencies=[Depends(require_scopes("datasets:write"))],
    responses={
        200: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to replay the committed scene layout.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def backfill_scenes_endpoint(
    dataset_id: uuid.UUID,
    response: Response,
    mode: str = Query(
        "auto",
        description="single / per_subdirectory / auto",
        pattern="^(single|per_subdirectory|auto)$",
    ),
    dry_run: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    """v0.14.0 · 对 dataset 跑 scene layout backfill,补 scene + frame_index。

    幂等:dataset 已有 scene → 直接返回(notes 提示)。
    dry_run:不写库,返回会创建 / 赋值的统计。
    """
    from app.schemas.scene import InferenceResult
    from app.services.assets_spatial import AssetsSpatialGateway
    from app.services.assets_spatial import AssetsSpatialGatewayError
    from aap_assets_catalog.spatial_contract import BackfillScenesRequest

    operation_id = idempotency_key or uuid.uuid4()

    try:
        layout = await AssetsSpatialGateway(db).backfill_scenes(
            BackfillScenesRequest(
                operation_id=operation_id,
                actor_id=current_user.id,
                dataset_id=dataset_id,
                mode=mode,
                dry_run=dry_run,
            )
        )
    except AssetsSpatialGatewayError as exc:
        if exc.code == "dataset_missing":
            raise HTTPException(status_code=404, detail="Dataset not found") from exc
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    response.headers["Idempotency-Key"] = str(operation_id)
    return InferenceResult(
        dataset_id=dataset_id,
        created_scenes=layout.created_scenes,
        assigned_items=layout.assigned_items,
        skipped_items=layout.skipped_items,
        dry_run=layout.dry_run,
        notes=list(layout.notes),
    )


@router.delete(
    "/{dataset_id}/items/{item_id}",
    status_code=204,
    dependencies=[Depends(require_scopes("datasets:write"))],
    responses={
        204: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to replay the committed deletion.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def delete_item(
    dataset_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    gateway = AssetsSourceGateway(db)
    receipt = await gateway.delete_source(
        SourceDeletionRequest(
            operation_id=idempotency_key or uuid.uuid4(),
            actor_id=current_user.id,
            dataset_id=dataset_id,
            item_id=item_id,
            audit_context=_deletion_audit_context(request),
        )
    )
    if not gateway.is_remote:
        await db.commit()
    response.headers["Idempotency-Key"] = str(receipt.operation_id)


# ── Project linking ─────────────────────────────────────────────────────────


@router.post(
    "/{dataset_id}/link",
    dependencies=[Depends(require_scopes("datasets:write"))],
)
async def link_project(
    dataset_id: uuid.UUID,
    data: DatasetLinkRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = DatasetService(db)
    ds = await svc.get(dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")
    # Linking materializes this project's tasks: it is a project-management
    # action, not a platform-catalog action.  A project administrator without
    # ownership of the target project receives no management authority.
    _project, access = await resolve_project_access_by_id(
        db, actor_id=current_user.id, project_id=data.project_id
    )
    if not access.is_manager:
        raise HTTPException(
            status_code=403, detail="仅项目负责人或超级管理员可关联数据集"
        )
    from app.application.task_creation_errors import raise_task_creation_http_failure
    from app.services.task_creation_contract import TaskCreationFailure

    try:
        link_result = await DatasetTaskCreationWorkflow(db).link_project(
            dataset_id, data.project_id, actor_id=current_user.id
        )
    except TaskCreationFailure as failure:
        raise_task_creation_http_failure(failure)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.DATASET_LINK,
        target_type="dataset",
        target_id=str(dataset_id),
        request=request,
        status_code=200,
        detail={"project_id": str(data.project_id)},
    )
    await db.commit()

    # v0.12.0 · enqueue-after-commit：大 dataset 的建 task 走 Celery，必须在 commit
    # 之后再 delay，否则 worker 可能先于 link / async_job 行可见而读不到。
    if link_result.async_job_id is not None:
        from app.workers.create_tasks import run_create_tasks

        run_create_tasks.delay(str(link_result.async_job_id))
        return {
            "status": "linking",
            "dataset_id": str(dataset_id),
            "project_id": str(data.project_id),
            "async_job_id": str(link_result.async_job_id),
        }

    return {
        "status": "linked",
        "dataset_id": str(dataset_id),
        "project_id": str(data.project_id),
        "async_job_id": None,
        "created_tasks": link_result.created_tasks,
    }


@router.get(
    "/{dataset_id}/link/{project_id}/preview-unlink",
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def preview_unlink_project(
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
    _project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    preview = await preview_dataset_unlink(
        db, actor_id=current_user.id, dataset_id=dataset_id, project_id=project_id
    )
    return preview.model_dump(mode="json")


@router.delete(
    "/{dataset_id}/link/{project_id}",
    status_code=200,
    dependencies=[Depends(require_scopes("datasets:write"))],
    responses={
        200: {
            "headers": {
                "Idempotency-Key": {
                    "description": "Reuse this UUID to replay the committed unlink result.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        }
    },
)
async def unlink_project(
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
    request: Request,
    response: Response,
    _project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    operation_id = idempotency_key if idempotency_key is not None else uuid.uuid4()
    info = await unlink_dataset_from_project(
        db,
        dataset_id,
        project_id,
        actor_id=current_user.id,
        cleanup_bucket=storage_service.media_cache_bucket,
        operation_id=operation_id,
        audit_context=_deletion_audit_context(request),
    )
    if info is None:
        raise HTTPException(status_code=404, detail="Link not found")
    await db.commit()
    response.headers["Idempotency-Key"] = str(operation_id)
    return info


@router.get(
    "/{dataset_id}/projects",
    response_model=list[ProjectOut],
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def get_linked_projects(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await AssetsCatalogGateway(db).list_dataset_projects(
        ListDatasetProjectsRequest(actor_id=current_user.id, dataset_id=dataset_id)
    )
    return [ProjectOut.model_validate(project) for project in result.projects]


def _infer_file_type(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "other"
