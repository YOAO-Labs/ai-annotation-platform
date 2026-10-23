from typing import Literal
from typing import Annotated
import asyncio
import hashlib
import io
import mimetypes
import uuid
from pathlib import PurePosixPath

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    get_db,
    get_current_user,
    require_project_owner,
    require_roles,
    require_scopes,
)
from app.db.enums import UserRole
from app.db.models.project import Project
from app.db.models.dataset import Dataset
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
    SniffAxisConventionResponse,
)
from app.schemas.dataset_view import DatasetItemQuery
from app.schemas.project import ProjectOut
from app.services.audit import AuditAction, AuditService
from app.services.axis_sniffer import AxisSnifferService
from app.services.dataset import (
    DatasetService,
    ensure_dataset_storage_name_is_writable,
)
from app.services.mask_formats.safe_archive import (
    ArchiveLimits,
    ArchiveSafetyError,
    SafeZipArchive,
)
from app.services.storage import storage_service
from app.services.content_snapshot import IMAGE_RESPONSES
from app.services.system_settings_service import SystemSettingsService


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
    offset: int = Query(0, ge=0),
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
    return DatasetListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=DatasetOut, status_code=201)
async def create_dataset(
    data: DatasetCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
):
    svc = DatasetService(db)
    ds = await svc.create(
        name=data.name,
        description=data.description,
        data_type=data.data_type,
        user_id=current_user.id,
        axis_convention=data.axis_convention,
        is_temporal=data.is_temporal,
    )
    await db.commit()
    await db.refresh(ds)
    result = await svc.get_with_project_count(ds.id)
    return result


@router.get(
    "/{dataset_id}",
    response_model=DatasetOut,
    dependencies=[Depends(require_scopes("datasets:read"))],
)
async def get_dataset(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    svc = DatasetService(db)
    result = await svc.get_with_project_count(dataset_id)
    if not result:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return result


@router.put("/{dataset_id}", response_model=DatasetOut)
async def update_dataset(
    request: Request,
    dataset_id: uuid.UUID,
    data: DatasetUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
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
)
async def sniff_axis_convention(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MANAGERS)),
):
    svc = DatasetService(db)
    ds = await svc.get(dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    result = await AxisSnifferService(db).sniff_dataset(dataset_id)
    if result is None:
        return SniffAxisConventionResponse()
    return SniffAxisConventionResponse(
        best=result.best,
        score=result.score,
        candidates=result.candidates,
        source=result.source,
        camera_role=result.camera_role,
        camera_item_id=result.camera_item_id,
        per_camera=result.per_camera,
        agreement=result.agreement,
    )


@router.delete("/{dataset_id}", status_code=204)
async def delete_dataset(
    request: Request,
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
):
    await recheck_directory_write(request, db, dataset_id)
    svc = DatasetService(db)
    cleanup = await svc.delete(dataset_id)
    if cleanup is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await db.commit()
    await asyncio.to_thread(svc.cleanup_deleted_storage, cleanup)


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
)
async def import_from_connection(
    dataset_id: uuid.UUID,
    payload: DatasetImportFromConnectionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
):
    from app.db.models.async_job import AsyncJobKind
    from app.services import async_job as async_job_svc
    from app.services import connector_guard, storage_access
    from app.services.content_snapshot import ContentError
    from app.services.dataset_content import key_authority
    from app.services.storage_location import connector_location_id
    from app.services.sources import SourcePathError, validate_source_path
    from app.services.storage_connection import (
        ConnectorAccessDenied,
        StorageConnectionService,
        assert_connection_usable,
        target_host,
    )
    from app.workers.dataset_import import run_dataset_import

    svc = DatasetService(db)
    ds = await svc.get(dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    conn = await StorageConnectionService.get(db, payload.connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="连接器不存在")
    try:
        assert_connection_usable(current_user, conn)
    except ConnectorAccessDenied:
        raise HTTPException(status_code=404, detail="连接器不存在")
    try:
        await connector_guard.assert_connection_target_allowed(db, target_host(conn))
        validate_source_path(conn, payload.source_path)
    except connector_guard.ConnectorHostDenied as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SourcePathError as e:
        raise HTTPException(status_code=400, detail=str(e))

    clean_globs = [
        pattern.strip()
        for pattern in (payload.include_globs or [])
        if pattern and pattern.strip()
    ]
    connection_key_id = getattr(request.state, "api_key_id", None)
    connection_key_proof = await key_authority(
        db, current_user, connection_key_id, "datasets:write"
    )
    source_location_id = connector_location_id(conn.id)

    try:
        maintenance_revision = await storage_access.check(
            db, source_location_id, lock=True
        )
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    # Capture both import budgets in the same committed request snapshot.  The
    # worker must never re-read global settings while enumerating files: queued
    # jobs retain the limits accepted here even after an admin changes them.
    import_limits = await SystemSettingsService.get_import_limits_snapshot(db)
    job = await async_job_svc.create_job(
        db,
        kind=AsyncJobKind.DATASET_IMPORT.value,
        user_id=current_user.id,
        payload={
            "dataset_id": str(dataset_id),
            "dataset_display_id": ds.display_id,
            "dataset_name": ds.name,
            "connection_id": str(conn.id),
            "connection_name": conn.name,
            "connection_kind": conn.kind,
            "connection_revision": conn.revision,
            "source_location_id": str(source_location_id),
            "source_maintenance_revision": maintenance_revision,
            "connection_authority": {
                "api_key_id": str(connection_key_id) if connection_key_id else None,
                "key_proof": connection_key_proof,
            },
            "source_path": payload.source_path,
            "recursive": payload.recursive,
            "include_globs": clean_globs,
            "settings_snapshot": import_limits,
            "directory_authority": getattr(
                request.state, "directory_dataset_authority", None
            ),
        },
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.DATASET_IMPORT,
        target_type="dataset",
        target_id=str(dataset_id),
        request=request,
        status_code=202,
        detail={
            "connection_id": str(conn.id),
            "connection_kind": conn.kind,
            "source_path": payload.source_path,
            "recursive": payload.recursive,
            "include_globs": clean_globs,
            "settings_version": import_limits["version"],
        },
    )
    try:
        await storage_access.check(
            db, source_location_id, expected_revision=maintenance_revision, lock=True
        )
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    await recheck_directory_write(request, db, dataset_id)
    await db.commit()

    task = run_dataset_import.delay(str(job.id))
    job.celery_task_id = task.id
    await db.commit()
    return DatasetImportFromConnectionResponse(job_id=job.id)


@router.post(
    "/{dataset_id}/items/upload-init", response_model=DatasetUploadInitResponse
)
async def upload_init(
    request: Request,
    dataset_id: uuid.UUID,
    data: DatasetUploadInitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await recheck_directory_write(request, db, dataset_id)
    svc = DatasetService(db)
    ds = await svc.get(dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    ensure_dataset_storage_name_is_writable(ds.name)
    from app.services import managed_original_creation as creations
    from app.services.dataset_assets import MAX_DIRECT_UPLOAD_BYTES
    from app.services.content_snapshot import ContentError

    try:
        relative_name = await svc._unique_file_name(dataset_id, data.file_name)
        allocation = await creations.allocate(
            db,
            dataset_id=dataset_id,
            logical_path=relative_name,
            origin="direct",
            max_bytes=MAX_DIRECT_UPLOAD_BYTES,
            requested_by=current_user.id,
            authority=getattr(request.state, "directory_dataset_authority", None),
            staging=True,
            content_type=data.content_type,
        )
        storage_key = allocation["object_key"]
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Invalid dataset relative path"
        ) from exc
    file_type = _infer_file_type(data.content_type)

    item = await svc.add_item(
        dataset_id=dataset_id,
        file_name=relative_name,
        file_path=storage_key,
        file_type=file_type,
        logical_path=relative_name,
    )
    await db.commit()

    upload_url = storage_service.generate_upload_url(
        allocation["staging_key"],
        data.content_type,
        bucket=allocation["staging_bucket"],
    )
    return DatasetUploadInitResponse(
        item_id=item.id, upload_url=upload_url, expires_in=900
    )


@router.post("/{dataset_id}/items/upload-complete/{item_id}")
async def upload_complete(
    dataset_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await recheck_directory_write(request, db, dataset_id)
    from app.db.models.dataset import DatasetItem

    svc = DatasetService(db)
    await db.get(Dataset, dataset_id, with_for_update={"key_share": True})
    item = await db.get(DatasetItem, item_id)
    if not item or item.dataset_id != dataset_id:
        raise HTTPException(status_code=404, detail="Item not found")

    from app.services.dataset_assets import (
        find_verified_content,
        hash_stored_object,
        register_item_asset,
        MAX_DIRECT_UPLOAD_BYTES,
    )
    from app.services.dataset_storage_keys import managed_dataset_prefix

    from starlette.concurrency import run_in_threadpool
    from botocore.exceptions import ClientError
    from app.services import managed_original_creation as creations
    from app.services.content_snapshot import ContentError

    try:
        allocation = await creations.for_key(
            db, dataset_id=dataset_id, object_key=item.file_path
        )
        if allocation is not None:
            if allocation["origin"] != "direct":
                raise HTTPException(409, detail={"code": "creation_origin_changed"})
            created = await creations.complete_direct(db, allocation)
            content_hash, sha256, size, etag = (
                created.md5,
                created.sha256,
                created.size_bytes,
                created.etag,
            )
        else:
            # Existing upload URLs retain their historical completion contract.
            meta = storage_service.verify_upload(
                item.file_path, bucket=storage_service.datasets_bucket
            )
            if not meta:
                await recheck_directory_write(request, db, dataset_id)
                await svc.delete_item(item_id)
                await recheck_directory_write(request, db, dataset_id)
                await db.commit()
                await svc.cleanup_directory_previews()
                raise HTTPException(400, detail="File not found in storage")
            if (meta.get("ContentLength") or 0) > MAX_DIRECT_UPLOAD_BYTES:
                raise HTTPException(413, detail="Direct upload exceeds 5 GiB")
            content_hash, sha256, size, etag = await run_in_threadpool(
                hash_stored_object, item.file_path, meta
            )
        await recheck_directory_write(request, db, dataset_id)
        await register_item_asset(db, item, sha256=sha256, size_bytes=size, etag=etag)
        if allocation is not None:
            await creations.attach(db, item, created.creation_id)
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail="Dataset content or storage identity changed"
        ) from exc
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "PreconditionFailed",
            "412",
            "NoSuchKey",
            "NoSuchVersion",
            "404",
        }:
            raise HTTPException(
                status_code=409, detail="Dataset content or storage identity changed"
            ) from exc
        raise
    existing = await find_verified_content(
        db, dataset_id, sha256, exclude_item_id=item_id, legacy_md5=content_hash
    )
    if existing:
        # A new sealed object remains a durable, unlinked creation responsibility.
        await recheck_directory_write(request, db, dataset_id)
        try:
            if allocation is None and item.file_path.startswith(
                managed_dataset_prefix(dataset_id)
            ):
                storage_service.delete_object(
                    item.file_path, bucket=storage_service.datasets_bucket
                )
        except Exception:
            pass
        await svc.delete_item(item_id)
        await recheck_directory_write(request, db, dataset_id)
        await db.commit()
        await svc.cleanup_directory_previews()
        raise HTTPException(
            status_code=409,
            detail={
                "msg": "文件已存在（内容重复）",
                "duplicate_of": str(existing.id),
            },
        )
    item.content_hash = content_hash
    item.file_size = size

    if item.file_type == "image" and (item.width is None or item.height is None):
        dims = storage_service.read_image_dimensions(
            item.file_path,
            bucket=storage_service.datasets_bucket,
        )
        if dims:
            item.width, item.height = dims

    await recheck_directory_write(request, db, dataset_id)
    linked_tasks = await svc.create_tasks_for_items(dataset_id, [item.id])
    await recheck_directory_write(request, db, dataset_id)
    await db.commit()

    if item.file_type == "image":
        from app.workers.media import generate_thumbnail

        generate_thumbnail.delay(str(item_id))
    elif item.file_type == "video":
        from app.workers.media import generate_video_metadata

        generate_video_metadata.delay(str(item_id))

    return {"status": "ok", "item_id": str(item_id), "linked_tasks": linked_tasks}


_ZIP_MAX_BYTES = 200 * 1024 * 1024  # 200 MB
_ZIP_MAX_ENTRIES = 5000  # 防 zip bomb：限制条目数
_PER_FILE_MAX_BYTES = 100 * 1024 * 1024  # 单文件 100MB 上限


def _normalize_zip_relpath(name: str) -> str | None:
    """把 ZIP entry 名规范化为安全的相对路径。

    - 统一 Windows 反斜杠为正斜杠。
    - 拒绝绝对路径（以 "/" 开头或含 Windows 盘符段）。
    - 拒绝 ".." 段（zip-slip 防护）。
    - 跳过 macOS 元数据（__MACOSX/）、隐藏文件（任意段以 "." 开头）、空 basename。
    - 返回规范化的 forward-slash 相对路径；不合法则返回 None。
    """
    # 统一斜杠
    name = name.replace("\\", "/")

    # macOS 元数据目录
    if name.startswith("__MACOSX/"):
        return None

    p = PurePosixPath(name)

    # 拒绝绝对路径
    if p.is_absolute():
        return None

    parts = p.parts
    # 空路径
    if not parts:
        return None

    # 拒绝 ".." 段（zip-slip）
    if ".." in parts:
        return None

    # Windows 盘符段（如 "C:"）
    if any(len(part) == 2 and part[1] == ":" for part in parts):
        return None

    # 隐藏文件 / 隐藏目录：任意路径段以 "." 开头
    if any(part.startswith(".") for part in parts):
        return None

    # 空 basename
    basename = p.name
    if not basename:
        return None

    # 返回规范化路径字符串（str(PurePosixPath) 不会带前导 "/"）
    return str(p)


@router.post("/{dataset_id}/items/upload-zip")
async def upload_zip(
    dataset_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """前端 multipart 上传单个 ZIP 包，由后端解压并把每个文件入库 + 上传到 MinIO。

    限制：≤ 200MB 整包、≤ 5000 文件、单文件 ≤ 100MB；自动跳过 macOS 元数据（__MACOSX/）
    与隐藏文件（.DS_Store 等）；同名文件以路径 hash 后缀去重。
    """
    await recheck_directory_write(request, db, dataset_id)
    svc = DatasetService(db)
    ds = await svc.get(dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    # 同一 dataset 的并发 upload-zip 用事务级 advisory lock 串行化:否则两个请求会
    # 并行写 item + 各自跑 scene_inference,撞 SceneNameConflict(被吞成 notes 仍返回
    # 200)且产生半建状态。xact 锁在本请求 commit/rollback 时自动释放。
    from sqlalchemy import text

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": str(dataset_id)},
    )

    raw = await file.read()
    if len(raw) > _ZIP_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"ZIP 包超过 {_ZIP_MAX_BYTES // 1024 // 1024}MB 限制。"
                "浏览器向导只适合单个原生 scene；多 scene / nuScenes 等大包请使用转换脚本。"
                "格式说明见 docs-site/user-guide/datasets/import-formats.md"
            ),
        )

    try:
        archive = SafeZipArchive(
            io.BytesIO(raw),
            ArchiveLimits(
                max_files=_ZIP_MAX_ENTRIES,
                max_entry_bytes=_PER_FILE_MAX_BYTES,
                max_total_bytes=_ZIP_MAX_BYTES,
                max_compression_ratio=100,
            ),
            skip_unsafe_paths=True,
        )
    except ArchiveSafetyError as exc:
        status_code = (
            413
            if exc.code
            in {
                "resource_budget_exceeded",
                "archive_compression_ratio_exceeded",
            }
            else 400
        )
        raise HTTPException(
            status_code=status_code,
            detail={"reason": exc.code, "message": str(exc), **exc.detail},
        ) from exc

    infos = archive.entries

    added = 0
    deduped = 0
    skipped: list[str] = list(archive.skipped_paths)
    errors: list[dict] = []
    new_image_item_ids: list[uuid.UUID] = []
    new_video_item_ids: list[uuid.UUID] = []
    new_item_ids: list[uuid.UUID] = []
    zip_top_level_dirs: set[str] = set()

    from sqlalchemy import select as sa_select
    from app.db.models.dataset import DatasetItem

    from app.db.models.dataset_asset import DatasetAsset
    from app.services import managed_original_creation as creations
    from app.services.content_snapshot import ContentError

    # Only verified byte digests establish duplicates; legacy hashes may be ETags.
    hash_rows = await db.execute(
        sa_select(DatasetAsset.checksum_value)
        .join(DatasetItem, DatasetItem.id == DatasetAsset.dataset_item_id)
        .where(
            DatasetItem.dataset_id == dataset_id,
            DatasetAsset.verification_state == "verified",
        )
    )
    existing_hashes: set[str] = {r[0] for r in hash_rows.all()}
    ensure_dataset_storage_name_is_writable(ds.name)
    existing_names = await svc._existing_file_names(dataset_id)
    from app.services.dataset_assets import ensure_legacy_bindings

    binding = await ensure_legacy_bindings(db)

    async def check_authority():
        try:
            await recheck_directory_write(request, db, dataset_id)
        except HTTPException:
            archive.close()
            await db.rollback()
            raise

    for info in infos:
        name = info.source_name
        safe_relpath = info.normalized_path
        safe_parts = PurePosixPath(safe_relpath).parts
        if safe_relpath.startswith("__MACOSX/") or any(
            part.startswith(".") for part in safe_parts
        ):
            skipped.append(name)
            continue
        if len(safe_parts) >= 2:
            zip_top_level_dirs.add(safe_parts[0])

        try:
            with archive.open(safe_relpath) as source:
                data = source.read(_PER_FILE_MAX_BYTES + 1)
            if len(data) > _PER_FILE_MAX_BYTES:
                raise ArchiveSafetyError(
                    "resource_budget_exceeded",
                    "ZIP entry exceeded the streaming byte limit",
                )
        except Exception as e:  # noqa: BLE001
            errors.append({"name": name, "error": f"解压失败: {e}"})
            continue

        content_hash = hashlib.md5(data).hexdigest()
        sha256 = hashlib.sha256(data).hexdigest()
        from app.services.dataset_assets import find_verified_content

        if sha256 in existing_hashes or await find_verified_content(
            db, dataset_id, sha256, legacy_md5=content_hash
        ):
            deduped += 1
            continue

        basename = PurePosixPath(safe_relpath).name
        content_type = mimetypes.guess_type(basename)[0] or "application/octet-stream"
        file_type = _infer_file_type(content_type)
        logical_path = await svc._unique_file_name(
            dataset_id, safe_relpath, existing_names=existing_names
        )
        await check_authority()
        try:
            allocation = await creations.allocate(
                db,
                dataset_id=dataset_id,
                logical_path=logical_path,
                origin="zip",
                max_bytes=_PER_FILE_MAX_BYTES,
                requested_by=current_user.id,
                authority=getattr(request.state, "directory_dataset_authority", None),
                content_type=content_type,
            )
            created = await creations.create_stream(
                db, allocation, io.BytesIO(data), content_type=content_type
            )
        except ContentError as exc:
            errors.append({"name": name, "error": exc.code})
            continue
        storage_key = allocation["object_key"]

        width: int | None = None
        height: int | None = None
        if file_type == "image":
            dims = storage_service.read_image_dimensions_from_bytes(data)
            if dims:
                width, height = dims

        item = await svc.add_item(
            dataset_id=dataset_id,
            file_name=PurePosixPath(logical_path).name,
            file_path=storage_key,
            file_type=file_type,
            file_size=len(data),
            content_hash=content_hash,
            width=width,
            height=height,
            sha256=sha256,
            object_etag=created.etag,
            storage_binding=binding,
            logical_path=logical_path,
        )
        await creations.attach(db, item, created.creation_id)
        existing_names.add(logical_path)
        existing_hashes.add(sha256)
        added += 1
        new_item_ids.append(item.id)
        if file_type == "image":
            new_image_item_ids.append(item.id)
        elif file_type == "video":
            new_video_item_ids.append(item.id)

    archive.close()
    linked_tasks = await svc.create_tasks_for_items(dataset_id, new_item_ids)

    # v0.14.0 · 上传完跑 scene_inference(mode="auto"):
    # - SUSTechPOINTS 单 scene zip(顶层 lidar/ camera/ calib/)→ 1 scene
    # - 顶层若干非角色子目录(nuScenes 多 scene)→ N scene
    # - 纯 image / video 帧序列 → 1 scene + 自然排序 frame_index
    # 幂等:若 dataset 已有 scene → 整体跳过(下次上传不重赋)。
    scene_inference_notes: list[str] = []
    from app.services.dataset_source_access import has_directory_source

    directory_images = (
        ds.data_type == "image"
        and not ds.is_temporal
        and await has_directory_source(db, dataset_id)
    )
    if added > 0 and directory_images:
        scene_inference_notes.append("目录图片数据集保留原地引用，不自动创建 Scene。")
    if added > 0 and not directory_images:
        from app.services import scene_inference as _scene_inference

        reserved_top_levels = sorted(
            d for d in zip_top_level_dirs if _scene_inference._is_role_dir_name(d)
        )
        non_role_top_levels = sorted(
            d for d in zip_top_level_dirs if not _scene_inference._is_role_dir_name(d)
        )
        if reserved_top_levels and non_role_top_levels:
            scene_inference_notes.append(
                "ZIP 顶层同时包含保留角色目录 "
                f"({', '.join(reserved_top_levels)}) 与 scene 目录 "
                f"({', '.join(non_role_top_levels)}); "
                "多 scene 顶层目录不要使用 lidar/camera/calib/image/video 等角色名。"
                "格式说明见 docs-site/user-guide/datasets/import-formats.md"
            )
        try:
            inf = await _scene_inference.infer_and_apply(
                db, dataset_id=dataset_id, mode="auto"
            )
            scene_inference_notes.extend(inf.notes)
        except ValueError as exc:
            # 超过 scene 上限 / 其他可恢复错误:不阻断 upload,把 notes 透回前端
            scene_inference_notes.append(f"scene_inference skipped: {exc}")

    try:
        await svc.assert_temporal_dataset_has_scenes(dataset_id)
    except ValueError as exc:
        # The independent creation ledger survives a rolled back ZIP batch.
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await check_authority()
    await db.commit()

    if new_image_item_ids:
        from app.workers.media import generate_thumbnail

        for iid in new_image_item_ids:
            generate_thumbnail.delay(str(iid))
    if new_video_item_ids:
        from app.workers.media import generate_video_metadata

        for iid in new_video_item_ids:
            generate_video_metadata.delay(str(iid))

    return {
        "added": added,
        "deduped": deduped,
        "skipped": len(skipped),
        "errors": errors,
        "total_in_zip": len(infos),
        "linked_tasks": linked_tasks,
        "scene_inference_notes": scene_inference_notes,
    }


@router.post("/{dataset_id}/items/scan")
async def scan_items(
    dataset_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
):
    svc = DatasetService(db)
    ds = await svc.get(dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")
    outcomes = await svc.scan_and_import(
        dataset_id,
        directory_authority=getattr(request.state, "directory_dataset_authority", None),
    )
    new_ids = [
        outcome.item_id
        for outcome in outcomes
        if outcome.status == "added" and outcome.item_id is not None
    ]
    linked_tasks = sum(outcome.linked_tasks for outcome in outcomes)
    await recheck_directory_write(request, db, dataset_id)
    await db.commit()

    if new_ids:
        await svc.enqueue_media_for_items(new_ids)

    return {"status": "ok", "new_items": len(new_ids), "linked_tasks": linked_tasks}


@router.post("/{dataset_id}/backfill-dimensions")
async def backfill_dimensions(
    request: Request,
    dataset_id: uuid.UUID,
    batch: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
):
    """为存量 image 类型 dataset_items 回填 width/height。

    一次最多处理 batch 条（默认 50），多次调用直到 processed == 0 视为完成。
    """
    await recheck_directory_write(request, db, dataset_id)
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
    for item in items:
        dims = storage_service.read_image_dimensions(
            item.file_path,
            bucket=storage_service.datasets_bucket,
        )
        if dims:
            item.width, item.height = dims
            processed += 1
        else:
            failed += 1
    await db.commit()
    return {
        "processed": processed,
        "failed": failed,
        "remaining_hint": len(items) == batch,
    }


@router.post("/{dataset_id}/backfill-media")
async def backfill_media_endpoint(
    request: Request,
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
):
    """异步触发存量图像缩略图 / 视频元数据与 poster 回填。"""
    await recheck_directory_write(request, db, dataset_id)
    svc = DatasetService(db)
    ds = await svc.get(dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    from app.services.reference_access import reject_reference_scope

    await reject_reference_scope(db, dataset_id=dataset_id)

    from app.workers.media import backfill_media

    backfill_media.delay(str(dataset_id))
    return {"status": "queued", "dataset_id": str(dataset_id)}


@router.post("/{dataset_id}/scenes/backfill")
async def backfill_scenes_endpoint(
    request: Request,
    dataset_id: uuid.UUID,
    mode: str = Query(
        "auto",
        description="single / per_subdirectory / auto",
        pattern="^(single|per_subdirectory|auto)$",
    ),
    dry_run: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
):
    """v0.14.0 · 对 dataset 跑 scene_inference,补 scene + frame_index。

    幂等:dataset 已有 scene → 直接返回(notes 提示)。
    dry_run:不写库,返回会创建 / 赋值的统计。
    """
    await recheck_directory_write(request, db, dataset_id)
    from app.schemas.scene import InferenceResult
    from app.services.scene_inference import infer_and_apply

    svc = DatasetService(db)
    ds = await svc.get(dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    from app.services.reference_access import reject_reference_scope

    await reject_reference_scope(db, dataset_id=dataset_id)

    try:
        result: InferenceResult = await infer_and_apply(
            db,
            dataset_id=dataset_id,
            mode=mode,
            dry_run=dry_run,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not dry_run:
        await db.commit()
    return result


@router.delete("/{dataset_id}/items/{item_id}", status_code=204)
async def delete_item(
    request: Request,
    dataset_id: uuid.UUID,
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_MANAGERS)),
):
    await recheck_directory_write(request, db, dataset_id)
    svc = DatasetService(db)
    ok = await svc.delete_item(item_id, dataset_id=dataset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    await db.commit()
    await svc.cleanup_directory_previews()


# ── Project linking ─────────────────────────────────────────────────────────


@router.post("/{dataset_id}/link")
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
        db, user=current_user, project_id=data.project_id
    )
    if not access.is_manager:
        raise HTTPException(
            status_code=403, detail="仅项目负责人或超级管理员可关联数据集"
        )
    link_result = await svc.link_project(
        dataset_id,
        data.project_id,
        directory_authority=getattr(request.state, "directory_dataset_authority", None),
    )
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


@router.get("/{dataset_id}/link/{project_id}/preview-unlink")
async def preview_unlink_project(
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
    _project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """v0.6.7 B-10 v2：取消关联前的预览数字（will be deleted）。前端拿来做二次确认文案。
    v0.7.3：补 will_delete_batches —— 与 service 层一致：失去 task 后变空壳的 batch（B-DEFAULT 除外）。
    """
    from sqlalchemy import func
    from app.db.models.dataset import DatasetItem
    from app.db.models.task import Task
    from app.db.models.annotation import Annotation
    from app.db.models.task_batch import TaskBatch

    target_rows = (
        await db.execute(
            select(Task.id, Task.batch_id)
            .join(DatasetItem, DatasetItem.id == Task.dataset_item_id)
            .where(Task.project_id == project_id, DatasetItem.dataset_id == dataset_id)
        )
    ).all()
    task_ids = [r[0] for r in target_rows]
    affected_batch_ids = {r[1] for r in target_rows if r[1] is not None}
    task_count = len(task_ids)
    ann_count = 0
    if task_ids:
        ann_count = (
            await db.execute(
                select(func.count(Annotation.id)).where(
                    Annotation.task_id.in_(list(task_ids))
                )
            )
        ).scalar() or 0

    # 哪些受影响 batch 在删完 task 后会变空壳 → 与 unlink 真实行为一致
    will_delete_batches = 0
    if affected_batch_ids:
        loss_per_batch = dict(
            (
                await db.execute(
                    select(Task.batch_id, func.count())
                    .where(Task.id.in_(task_ids))
                    .group_by(Task.batch_id)
                )
            ).all()
        )
        for b in (
            (
                await db.execute(
                    select(TaskBatch).where(TaskBatch.id.in_(affected_batch_ids))
                )
            )
            .scalars()
            .all()
        ):
            if b.display_id == "B-DEFAULT":
                continue
            if (b.total_tasks or 0) - loss_per_batch.get(b.id, 0) <= 0:
                will_delete_batches += 1

    return {
        "will_delete_tasks": int(task_count),
        "will_delete_annotations": int(ann_count),
        "will_delete_batches": int(will_delete_batches),
    }


@router.delete("/{dataset_id}/link/{project_id}", status_code=200)
async def unlink_project(
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
    request: Request,
    _project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await recheck_directory_write(request, db, dataset_id)
    svc = DatasetService(db)
    info = await svc.unlink_project(dataset_id, project_id)
    if info is None:
        raise HTTPException(status_code=404, detail="Link not found")
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.DATASET_UNLINK,
        target_type="dataset",
        target_id=str(dataset_id),
        request=request,
        status_code=200,
        detail={
            "project_id": str(project_id),
            "deleted_tasks": info["deleted_tasks"],
            "deleted_annotations": info["deleted_annotations"],
            "deleted_batches": info.get("deleted_batches", 0),
            "deleted_batch_ids": info.get("deleted_batch_ids", []),
            "soft": False,
        },
    )
    await db.commit()
    return info


@router.get("/{dataset_id}/projects", response_model=list[ProjectOut])
async def get_linked_projects(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    from app.services.project_aggregates import project_scope_clause

    svc = DatasetService(db)
    projects = await svc.get_linked_projects(dataset_id)
    scope = project_scope_clause(actor, Project.id)
    if scope is None:
        return projects
    ids = set(
        await db.scalars(
            select(Project.id).where(Project.id.in_([p.id for p in projects]), scope)
        )
    )
    return [p for p in projects if p.id in ids]


def _infer_file_type(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "other"
