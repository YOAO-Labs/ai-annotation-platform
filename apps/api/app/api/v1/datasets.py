from typing import Annotated
import uuid
from datetime import datetime, timezone
from typing import Literal
from aap_assets_media.contract import (
    MediaFailure,
    MetadataSpec,
    RequestWorkRequest,
)

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
    GetDatasetRequest,
)
from aap_assets_catalog.associations_contract import ListDatasetProjectsRequest
from app.deps import (
    get_db,
    get_current_user,
    require_project_owner,
    require_roles,
    require_scopes,
)
from app.db.enums import UserRole
from app.api.v1.recovery_commands import get_registration_default
from app.db.models.project import Project
from app.db.models.user import User
from app.services.project_access import resolve_project_access_by_id
from app.schemas.dataset import (
    DatasetItemOut,
    DatasetWorkbench,
    DatasetTrainingVersionList,
    DatasetCreate,
    DatasetUpdate,
    DatasetOut,
    DatasetListResponse,
    DatasetItemListResponse,
    DatasetSceneOptions,
    DatasetLinkRequest,
    DatasetUploadInitRequest,
    DatasetUploadInitResponse,
    DatasetImportFromConnectionRequest,
    DatasetImportFromConnectionResponse,
    DatasetImageBackfillResponse,
    SniffAxisConventionResponse,
)
from app.schemas.dataset_view import DatasetItemQuery
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
from app.services.assets_media import media_gateway, media_http_error
from app.services.assets_import import DatasetImportCapabilities
from app.services.storage import storage_service
from app.services.content_snapshot import IMAGE_RESPONSES
from app.services.system_settings_service import SystemSettingsService
from aap_audit_contract import ProjectDeletionAuditContext
from app.services.identity_contracts import IdentityActorFacts


async def directory_dataset_write_access(
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    if (
        request.method in {"GET", "HEAD", "OPTIONS"}
        or "dataset_id" not in request.path_params
    ):
        return
    from app.services.dataset_source_access import (
        capture_dataset_write,
    )

    try:
        dataset_id = uuid.UUID(str(request.path_params["dataset_id"]))
    except ValueError:
        raise HTTPException(422, "Invalid dataset ID") from None
    request.state.directory_dataset_authority = await capture_dataset_write(
        db, dataset_id, actor, getattr(request.state, "api_key_id", None)
    )


async def recheck_directory_write(request, db, dataset_id, *, project_id=None):
    from app.services.dataset_source_access import validate_dataset_write

    await validate_dataset_write(
        db,
        dataset_id,
        getattr(request.state, "directory_dataset_authority", None),
        project_id=project_id,
        lock=True,
    )


router = APIRouter(dependencies=[Depends(directory_dataset_write_access)])

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
    state: Literal["active", "archived", "all"] = "active",
    data_type: str | None = None,
    has_scenes: bool | None = None,
    location_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=9_223_372_036_854_775_807),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    if location_id is not None:
        from app.services.dataset_source_access import require_manager
        from app.services.storage_location import get_visible

        actor = await require_manager(db, actor.id)
        await get_visible(db, location_id, actor)
    svc = DatasetService(db)
    items, total = await svc.list(
        search=search,
        state=state,
        data_type=data_type,
        has_scenes=has_scenes,
        limit=limit,
        offset=offset,
        location_id=location_id,
        actor=actor,
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
    current_user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
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
    current_user: IdentityActorFacts = Depends(get_current_user),
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
    request: Request,
    dataset_id: uuid.UUID,
    data: DatasetUpdate,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    await recheck_directory_write(request, db, dataset_id)
    svc = DatasetService(db)
    # v0.13.11 · 用 model_fields_set 区分「字段未传」与「显式传 None」(后者清除
    # metadata_["axis_convention"])。未传时不进 kwargs，让 service 走 _UNSET。
    update_kwargs: dict = {}
    if "axis_convention" in data.model_fields_set:
        update_kwargs["axis_convention"] = data.axis_convention
    ds = await svc.update(
        dataset_id,
        name=data.name,
        description=data.description,
        **update_kwargs,
    )
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await db.commit()
    # v0.13.11 · 在 commit 后 ds 各属性进入 expired 状态; get_with_project_count
    # 复用 identity map 中的同一对象, 直接访问 .updated_at 会触发 sync lazy reload
    # 在 async session 下抛 MissingGreenlet。显式 refresh 重读, 复刻 POST 的做法。
    await db.refresh(ds)
    result = await svc.get_with_project_count(dataset_id)
    return result


@router.post(
    "/{dataset_id}/sniff-axis-convention",
    response_model=SniffAxisConventionResponse,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def sniff_axis_convention(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
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
        },
        409: {
            "description": (
                "Source is referenced or busy, or the idempotency key conflicts. "
                "For source_in_use, remove references in the owning project or "
                "feature before retrying; the source and stored objects are retained."
            )
        },
    },
)
async def delete_dataset(
    dataset_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
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
    "/{dataset_id}/scene-options",
    response_model=DatasetSceneOptions,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def dataset_scene_options(
    dataset_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: str | None = Query(None, max_length=200),
    selected_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    from app.services.dataset_workbench import scene_options

    return await scene_options(
        db,
        dataset_id,
        limit=limit,
        offset=offset,
        search=search,
        selected_id=selected_id,
    )


@router.get(
    "/{dataset_id}/items",
    response_model=DatasetItemListResponse,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def list_dataset_items(
    dataset_id: uuid.UUID,
    query: Annotated[DatasetItemQuery, Query()],
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    from app.services.dataset_workbench import item_page

    items, total = await item_page(
        db,
        dataset_id,
        actor,
        limit=query.limit,
        offset=query.offset,
        source_id=query.source_id,
        state=query.availability,
        search=query.search,
        filters=query,
    )
    return DatasetItemListResponse(
        items=items, total=total, limit=query.limit, offset=query.offset
    )


@router.get(
    "/{dataset_id}/items/{item_id}",
    response_model=DatasetItemOut,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def get_dataset_item(
    dataset_id: uuid.UUID,
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    from app.services.dataset_workbench import item_page

    items, _ = await item_page(
        db, dataset_id, actor, limit=1, offset=0, item_id=item_id
    )
    if not items:
        raise HTTPException(404, "Dataset item not found")
    return items[0]


@router.get(
    "/{dataset_id}/items/{item_id}/preview",
    response_class=Response,
    responses=IMAGE_RESPONSES,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def get_dataset_item_preview(
    dataset_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    from app.db.models.dataset import DatasetItem
    from app.db.models.dataset_source import DatasetSourceItem
    from app.services.content_snapshot import SnapshotResponse
    from app.services.dataset_content import content_http_error
    from app.services.directory_previews import read_preview
    from app.services.dataset_workbench import visible_mapping_query

    if request.headers.get("range") is not None:
        raise HTTPException(416, "Range is not supported for previews")
    mapping = await db.scalar(
        visible_mapping_query(item_id, actor)
        .join(DatasetItem, DatasetItem.id == DatasetSourceItem.dataset_item_id)
        .where(DatasetItem.id == item_id, DatasetItem.dataset_id == dataset_id)
        .limit(1)
    )
    if mapping is None:
        raise HTTPException(404, "Dataset item preview not found")
    try:
        snapshot = await read_preview(
            db,
            mapping.source_id,
            mapping.path_digest,
            actor,
            getattr(request.state, "api_key_id", None),
        )
    except Exception as exc:
        raise content_http_error(exc) from None
    return SnapshotResponse(snapshot)


@router.get(
    "/{dataset_id}/workbench",
    response_model=DatasetWorkbench,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def get_dataset_workbench(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    from app.services.dataset_workbench import workbench

    return await workbench(db, dataset_id, actor)


@router.get(
    "/{dataset_id}/training-versions",
    response_model=DatasetTrainingVersionList,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def get_dataset_training_versions(
    dataset_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    from app.services.dataset_workbench import training_versions

    return await training_versions(db, dataset_id, actor, limit=limit, offset=offset)


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
    current_user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
    registration_default: bool = Depends(get_registration_default),
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
    import_limits = await SystemSettingsService.get_import_limits_snapshot(
        db, registration_default=registration_default
    )
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
    request: Request,
    dataset_id: uuid.UUID,
    data: DatasetUploadInitRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
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
    current_user: IdentityActorFacts = Depends(get_current_user),
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
    if outcome.file_type == "video":
        try:
            async with media_gateway(db) as media:
                await media.call(
                    "request_video_media",
                    RequestWorkRequest(
                        operation_id=request.operation_id,
                        actor_id=current_user.id,
                        dataset_item_id=item_id,
                        spec=MetadataSpec(),
                    ),
                )
            await db.commit()
        except MediaFailure as exc:
            raise media_http_error(exc) from None
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
    current_user: IdentityActorFacts = Depends(get_current_user),
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
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
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


_MEDIA_BACKFILL_STATUS = {
    "pending": "pending",
    "running": "running",
    "completed": "ready",
    "failed": "failed",
    "cancelled": "canceled",
}


def _media_backfill_response(job) -> DatasetImageBackfillResponse:
    payload = job.payload or {}
    purpose = payload.get("purpose")
    status = _MEDIA_BACKFILL_STATUS.get(job.status)
    if purpose not in {"dimensions", "thumbnail"} or status is None:
        raise HTTPException(status_code=502, detail="Invalid media backfill receipt")
    try:
        return DatasetImageBackfillResponse(
            dataset_id=uuid.UUID(payload["dataset_id"]),
            operation_id=uuid.UUID(payload["operation_id"]),
            job_id=job.id,
            purpose=purpose,
            status=status,
            accepted_before=datetime.fromisoformat(payload["accepted_before"]),
        )
    except (KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=502, detail="Invalid media backfill receipt"
        ) from None


async def _schedule_media_backfill(
    *,
    db: AsyncSession,
    actor: IdentityActorFacts,
    dataset_id: uuid.UUID,
    request: Request,
    purpose: Literal["dimensions", "thumbnail"],
    idempotency_key: uuid.UUID | None,
) -> DatasetImageBackfillResponse:
    """Admit one durable Jobs-owned backfill through the canonical media worker."""
    from app.db.models.async_job import AsyncJobKind
    from app.services import async_job as async_job_svc
    from app.services.data_management.actions import (
        assert_idempotent_request_matches,
        find_idempotent_job,
        lock_idempotency_key,
        normalize_idempotency_key,
        request_digest,
    )
    from app.services.jobs_dispatch import (
        prepare_job_dispatch,
        publish_prepared_dispatch,
    )
    from app.services.jobs_queue import dispatch_job

    await recheck_directory_write(request, db, dataset_id)
    from app.services.reference_access import reject_reference_scope

    await reject_reference_scope(db, dataset_id=dataset_id)
    await AssetsCatalogGateway(db).get(
        GetDatasetRequest(actor_id=actor.id, dataset_id=dataset_id)
    )
    key = normalize_idempotency_key(
        str(idempotency_key) if idempotency_key is not None else None
    )
    digest = request_digest({"dataset_id": str(dataset_id), "purpose": purpose})
    job = None
    if key is not None:
        await lock_idempotency_key(
            db,
            action="media_backfill",
            project_id=None,  # dataset-scoped action; the owner accepts NULL
            actor_id=actor.id,
            key=key,
        )
        existing = await find_idempotent_job(
            db,
            kind=AsyncJobKind.MEDIA_BACKFILL.value,
            project_id=None,  # dataset-scoped action; the owner accepts NULL
            actor_id=actor.id,
            key=key,
        )
        if existing is not None:
            assert_idempotent_request_matches(existing, digest=digest)
            if existing.status != "pending":
                return _media_backfill_response(existing)
            job = existing
    if job is None:
        accepted_before = datetime.now(timezone.utc)
        video_delegations = {}
        if purpose == "thumbnail":
            from app.services import media_item_reads
            from app.services import video_derivative_originals as originals
            from app.services.content_snapshot import ContentError

            try:
                for item in await media_item_reads.read_dataset_items(db, dataset_id):
                    if item.file_type != "video" or item.created_at > accepted_before:
                        continue
                    authority = await originals.authorize_policy(
                        db, "dataset", item.id, actor, request
                    )
                    video_delegations[str(item.id)] = originals.authorization(authority)
            except ContentError as exc:
                raise HTTPException(exc.status, detail={"code": exc.code}) from None
        payload = {
            "dataset_id": str(dataset_id),
            "purpose": purpose,
            "accepted_before": accepted_before.isoformat(),
            "operation_id": str(idempotency_key or uuid.uuid4()),
            "media_backfill_kind": "aap-media-backfill/v1",
            "video_delegations": video_delegations,
        }
        if key is not None:
            payload["data_manager_idempotency_key"] = key
            payload["data_manager_request_digest"] = digest
        job = await async_job_svc.create_job(
            db,
            kind=AsyncJobKind.MEDIA_BACKFILL.value,
            user_id=actor.id,
            project_id=None,
            payload=payload,
        )
    execution_id = str(job.id)
    dispatch_args = [str(job.id)]
    dispatch_kwargs: dict = {}
    lease = await prepare_job_dispatch(
        db,
        job_id=job.id,
        task_name="app.workers.media.run_media_backfill",
        execution_id=execution_id,
        args=dispatch_args,
        kwargs=dispatch_kwargs,
        options={"queue": "media"},
    )
    await recheck_directory_write(request, db, dataset_id)
    # The worker must see the durable row before Celery can start. A concurrent
    # retry with the same Idempotency-Key reuses the same job and envelope.
    await db.commit()
    await publish_prepared_dispatch(
        db,
        lease,
        publish=lambda: dispatch_job(
            task_name="app.workers.media.run_media_backfill",
            execution_id=execution_id,
            args=dispatch_args,
            kwargs=dispatch_kwargs,
            options={"queue": "media"},
        ),
    )
    return _media_backfill_response(job)


_BACKFILL_HEADERS = {
    202: {
        "headers": {
            "Idempotency-Key": {
                "description": "Reuse this UUID to replay the accepted media backfill.",
                "schema": {"type": "string", "format": "uuid"},
            }
        }
    }
}


@router.post(
    "/{dataset_id}/backfill-dimensions",
    response_model=DatasetImageBackfillResponse,
    status_code=202,
    dependencies=[Depends(require_scopes("datasets:write"))],
    responses=_BACKFILL_HEADERS,
)
async def backfill_dimensions(
    dataset_id: uuid.UUID,
    response: Response,
    request: Request,
    batch: int = Query(
        50,
        ge=1,
        le=200,
        deprecated=True,
        description="Retained for compatibility; the owner scans in bounded batches.",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    """Accept a durable bounded backfill of stored image dimensions."""
    del batch
    result = await _schedule_media_backfill(
        db=db,
        actor=current_user,
        dataset_id=dataset_id,
        request=request,
        purpose="dimensions",
        idempotency_key=idempotency_key,
    )
    response.headers["Idempotency-Key"] = str(result.operation_id)
    return result


@router.post(
    "/{dataset_id}/backfill-media",
    response_model=DatasetImageBackfillResponse,
    status_code=202,
    dependencies=[Depends(require_scopes("datasets:write"))],
    responses=_BACKFILL_HEADERS,
)
async def backfill_media_endpoint(
    dataset_id: uuid.UUID,
    response: Response,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    """Accept a durable canonical media backfill for existing dataset items."""
    result = await _schedule_media_backfill(
        db=db,
        actor=current_user,
        dataset_id=dataset_id,
        request=request,
        purpose="thumbnail",
        idempotency_key=idempotency_key,
    )
    response.headers["Idempotency-Key"] = str(result.operation_id)
    return result


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
    request: Request,
    dataset_id: uuid.UUID,
    response: Response,
    mode: str = Query(
        "auto",
        description="single / per_subdirectory / auto",
        pattern="^(single|per_subdirectory|auto)$",
    ),
    dry_run: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
    idempotency_key: uuid.UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    """v0.14.0 · 对 dataset 跑 scene layout backfill,补 scene + frame_index。

    幂等:dataset 已有 scene → 直接返回(notes 提示)。
    dry_run:不写库,返回会创建 / 赋值的统计。
    """
    await recheck_directory_write(request, db, dataset_id)
    from app.schemas.scene import InferenceResult
    from app.services.assets_spatial import AssetsSpatialGateway
    from app.services.assets_spatial import AssetsSpatialGatewayError
    from aap_assets_catalog.spatial_contract import BackfillScenesRequest

    operation_id = idempotency_key or uuid.uuid4()

    from app.services.reference_access import reject_reference_scope

    await reject_reference_scope(db, dataset_id=dataset_id)

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
        },
        409: {
            "description": (
                "Source is referenced or busy, or the idempotency key conflicts. "
                "For source_in_use, remove references in the owning project or "
                "feature before retrying; the source and stored objects are retained."
            )
        },
    },
)
async def delete_item(
    dataset_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
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
    current_user: IdentityActorFacts = Depends(get_current_user),
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
    await recheck_directory_write(request, db, dataset_id, project_id=data.project_id)
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
    current_user: IdentityActorFacts = Depends(get_current_user),
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
    current_user: IdentityActorFacts = Depends(get_current_user),
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
    current_user: IdentityActorFacts = Depends(get_current_user),
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
