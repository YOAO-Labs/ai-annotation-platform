import asyncio
import hashlib
import io
import json
import time
import uuid

import httpx
from aap_protocol_v2 import (
    MAX_MASK_RESPONSE_BYTES,
    CocoRlePayload,
    canonical_rle_bytes,
)
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    assert_request_scopes,
    get_current_user,
    get_db,
    get_gpu_dispatch_context_factory,
    get_gpu_shadow_session_factory,
    project_access_for_path,
    require_roles,
    require_project_capability,
    require_project_visible,
    require_project_owner,
)
from app.db.enums import UserRole
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.dataset import DatasetItem
from app.db.models.user import User
from app.db.models.task import Task
from app.db.models.project import Project
from app.schemas.ml_backend import (
    MLBackendCreate,
    MLBackendUpdate,
    MLBackendOut,
    MLBackendHealthResponse,
    MLBackendUnloadResponse,
    MLBackendReloadRequest,
    InteractiveRequest,
    InteractiveAnnotateResponse,
    BackendCapabilities,
    ProjectMLBackendItem,
    ProjectMLBackendList,
    ProjectMLBackendEnablement,
    MLBackendPoolSummary,
    ProjectMLBackendPoolItem,
    ProjectMLBackendPoolList,
    ProjectMLBackendPoolEnablement,
)
from app.services.gpu_arbitration.contracts import (
    GPUArbiterDispatchError,
    GPUDispatchContextFactory,
    GPUShadowSessionFactory,
)
from app.services.gpu_arbitration.policy import GPUClaimConfigurationError
from app.services.ml_backend import (
    GPUBackendManagedMutationBlocked,
    MLBackendService,
    MLBackendURLConflict,
)
from app.services import ml_client as ml_client_module
from app.services.ml_capabilities import extract_capabilities
from app.services.ml_interaction_proxy import (
    normalize_native_mask_response,
    prepare_interactive_context,
)
from app.services.ml_routing.client import RoutedMLBackendClient
from app.services.prediction import PredictionService, to_video_bbox_result
from app.services.project_access import ProjectAccess, ProjectCapability
from app.services.storage import StorageService
from app.services.audit import AuditService
from app.services.ai_mask_receipt import issue_ai_mask_receipt
from app.services.ai_mask_prompt import resolve_authorized_mask_prompt
from app.services.ai_mask_session import issue_ai_mask_session
from app.api.v1.tasks._shared import (
    _assert_review_adjustment_evidence,
    _assert_task_visible,
    _resolve_task_access,
    assert_annotation_write_allowed,
)
from app.observability.metrics import (
    mask_ai_operation,
    mask_ai_prompt_family,
    observe_mask_ai_phase,
    record_mask_ai_operation,
)

# v0.10.1 · /setup 代理结果的进程内 TTL 缓存. 工作台进入即拉, 避免 N 次 backend 探活.
# key = backend_id (绑定改动 → 重绑后新 backend_id 自然 invalidate); 30s TTL 兜底.
_SETUP_CACHE_TTL_SECONDS = 30.0
_setup_cache: dict[uuid.UUID, tuple[float, dict]] = {}

router = APIRouter()


def _assert_ml_backend_read(access: ProjectAccess) -> None:
    """Backend read access mirrors the previous manager/reviewer/annotator set.

    A project viewer keeps the existing read-only project surfaces but not the
    ML backend catalog used by the Workbench.
    """

    if access.is_manager or access.project_role in {"annotator", "reviewer"}:
        return
    raise HTTPException(status_code=403, detail="缺少项目权限: task.read")


async def _assert_annotation_write_access(
    db: AsyncSession, task: Task, user: User
) -> ProjectAccess:
    """Annotation-phase AI access for a task's actual project.

    Reviewers are admitted only for a review adjustment with frozen non-self
    evidence; plain annotation-phase work denies reviewers.
    """

    access = await _resolve_task_access(db, task, user, lock_membership=True)
    assert_annotation_write_allowed(task, access)
    await _assert_review_adjustment_evidence(db, task, user, access)
    return access


async def _reacquire_write_boundary(
    db: AsyncSession, task_id: uuid.UUID, user: User
) -> tuple[Task, ProjectAccess]:
    """Re-acquire task and locked membership at the final write boundary.

    Long-running ML compute must not hold database locks.  Immediately before a
    restricted write or response we re-check the active account, re-resolve the
    current membership and lock the refreshed task row ``NOWAIT`` so a concurrent
    revocation or role change cannot slip a stale authority through and a busy
    resource cannot block the request indefinitely.  Account-first, then
    membership, then the bounded task lock.
    """

    from app.services.task_lock import assert_task_user_active

    if not await assert_task_user_active(db, user.id):
        raise HTTPException(
            status_code=401,
            detail="账号已停用，请联系管理员",
            headers={"WWW-Authenticate": "Bearer"},
        )
    task = await db.get(Task, task_id, populate_existing=True)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    access = await _resolve_task_access(db, task, user, lock_membership=True)
    task = await _lock_task_nowait(db, task_id)
    assert_annotation_write_allowed(task, access)
    await _assert_review_adjustment_evidence(db, task, user, access)
    return task, access


async def _lock_task_nowait(db: AsyncSession, task_id: uuid.UUID) -> Task:
    try:
        task = (
            await db.execute(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
            raise HTTPException(
                status_code=409, detail={"reason": "task_busy"}
            ) from exc
        raise
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# v0.23.5 · WS-D · D2 · cap on uploaded frame bytes (predict-frame /
# interactive-annotating-frame). Bounds memory regardless of a lying/missing
# Content-Length header; 32 MiB comfortably fits a 4K JPEG / PNG with headroom.
MAX_FRAME_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_INTERACTIVE_CONTEXT_BODY_BYTES = 1024 * 1024
# v0.23.5 · WS-D · D2 · streaming read stride (1 MiB) — small enough to abort
# between chunks when the cap is exceeded, large enough to avoid per-call
# overhead on legitimate multi-MiB frames.
_FRAME_READ_CHUNK = 1024 * 1024
# v0.23.5 · WS-D · D2 · image format / dimension cap mirroring ADR-0048 / 0052
# (dims ≤ 4096, pixels ≤ 16_777_216). Frames bigger than this can blow up the
# downstream inference memory budget and are rejected before hitting the backend.
_ALLOWED_FRAME_IMAGE_FORMATS = {"JPEG", "PNG"}
_MAX_FRAME_DIMENSION = 4096
_MAX_FRAME_PIXELS = _MAX_FRAME_DIMENSION * _MAX_FRAME_DIMENSION


async def _read_capped_frame(
    frame: UploadFile,
    *,
    request: Request | None = None,
    max_bytes: int = MAX_FRAME_UPLOAD_BYTES,
) -> bytes:
    """v0.23.5 · WS-D · D2 · read an uploaded frame under a hard byte + format cap.

    Two layers of defense against unbounded reads:

    1. If ``Content-Length`` is present on ``request`` (or the Starlette upload
       exposes ``frame.size``), reject early with 413 when it already exceeds
       ``max_bytes`` — before we buffer a single byte.
    2. Stream-read in ``_FRAME_READ_CHUNK`` increments and abort with 413 the
       moment the running total crosses ``max_bytes``. This catches clients that
       lie about (or omit) Content-Length.

    After collection the bytes are decoded with PIL: only ``{JPEG, PNG}`` are
    accepted and dimensions must respect the 4096 / 16M-pixel mask contract.
    Decode or dimension failures raise 400 (bad request), not 500.
    """
    # 1. Pre-flight on declared size, if available.
    declared: int | None = None
    size_attr = getattr(frame, "size", None)
    if isinstance(size_attr, int) and size_attr >= 0:
        declared = size_attr
    if (
        declared is None
        and request is not None
        and not request.headers.get("content-type", "").lower().startswith("multipart/")
    ):
        cl = request.headers.get("content-length")
        if cl:
            try:
                declared = int(cl)
            except (TypeError, ValueError):
                declared = None
    if declared is not None and declared > max_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "reason": "frame_too_large",
                "max_bytes": max_bytes,
                "declared_bytes": declared,
            },
        )

    # 2. Streaming accumulate (bypasses ``frame.read()`` which buffers fully).
    buf = bytearray()
    while True:
        chunk = await frame.read(_FRAME_READ_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail={
                    "reason": "frame_too_large",
                    "max_bytes": max_bytes,
                    "received_bytes": len(buf),
                },
            )
    data = bytes(buf)
    if not data:
        raise HTTPException(status_code=400, detail="Empty frame image")

    # 3. Decode + dimension/format validation via PIL.
    try:
        from PIL import Image  # noqa: PLC0415 — Pillow is a declared dep
    except ImportError as exc:  # pragma: no cover — Pillow is in pyproject
        raise HTTPException(
            status_code=500, detail="image validation unavailable"
        ) from exc
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            width, height = img.size
            img.verify()
        # verify() checks the stream structure but invalidates the decoder;
        # reopen and load pixels to reject truncated images with valid headers.
        with Image.open(io.BytesIO(data)) as img:
            img.load()
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"frame is not a decodable image: {exc}"
        ) from exc
    if fmt not in _ALLOWED_FRAME_IMAGE_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported frame image format: {fmt}",
        )
    if width > _MAX_FRAME_DIMENSION or height > _MAX_FRAME_DIMENSION:
        raise HTTPException(
            status_code=400,
            detail=(
                f"frame dimensions exceed {_MAX_FRAME_DIMENSION}px: {width}x{height}"
            ),
        )
    if width * height > _MAX_FRAME_PIXELS:
        raise HTTPException(
            status_code=400,
            detail=f"frame pixel count exceeds {_MAX_FRAME_PIXELS}",
        )
    return data


def _out(backend: MLBackendRegistry, project_id: uuid.UUID) -> MLBackendOut:
    """把全局注册行序列化为 MLBackendOut, 注入「本项目」id (表该项目启用了此 backend)。"""
    return MLBackendOut.model_validate(backend, from_attributes=True).model_copy(
        update={"project_id": project_id}
    )


def _resolve_task_url(task: Task) -> str:
    """v0.9.4 · 把 task.file_path (MinIO 对象 key) 转成 ML backend 可访问的 presigned URL。

    SAM backend 协议要求 file_path 是 http(s):// URL 或本地路径; tasks 表里存的是 key,
    必须先签发 presigned URL。当平台 api 跑在 host 进程而 ML backend 在 docker 网内时,
    再把 host 替换为 ``settings.ml_backend_storage_host`` (容器可达地址)。

    v0.23.0 · 实现已下沉到 :func:`app.services.storage.resolve_task_url`, 供 router、
    worker 与 video tracker runner 共用; 这里仅保留薄别名。
    """
    from app.services.storage import resolve_task_url

    return resolve_task_url(task)


@router.post(
    "",
    response_model=MLBackendOut,
    status_code=201,
    dependencies=[Depends(require_project_owner)],
)
async def create_ml_backend(
    project_id: uuid.UUID,
    data: MLBackendCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    # v0.19.0 ADR-0044 · 「为项目添加 backend」= 按 url 复用/新建全局注册项 + 为本项目启用。
    # 不再有 max_ml_backends_per_project 上限 (显存保护交全局行 extra_params.max_concurrency)。
    svc = MLBackendService(db)
    backend = await svc.get_by_url(data.url)
    if backend is None:
        backend = await svc.create_registry(
            name=data.name,
            url=data.url,
            source="manual",
            is_interactive=data.is_interactive,
            auth_method=data.auth_method,
            auth_token=data.auth_token,
            extra_params=data.extra_params,
        )
    await svc.set_enabled(project_id, backend.id, enabled=True)
    # B-5 · AI 审计 — ML backend 注册 + 项目启用
    await AuditService.log(
        db,
        actor=current_user,
        action="ml_backend.created",
        target_type="ml_backend",
        target_id=str(backend.id),
        request=request,
        status_code=201,
        detail={
            "project_id": str(project_id),
            "name": data.name,
            "url": data.url,
            "is_interactive": data.is_interactive,
        },
    )
    await db.commit()
    await db.refresh(backend)
    return _out(backend, project_id)


@router.get(
    "",
    response_model=list[MLBackendOut],
)
async def list_ml_backends(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    access: ProjectAccess = Depends(project_access_for_path),
):
    _assert_ml_backend_read(access)
    svc = MLBackendService(db)
    backends = await svc.list_enabled_for_project(project_id)
    return [_out(b, project_id) for b in backends]


@router.get(
    "/available",
    response_model=ProjectMLBackendList,
)
async def list_available_ml_backends(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _access: ProjectAccess = Depends(
        require_project_capability(ProjectCapability.PROJECT_MANAGE.value)
    ),
):
    """v0.19.0 ADR-0044 · 项目设置「启用勾选清单」: 列出全部全局 backend + 本项目启用态/覆盖。

    与 GET "" (只返回已启用) 区分: 本端点含未启用项, 供项目管理员勾选启用。
    路由声明在 `/{backend_id}` 之前, 避免被当作 backend_id 捕获。
    """
    svc = MLBackendService(db)
    rows = await svc.list_available_for_project(project_id)
    items = [
        ProjectMLBackendItem(
            backend=MLBackendOut.model_validate(reg, from_attributes=True),
            enabled=bool(assoc and assoc.enabled),
            default_variants=assoc.default_variants if assoc else None,
        )
        for reg, assoc in rows
    ]
    return ProjectMLBackendList(items=items)


@router.put(
    "/{backend_id}/enablement",
    response_model=ProjectMLBackendItem,
    dependencies=[Depends(require_project_owner)],
)
async def set_ml_backend_enablement(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    data: ProjectMLBackendEnablement,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """v0.19.0 ADR-0044 · 切换本项目对某全局 backend 的启用 + 写项目级覆盖 (阈值/变体)。

    全局 backend 必须存在 (不在此创建全局项; 注册全局项走 admin 端点)。
    """
    svc = MLBackendService(db)
    backend = await svc.get(backend_id)
    if backend is None:
        raise HTTPException(status_code=404, detail="ML Backend not found")
    overrides = {
        k: v for k, v in (("default_variants", data.default_variants),) if v is not None
    }
    assoc = await svc.set_enabled(
        project_id, backend_id, enabled=data.enabled, **overrides
    )
    _setup_cache.pop(backend_id, None)
    await AuditService.log(
        db,
        actor=current_user,
        action="ml_backend.enablement",
        target_type="ml_backend",
        target_id=str(backend_id),
        request=request,
        status_code=200,
        detail={
            "project_id": str(project_id),
            "enabled": data.enabled,
            "overrides": list(overrides.keys()),
        },
    )
    await db.commit()
    await db.refresh(backend)
    await db.refresh(assoc)
    return ProjectMLBackendItem(
        backend=MLBackendOut.model_validate(backend, from_attributes=True),
        enabled=assoc.enabled,
        default_variants=assoc.default_variants,
    )


@router.get(
    "/{backend_id}",
    response_model=MLBackendOut,
)
async def get_ml_backend(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    access: ProjectAccess = Depends(project_access_for_path),
):
    _assert_ml_backend_read(access)
    svc = MLBackendService(db)
    backend = await svc.get(backend_id)
    if not backend or not await svc.is_enabled(project_id, backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")
    return _out(backend, project_id)


@router.put(
    "/{backend_id}",
    response_model=MLBackendOut,
    dependencies=[Depends(require_project_owner)],
)
async def update_ml_backend(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    data: MLBackendUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    svc = MLBackendService(db)
    if not await svc.is_enabled(project_id, backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")
    _setup_cache.pop(backend_id, None)
    updates = data.model_dump(exclude_unset=True)
    # 更新作用于全局注册行 (auth/url/extra_params 等端点固有属性)。
    try:
        backend = await svc.update(backend_id, **updates)
    except GPUClaimConfigurationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "gpu_config_invalid",
                "message": str(exc),
                "diagnostics": [
                    diagnostic.model_dump(mode="json") for diagnostic in exc.diagnostics
                ],
            },
        ) from exc
    except MLBackendURLConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ml_backend_url_conflict",
                "message": f"该 URL 已注册为全局 backend ({exc.backend_name})",
            },
        ) from exc
    except GPUBackendManagedMutationBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "gpu_backend_retirement_required",
                "message": str(exc),
            },
        ) from exc
    if not backend:
        raise HTTPException(status_code=404, detail="ML Backend not found")
    await AuditService.log(
        db,
        actor=current_user,
        action="ml_backend.updated",
        target_type="ml_backend",
        target_id=str(backend_id),
        request=request,
        status_code=200,
        detail={"project_id": str(project_id), "fields": list(updates.keys())},
    )
    await db.commit()
    await db.refresh(backend)
    return _out(backend, project_id)


@router.delete(
    "/{backend_id}",
    status_code=204,
    dependencies=[Depends(require_project_owner)],
)
async def delete_ml_backend(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # v0.19.0 ADR-0044 · 项目作用域的「删除」= 为本项目停用全局 backend (不删全局注册项;
    # 删全局项是 superadmin 的 admin 端点, 见 PR3)。
    svc = MLBackendService(db)
    if not await svc.is_enabled(project_id, backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")
    _setup_cache.pop(backend_id, None)
    await svc.set_enabled(project_id, backend_id, enabled=False)
    await AuditService.log(
        db,
        actor=current_user,
        action="ml_backend.deleted",
        target_type="ml_backend",
        target_id=str(backend_id),
        request=request,
        status_code=204,
        detail={"project_id": str(project_id), "op": "disable"},
    )
    await db.commit()


@router.post("/{backend_id}/unload", response_model=MLBackendUnloadResponse)
async def unload_ml_backend(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    shadow_session_factory: GPUShadowSessionFactory = Depends(
        get_gpu_shadow_session_factory
    ),
    dispatch_context_factory: GPUDispatchContextFactory = Depends(
        get_gpu_dispatch_context_factory
    ),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
) -> MLBackendUnloadResponse:
    """B-28+ · 触发 backend 卸载模型释放显存. backend 未实现 /unload 时返回 502.

    鉴权 super_admin only · /unload 作用于「全局 backend 显存驻留」(一物理 backend 被多个
    项目共用), 项目 owner 也能借此驱逐其他项目正在用的权重。故这类破坏性的驻留操作收口到
    平台管理员, 与前端「运行时观测」面板 (super_admin only) 及 admin observe/smoke-test 的
    运维基线一致; 不叠加 require_project_owner —— 全局操作按 backend_id 定位, 与 path 里的
    project 无归属关系。构造性的 /warmup 仍保留在 project_owner (见该端点注释)。"""
    from app.services.ml_routing.safety import (
        MLBackendQuiescenceError,
        require_registry_quiescent,
    )

    svc = MLBackendService(
        db,
        shadow_session_factory=shadow_session_factory,
        dispatch_context_factory=dispatch_context_factory,
    )
    try:
        await require_registry_quiescent(db, backend_id)
        result = await svc.unload(backend_id)
    except MLBackendQuiescenceError as exc:
        raise HTTPException(
            status_code=503 if exc.unavailable else 409, detail=exc.as_detail()
        ) from exc
    except GPUArbiterDispatchError:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"backend unload failed: {exc}")
    if result is None:
        raise HTTPException(status_code=404, detail="ML Backend not found")
    await AuditService.log(
        db,
        actor=current_user,
        action="ml_backend.unloaded",
        target_type="ml_backend",
        target_id=str(backend_id),
        request=request,
        status_code=200,
        detail={"project_id": str(project_id), "result": result},
    )
    await db.commit()
    return MLBackendUnloadResponse.model_validate(result)


@router.post("/{backend_id}/reload")
async def reload_ml_backend(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    request: Request,
    body: MLBackendReloadRequest | None = None,
    db: AsyncSession = Depends(get_db),
    shadow_session_factory: GPUShadowSessionFactory = Depends(
        get_gpu_shadow_session_factory
    ),
    dispatch_context_factory: GPUDispatchContextFactory = Depends(
        get_gpu_dispatch_context_factory
    ),
    current_user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    """B-28+ · 触发 backend 重新加载模型. 已加载则 noop.

    v0.10.26 · 可选 body {sam_variant, dino_variant} 预热指定变体 (模型市场单变体预热);
    缺省回退 backend 默认变体.

    鉴权 super_admin only · /reload 会改写「全局 backend 常驻变体」(同 backend 被多项目共用),
    切变体等于换掉其他项目正在用的权重, 属破坏性驻留操作, 与 /unload 同基线收口到平台管理员
    (对齐 super_admin only 的「运行时观测」面板)。"""
    svc = MLBackendService(
        db,
        shadow_session_factory=shadow_session_factory,
        dispatch_context_factory=dispatch_context_factory,
    )
    sam_variant = body.sam_variant if body else None
    dino_variant = body.dino_variant if body else None
    task_type = body.task_type if body else None
    try:
        result = await svc.reload(
            backend_id,
            sam_variant=sam_variant,
            dino_variant=dino_variant,
            task_type=task_type,
        )
    except GPUArbiterDispatchError:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"backend reload failed: {exc}")
    if result is None:
        raise HTTPException(status_code=404, detail="ML Backend not found")
    await AuditService.log(
        db,
        actor=current_user,
        action="ml_backend.reloaded",
        target_type="ml_backend",
        target_id=str(backend_id),
        request=request,
        status_code=200,
        detail={
            "project_id": str(project_id),
            "sam_variant": sam_variant,
            "dino_variant": dino_variant,
            "result": result,
        },
    )
    await db.commit()
    return result


@router.post(
    "/{backend_id}/warmup",
    dependencies=[Depends(require_project_owner)],
)
async def warmup_ml_backend(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    request: Request,
    body: dict | None = None,
    db: AsyncSession = Depends(get_db),
    shadow_session_factory: GPUShadowSessionFactory = Depends(
        get_gpu_shadow_session_factory
    ),
    dispatch_context_factory: GPUDispatchContextFactory = Depends(
        get_gpu_dispatch_context_factory
    ),
    current_user: User = Depends(get_current_user),
):
    """v0.14.14 协议 §4.4 · 转发 POST /warmup 到 backend.

    body 原样转发 (各 backend schema 不同). backend 未声明 warmup_endpoint=True 时仍
    转发, 收到 404/405 由上游 502 反馈; 前端 ⚡ 按钮应已通过 health_meta.capabilities.
    warmup_endpoint 提前置灰.

    鉴权 project_owner (刻意不随 /unload·/reload 收口到 super_admin) · warmup 是「构造性」
    预热, 是项目自身预标/交互推理的前置: AIPreAnnotate 预热加载类别表 (model.names)、能力目录
    对项目管理员可见的预热按钮, 都是项目负责人的日常流程。共享态权衡: 预热可能驱逐同 backend
    上其他项目常驻的模型, 属可接受代价 —— 它是本项目用模型的必要前提, 且 backend 侧有
    max_concurrency / idle 淘汰兜底。只有会直接驱逐/换掉他人在用权重的 unload/reload 才收口
    到平台管理员。"""
    svc = MLBackendService(
        db,
        shadow_session_factory=shadow_session_factory,
        dispatch_context_factory=dispatch_context_factory,
    )
    try:
        result = await svc.warmup(backend_id, body or {})
    except GPUArbiterDispatchError:
        raise
    except httpx.HTTPStatusError as exc:
        # 透传 backend 上游的业务错误 (4xx 变体非法 / 5xx OOM / weight missing).
        status = exc.response.status_code
        detail = exc.response.text or f"backend warmup HTTP {status}"
        headers = {}
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after:
            headers["Retry-After"] = retry_after
        raise HTTPException(
            status_code=status,
            detail=detail,
            headers=headers or None,
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"backend warmup failed: {exc}"
        ) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="ML Backend not found")
    # v0.14.17 · 预热会改变 backend 已加载的模型, 从而改变 /setup.models[].classes (yolo 加载后
    # 才暴露 model.names). 失效 /setup 缓存, 让随后的 /capabilities 立即拿到新类别表 (否则最坏等 30s TTL)。
    _setup_cache.pop(backend_id, None)
    await AuditService.log(
        db,
        actor=current_user,
        action="ml_backend.warmup",
        target_type="ml_backend",
        target_id=str(backend_id),
        request=request,
        status_code=200,
        detail={"project_id": str(project_id), "body": body, "result": result},
    )
    await db.commit()
    return result


async def _fetch_setup_cached(backend, backend_id: uuid.UUID) -> dict:
    """v0.10.1 · 探 backend /setup, 命中 30s TTL 进程内缓存则直接返回.

    v0.14.9 · 抽出供 /setup 与 /capabilities 端点共用同一缓存链路; backend /setup
    不可达时抛 502 (与原 /setup 端点行为一致).
    """
    now = time.monotonic()
    cached = _setup_cache.get(backend_id)
    if cached is not None and (now - cached[0]) < _SETUP_CACHE_TTL_SECONDS:
        return cached[1]

    client = ml_client_module.MLBackendClient(backend)
    try:
        data = await client.setup()
    except Exception as exc:  # httpx.HTTPError 或 timeout
        raise HTTPException(
            status_code=502, detail=f"backend /setup unreachable: {exc}"
        )
    _setup_cache[backend_id] = (now, data)
    return data


async def _prepare_safe_interactive_context(
    backend: MLBackendRegistry,
    backend_id: uuid.UUID,
    context: dict,
    *,
    task_id: uuid.UUID,
    frame_index: int | None = None,
) -> tuple[dict, str | None]:
    uses_mask_contract = any(
        key in context
        for key in (
            "mask_prompt",
            "mask_input",
            "scribbles",
            "output_geometry",
            "model_id",
        )
    ) or context.get("type") in {"point", "interactive_box", "mask", "scribble"}
    if not uses_mask_contract:
        return dict(context), None
    setup = await _fetch_setup_cached(backend, backend_id)
    capabilities = extract_capabilities(setup)
    return prepare_interactive_context(
        context,
        capabilities,
        task_id=str(task_id),
        frame_index=frame_index,
        requested_backend_id=str(backend_id),
    )


async def _task_media_size(
    db: AsyncSession,
    task: Task,
) -> tuple[int, int] | None:
    if task.dataset_item_id is None:
        return None
    item = await db.get(DatasetItem, task.dataset_item_id)
    if (
        item is None
        or not isinstance(item.width, int)
        or not isinstance(item.height, int)
        or item.width <= 0
        or item.height <= 0
    ):
        return None
    return item.width, item.height


def _decoded_image_size(data: bytes) -> tuple[int, int]:
    from PIL import Image  # noqa: PLC0415

    with Image.open(io.BytesIO(data)) as image:
        return image.size


def _interactive_response(
    result,
    *,
    context: dict,
    expected_size: tuple[int, int] | None,
    client: RoutedMLBackendClient,
    requested_backend_id: uuid.UUID,
    model_id: str | None,
    task_id: uuid.UUID,
    frame_index: int | None,
) -> dict:
    operation = mask_ai_operation(context)
    decode_started = time.monotonic()
    try:
        normalized, diagnostic = normalize_native_mask_response(
            result.result,
            getattr(result, "diagnostic", None),
            context=context,
            expected_size=expected_size,
        )
    except Exception:
        observe_mask_ai_phase(
            operation=operation,
            phase="decode",
            outcome="error",
            duration_seconds=time.monotonic() - decode_started,
        )
        raise
    observe_mask_ai_phase(
        operation=operation,
        phase="decode",
        outcome="success",
        duration_seconds=time.monotonic() - decode_started,
    )
    routing = {
        "requested_backend_id": str(requested_backend_id),
        "backend_pool_id": str(client.pool_id) if client.pool_id else None,
        "backend_instance_id": (
            str(client.last_instance_id) if client.last_instance_id else None
        ),
        "model_id": model_id,
    }
    accept_receipts: dict[str, str] = {}
    prompt_revision = context.get("prompt_revision")
    prompt_summary = _interactive_prompt_summary(context)
    prompt_source = _interactive_prompt_source(context)
    accept_target = {
        "mode": "refine" if prompt_source is not None else "create",
        "source_annotation_id": (
            prompt_source.get("source_annotation_id") if prompt_source else None
        ),
        "source_version": prompt_source.get("source_version")
        if prompt_source
        else None,
        "frame_index": frame_index,
    }
    if context.get("output_geometry") == "mask" and isinstance(prompt_revision, str):
        for candidate_index, candidate in enumerate(normalized):
            if candidate["type"] != "mask":
                continue
            rle = CocoRlePayload.model_validate(candidate["value"]["rle"])
            content_digest = hashlib.sha256(canonical_rle_bytes(rle)).hexdigest()
            candidate_id = str(candidate["candidate_id"])
            accept_receipts[candidate_id] = issue_ai_mask_receipt(
                {
                    "task_id": str(task_id),
                    "frame_index": frame_index,
                    "candidate_id": candidate_id,
                    "candidate_index": candidate_index,
                    "content_digest": content_digest,
                    "prompt_revision": prompt_revision,
                    "score": candidate.get("score"),
                    "routing": routing,
                    "inference": {
                        "model_version": getattr(result, "model_version", None),
                        "inference_time_ms": result.inference_time_ms,
                        "cache_hit": result.cache_hit,
                        "model_load_ms": result.model_load_ms,
                    },
                    "prompt_summary": prompt_summary,
                    "prompt_source": prompt_source,
                    "accept_target": accept_target,
                }
            )
    mask_input_next = None
    raw_mask_input = getattr(result, "mask_input_next", None)
    if isinstance(raw_mask_input, str) and len(normalized) == 1:
        mask_prompt = context.get("mask_prompt")
        source = (
            {
                "source_annotation_id": mask_prompt.get("source_annotation_id"),
                "source_version": mask_prompt.get("source_version"),
                "source_digest": mask_prompt.get("source_digest"),
            }
            if isinstance(mask_prompt, dict)
            else None
        )
        try:
            mask_input_next = issue_ai_mask_session(
                raw_mask_input,
                {
                    "task_id": str(task_id),
                    "frame_index": frame_index,
                    "requested_backend_id": str(requested_backend_id),
                    "model_id": model_id,
                    "model_variants": context.get("model_variants") or {},
                    "source": source,
                    "origin_prompt_revision": prompt_revision,
                    "candidate_id": normalized[0].get("candidate_id"),
                    "candidate_index": 0,
                },
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "reason": "invalid_mask_session_payload",
                    "message": "backend returned invalid Mask session logits",
                },
            ) from exc
    return {
        "result": normalized,
        "score": result.score,
        "model_version": getattr(result, "model_version", None),
        "inference_time_ms": result.inference_time_ms,
        "cache_hit": result.cache_hit,
        "model_load_ms": result.model_load_ms,
        "mask_input_next": mask_input_next,
        "diagnostic": diagnostic,
        "prompt_revision": prompt_revision,
        "output_geometry": context.get("output_geometry", "polygon"),
        "frame_index": frame_index,
        "routing": routing,
        "prompt_summary": prompt_summary,
        "accept_receipts": accept_receipts,
    }


def _interactive_prompt_source(context: dict) -> dict | None:
    mask_prompt = context.get("mask_prompt")
    if not isinstance(mask_prompt, dict):
        return None
    return {
        "source_annotation_id": mask_prompt.get("source_annotation_id"),
        "source_version": mask_prompt.get("source_version"),
        "source_digest": mask_prompt.get("source_digest"),
    }


def _interactive_prompt_summary(context: dict) -> dict:
    prompt_type = str(context.get("type") or "point")
    labels = context.get("labels") if isinstance(context.get("labels"), list) else []
    exemplars = (
        context.get("exemplars") if isinstance(context.get("exemplars"), list) else []
    )
    scribbles = (
        context.get("scribbles") if isinstance(context.get("scribbles"), list) else []
    )
    excluded = {
        "type",
        "points",
        "labels",
        "bbox",
        "exemplars",
        "scribbles",
        "mask_prompt",
        "mask_input",
        "prompt_revision",
        "output_geometry",
        "model_id",
    }
    parameters = {key: value for key, value in context.items() if key not in excluded}
    parameters_digest = (
        hashlib.sha256(
            json.dumps(
                parameters,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if parameters
        else None
    )
    return {
        "family": prompt_type,
        "positive_points": sum(label == 1 for label in labels),
        "negative_points": sum(label == 0 for label in labels),
        "boxes": 1 if prompt_type == "interactive_box" else len(exemplars),
        "positive_scribbles": sum(
            isinstance(stroke, dict) and stroke.get("polarity") == 1
            for stroke in scribbles
        ),
        "negative_scribbles": sum(
            isinstance(stroke, dict) and stroke.get("polarity") == 0
            for stroke in scribbles
        ),
        "multimask": context.get("multimask_output") is True,
        "parameters_digest": parameters_digest,
    }


@router.get(
    "/{backend_id}/setup",
)
async def get_ml_backend_setup(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    access: ProjectAccess = Depends(project_access_for_path),
):
    """v0.10.1 · 代理 backend /setup, 返回 JSON Schema 自描述能力 (供前端 useMLCapabilities).

    30s TTL 进程内缓存; backend 升级/重启后最坏延迟 30s. 删除/更新 backend 时 invalidate.
    """
    _assert_ml_backend_read(access)
    svc = MLBackendService(db)
    backend = await svc.get(backend_id)
    if not backend or not await svc.is_enabled(project_id, backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")

    return await _fetch_setup_cached(backend, backend_id)


@router.get(
    "/{backend_id}/capabilities",
    response_model=BackendCapabilities,
)
async def get_ml_backend_capabilities(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    access: ProjectAccess = Depends(project_access_for_path),
):
    """v0.14.9 · 能力声明协议 v2: 探 /setup (复用 setup 缓存链路) → 派生能力快照.

    返回含 models[] / infra / modalities + 扁平并集字段, 供前端多模型目录消费.
    权限同 /setup 端点 (managers + reviewer + annotator)。/setup 不可达时 502.
    """
    _assert_ml_backend_read(access)
    svc = MLBackendService(db)
    backend = await svc.get(backend_id)
    if not backend or not await svc.is_enabled(project_id, backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")

    setup = await _fetch_setup_cached(backend, backend_id)
    return extract_capabilities(setup) or {}


@router.post(
    "/{backend_id}/capabilities/refresh",
    response_model=BackendCapabilities,
    dependencies=[Depends(require_project_owner)],
)
async def refresh_ml_backend_capabilities(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """v0.14.9 · 强制刷新能力快照: 先 invalidate setup 缓存再重探 + 派生.

    权限同 /health (managers); 用于 backend 升级/换模型后主动拉新能力。
    """
    svc = MLBackendService(db)
    backend = await svc.get(backend_id)
    if not backend or not await svc.is_enabled(project_id, backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")

    _setup_cache.pop(backend_id, None)
    setup = await _fetch_setup_cached(backend, backend_id)
    return extract_capabilities(setup) or {}


@router.post(
    "/{backend_id}/health",
    response_model=MLBackendHealthResponse,
    dependencies=[Depends(require_project_owner)],
)
async def check_health(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = MLBackendService(db)
    backend = await svc.get(backend_id)
    if not backend or not await svc.is_enabled(project_id, backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")
    healthy = await svc.check_health(backend_id)
    await db.commit()
    return MLBackendHealthResponse(
        status="ok" if healthy else "error",
        backend_id=backend.id,
        backend_name=backend.name,
    )


async def _get_task_in_project(
    db: AsyncSession, task_id: uuid.UUID, project_id: uuid.UUID
) -> Task:
    """取 task 并校验归属本项目 — 防跨项目越权 (只用主键取任务会让 A 项目成员用 A 的
    backend + B 项目的 task_id 完成推理)。不存在 / 不属于本项目都回 404, 不暴露存在性。"""
    task = await db.get(Task, task_id)
    if task is None or task.project_id != project_id:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


async def _require_ai_interactive_enabled(
    db: AsyncSession, project_id: uuid.UUID
) -> None:
    """项目级「交互式 AI 工具」总开关的后端守卫。语义与前端 (`?? true`) 一致 = 默认开:
    仅当显式关闭 (ai_interactive_enabled is False) 才 403, None/True 放行。"""
    project = await db.get(Project, project_id)
    if project is not None and project.ai_interactive_enabled is False:
        raise HTTPException(
            status_code=403, detail="AI interactive is disabled for this project"
        )


@router.post(
    "/{backend_id}/predict-test",
    dependencies=[Depends(require_project_owner)],
)
async def predict_test(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    shadow_session_factory: GPUShadowSessionFactory = Depends(
        get_gpu_shadow_session_factory
    ),
    dispatch_context_factory: GPUDispatchContextFactory = Depends(
        get_gpu_dispatch_context_factory
    ),
    current_user: User = Depends(get_current_user),
):
    svc = MLBackendService(db)
    backend = await svc.get(backend_id)
    if not backend or not await svc.is_enabled(project_id, backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")

    task = await _get_task_in_project(db, task_id, project_id)
    await db.commit()

    client = RoutedMLBackendClient(
        db,
        backend,
        project_id=project_id,
        owner=f"api:predict-test:{task.id}",
        operation="predict_test",
        shadow_session_factory=shadow_session_factory,
        dispatch_context_factory=dispatch_context_factory,
    )
    results = await client.predict(
        [{"id": str(task.id), "file_path": _resolve_task_url(task)}]
    )
    return {
        "results": [
            {"task_id": r.task_id, "result": r.result, "score": r.score}
            for r in results
        ]
    }


@router.post(
    "/{backend_id}/interactive-annotating",
    response_model=InteractiveAnnotateResponse,
    dependencies=[Depends(require_project_visible)],
)
async def interactive_annotating(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    body: InteractiveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    shadow_session_factory: GPUShadowSessionFactory = Depends(
        get_gpu_shadow_session_factory
    ),
    dispatch_context_factory: GPUDispatchContextFactory = Depends(
        get_gpu_dispatch_context_factory
    ),
    current_user: User = Depends(get_current_user),
):
    svc = MLBackendService(db)
    backend = await svc.get(backend_id)
    if not backend or not await svc.is_enabled(project_id, backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")
    if not backend.is_interactive:
        raise HTTPException(
            status_code=400,
            detail="This backend does not support interactive annotation",
        )

    await _require_ai_interactive_enabled(db, project_id)
    task = await _get_task_in_project(db, body.task_id, project_id)
    access = await _assert_annotation_write_access(db, task, current_user)
    await _assert_task_visible(db, task, current_user, access=access)
    if (body.context or {}).get("mask_prompt_source") is not None:
        assert_request_scopes(request, "annotations:read")

    # AI 推理参数 (阈值 / 变体等) 已统一改走工作台 AI 面板: 前端按所绑定 backend 的
    # /setup.params 动态渲染、每用户独立调整, 并随 context 透传。平台不再注入项目级 DINO
    # 阈值 (那会把 gsam2 专属参数塞给 sam3 等不支持的后端); 各 backend 缺省值由自身 /setup 决定。
    resolved_context = await resolve_authorized_mask_prompt(
        db,
        task=task,
        context=dict(body.context or {}),
        frame_index=None,
    )
    context, model_id = await _prepare_safe_interactive_context(
        backend,
        backend_id,
        resolved_context,
        task_id=task.id,
        frame_index=None,
    )
    expected_size = (
        await _task_media_size(db, task)
        if context.get("output_geometry") == "mask"
        else None
    )
    await db.commit()

    client = RoutedMLBackendClient(
        db,
        backend,
        project_id=project_id,
        owner=f"api:interactive:{task.id}",
        operation="interactive_predict",
        shadow_session_factory=shadow_session_factory,
        dispatch_context_factory=dispatch_context_factory,
    )
    predict_kwargs = (
        {"max_response_bytes": MAX_MASK_RESPONSE_BYTES}
        if context.get("output_geometry") == "mask"
        else {}
    )
    metric_operation = mask_ai_operation(context)
    metric_prompt = mask_ai_prompt_family(context)
    metric_geometry = str(context.get("output_geometry") or "unknown")
    inference_started = time.monotonic()
    try:
        result = await client.predict_interactive(
            task_data={"id": str(task.id), "file_path": _resolve_task_url(task)},
            context=context,
            **predict_kwargs,
        )
    except Exception:
        observe_mask_ai_phase(
            operation=metric_operation,
            phase="inference",
            outcome="error",
            duration_seconds=time.monotonic() - inference_started,
        )
        record_mask_ai_operation(
            operation=metric_operation,
            prompt_family=metric_prompt,
            output_geometry=metric_geometry,
            outcome="error",
        )
        raise
    observe_mask_ai_phase(
        operation=metric_operation,
        phase="inference",
        outcome="success",
        duration_seconds=time.monotonic() - inference_started,
    )
    # Do not return restricted candidates from a revoked/role-changed authority:
    # re-acquire the task row and locked membership at the response boundary.
    await _reacquire_write_boundary(db, task.id, current_user)
    encode_started = time.monotonic()
    try:
        response_body = _interactive_response(
            result,
            context=context,
            expected_size=expected_size,
            client=client,
            requested_backend_id=backend_id,
            model_id=model_id,
            task_id=task.id,
            frame_index=None,
        )
    except Exception:
        observe_mask_ai_phase(
            operation=metric_operation,
            phase="encode",
            outcome="error",
            duration_seconds=time.monotonic() - encode_started,
        )
        record_mask_ai_operation(
            operation=metric_operation,
            prompt_family=metric_prompt,
            output_geometry=metric_geometry,
            outcome="error",
        )
        raise
    observe_mask_ai_phase(
        operation=metric_operation,
        phase="encode",
        outcome="success",
        duration_seconds=time.monotonic() - encode_started,
    )
    record_mask_ai_operation(
        operation=metric_operation,
        prompt_family=metric_prompt,
        output_geometry=metric_geometry,
        candidate_count=len(response_body["result"]),
        outcome="success",
    )
    return response_body


@router.post(
    "/{backend_id}/predict-frame",
    dependencies=[Depends(require_project_visible)],
)
async def predict_frame(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    request: Request,
    frame: UploadFile = File(...),
    task_id: uuid.UUID = Form(...),
    frame_index: int = Form(...),
    config: str = Form("{}"),
    db: AsyncSession = Depends(get_db),
    shadow_session_factory: GPUShadowSessionFactory = Depends(
        get_gpu_shadow_session_factory
    ),
    dispatch_context_factory: GPUDispatchContextFactory = Depends(
        get_gpu_dispatch_context_factory
    ),
    current_user: User = Depends(get_current_user),
):
    """v0.21.4 · 对客户端传入的「视频当前帧」JPEG 跑图像 backend, 落单帧 ``video_bbox`` 候选。

    视频 task 的 ``file_path`` 是整段 mp4, 图像 backend 从 task URL 取不到帧 (见
    ``_resolve_task_url``)。故前端把当前帧解成 JPEG 随 multipart 传入; 服务端上传 import 桶换
    presigned URL 投递 (``upload_crop_bytes``, **通用**——gsam2/sam3 全支持 http URL, 不走
    ``data:`` 捷径); 结果逐框改写成 ``video_bbox(frame_index)`` 落一条 Prediction, 采纳复用既有
    ``/predictions/{id}/accept`` 机制 → ``VideoBboxGeometry``。

    与批量预标 (整段视频投 signed URL 走 worker) 是两条路: 本路同步、单帧、client 供图。
    """
    from app.workers.tasks import _build_predict_context

    svc = MLBackendService(db)
    backend = await svc.get(backend_id)
    if not backend or not await svc.is_enabled(project_id, backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")

    await _require_ai_interactive_enabled(db, project_id)
    task = await _get_task_in_project(db, task_id, project_id)
    access = await _assert_annotation_write_access(db, task, current_user)
    await _assert_task_visible(db, task, current_user, access=access)

    try:
        cfg = json.loads(config) if config else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid config JSON")
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=400, detail="config must be a JSON object")

    # DINO 阈值取项目级 override (与 worker 单阶段路径一致, 见 workers/tasks.py `_run_batch`)。
    project = await db.get(Project, project_id)
    context = _build_predict_context(
        prompt=cfg.get("prompt"),
        # 单帧只要框: 文本 backend (gsam2) 输出 box; mask/polygon 非 video_bbox 会被丢弃。
        output_mode=cfg.get("output_mode") or "box",
        params=cfg.get("params"),
        model_id=cfg.get("model_id"),
        task_type=cfg.get("task_type"),
        model_variants=cfg.get("model_variants"),
        class_filter=cfg.get("class_filter"),
        box_threshold=(
            float(project.box_threshold)
            if project is not None and project.box_threshold is not None
            else None
        ),
        text_threshold=(
            float(project.text_threshold)
            if project is not None and project.text_threshold is not None
            else None
        ),
    )

    # v0.23.5 · WS-D · D2 · bounded frame read (Content-Length + streaming cap
    # + JPEG/PNG decode + dim/pixel limits) before any storage I/O.
    jpeg_bytes = await _read_capped_frame(frame, request=request)

    storage = StorageService()
    # v0.23.5 · WS-D · D5 · boto3 sync put_object wrapped in to_thread.
    frame_url = await asyncio.to_thread(
        storage.upload_crop_bytes,
        jpeg_bytes,
        f"frame-predict/{task_id}/{frame_index}.jpg",
    )
    await db.commit()

    client = RoutedMLBackendClient(
        db,
        backend,
        project_id=project_id,
        owner=f"api:predict-frame:{task.id}:{frame_index}",
        operation="frame_predict",
        shadow_session_factory=shadow_session_factory,
        dispatch_context_factory=dispatch_context_factory,
    )
    results = await client.predict(
        [{"id": str(task.id), "file_path": frame_url}], context=context
    )

    raw_shapes: list[dict] = []
    for r in results:
        if isinstance(r.result, list):
            raw_shapes.extend(r.result)
    video_shapes = to_video_bbox_result(raw_shapes, frame_index)

    score = next((r.score for r in results if r.score is not None), None)
    # Final mutation boundary: re-acquire the task row and locked membership so a
    # revocation or role change during inference cannot persist a prediction.
    await _reacquire_write_boundary(db, task.id, current_user)
    pred_svc = PredictionService(db)
    prediction = await pred_svc.create_from_ml_result(
        task_id=task.id,
        project_id=project_id,
        ml_backend_id=client.last_instance_id or backend_id,
        result=video_shapes,
        score=score,
        ml_backend_pool_id=client.pool_id,
    )
    await db.commit()
    return {
        "prediction_id": str(prediction.id),
        "candidate_count": len(video_shapes),
        "frame_index": frame_index,
    }


@router.post(
    "/{backend_id}/interactive-annotating-frame",
    response_model=InteractiveAnnotateResponse,
    dependencies=[Depends(require_project_visible)],
)
async def interactive_annotating_frame(
    project_id: uuid.UUID,
    backend_id: uuid.UUID,
    request: Request,
    frame: UploadFile = File(...),
    task_id: uuid.UUID = Form(...),
    frame_index: int = Form(...),
    context: str = Form("{}", max_length=MAX_INTERACTIVE_CONTEXT_BODY_BYTES),
    db: AsyncSession = Depends(get_db),
    shadow_session_factory: GPUShadowSessionFactory = Depends(
        get_gpu_shadow_session_factory
    ),
    dispatch_context_factory: GPUDispatchContextFactory = Depends(
        get_gpu_dispatch_context_factory
    ),
    current_user: User = Depends(get_current_user),
):
    """视频当前帧的**交互式** SAM 提示 (point / interactive_box / exemplar)。

    与 ``interactive-annotating`` 同一个 backend 契约（兼容 ``context`` 透传，显式
    原生 Mask 请求会经过能力预检与响应校验，见 ``InteractiveRequest.context``），
    唯一差别是**图从哪来**: 图片 task 由服务端
    ``_resolve_task_url(task)`` 取, 而视频 task 的 ``file_path`` 是整段 mp4, SAM 吃不到帧。
    故前端把当前帧解成 JPEG 随 multipart 传入 (复用 ``predict-frame`` 的 client 供图机制:
    上传 import 桶换 presigned URL, 通用 http URL, 不走 ``data:`` 捷径)。

    与 ``predict-frame`` 的区别: 那条走**批量 ``/predict`` 协议**(text prompt → box, 结果落
    Prediction 待采纳); 本条走 ``predict_interactive``, 候选**瞬态返回不落库**, 由前端
    state 持有, 采纳时直接建 Annotation (对齐图片侧交互链路)。

    ``mask_input_next`` 原样回传, 支撑同一帧多次点击的 low-res logits 回灌精修。
    """
    svc = MLBackendService(db)
    backend = await svc.get(backend_id)
    if not backend or not await svc.is_enabled(project_id, backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")
    if not backend.is_interactive:
        raise HTTPException(
            status_code=400,
            detail="This backend does not support interactive annotation",
        )

    await _require_ai_interactive_enabled(db, project_id)
    task = await _get_task_in_project(db, task_id, project_id)
    access = await _assert_annotation_write_access(db, task, current_user)
    await _assert_task_visible(db, task, current_user, access=access)

    if len(context.encode()) > MAX_INTERACTIVE_CONTEXT_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "reason": "interactive_context_too_large",
                "message": "interactive context must be <= 1 MiB",
            },
        )

    try:
        ctx = json.loads(context) if context else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid context JSON")
    if not isinstance(ctx, dict):
        raise HTTPException(status_code=400, detail="context must be a JSON object")
    if ctx.get("mask_prompt_source") is not None:
        assert_request_scopes(request, "annotations:read")

    ctx = await resolve_authorized_mask_prompt(
        db,
        task=task,
        context=ctx,
        frame_index=frame_index,
    )
    ctx, model_id = await _prepare_safe_interactive_context(
        backend,
        backend_id,
        ctx,
        task_id=task.id,
        frame_index=frame_index,
    )

    # v0.23.5 · WS-D · D2 · bounded frame read (Content-Length + streaming cap
    # + JPEG/PNG decode + dim/pixel limits) before any storage I/O.
    jpeg_bytes = await _read_capped_frame(frame, request=request)
    expected_size = (
        _decoded_image_size(jpeg_bytes)
        if ctx.get("output_geometry") == "mask"
        else None
    )

    storage = StorageService()
    # v0.23.5 · WS-D · D5 · boto3 sync put_object wrapped in to_thread.
    metric_operation = mask_ai_operation(ctx)
    metric_prompt = mask_ai_prompt_family(ctx)
    metric_geometry = str(ctx.get("output_geometry") or "unknown")
    upload_started = time.monotonic()
    try:
        frame_url = await asyncio.to_thread(
            storage.upload_crop_bytes,
            jpeg_bytes,
            f"frame-interactive/{task_id}/{frame_index}.jpg",
        )
    except Exception:
        observe_mask_ai_phase(
            operation=metric_operation,
            phase="upload",
            outcome="error",
            duration_seconds=time.monotonic() - upload_started,
        )
        record_mask_ai_operation(
            operation=metric_operation,
            prompt_family=metric_prompt,
            output_geometry=metric_geometry,
            outcome="error",
        )
        raise
    observe_mask_ai_phase(
        operation=metric_operation,
        phase="upload",
        outcome="success",
        duration_seconds=time.monotonic() - upload_started,
    )
    await db.commit()

    # 与 interactive_annotating 一致: 不注入项目级 DINO 阈值 (那是 gsam2 专属, 塞给 sam3
    # 等后端会出错); 推理参数由前端按 /setup.params 渲染后随 context 透传。
    client = RoutedMLBackendClient(
        db,
        backend,
        project_id=project_id,
        owner=f"api:interactive-frame:{task.id}:{frame_index}",
        operation="interactive_frame_predict",
        shadow_session_factory=shadow_session_factory,
        dispatch_context_factory=dispatch_context_factory,
    )
    predict_kwargs = (
        {"max_response_bytes": MAX_MASK_RESPONSE_BYTES}
        if ctx.get("output_geometry") == "mask"
        else {}
    )
    inference_started = time.monotonic()
    try:
        result = await client.predict_interactive(
            task_data={"id": str(task.id), "file_path": frame_url},
            context=ctx,
            **predict_kwargs,
        )
    except Exception:
        observe_mask_ai_phase(
            operation=metric_operation,
            phase="inference",
            outcome="error",
            duration_seconds=time.monotonic() - inference_started,
        )
        record_mask_ai_operation(
            operation=metric_operation,
            prompt_family=metric_prompt,
            output_geometry=metric_geometry,
            outcome="error",
        )
        raise
    observe_mask_ai_phase(
        operation=metric_operation,
        phase="inference",
        outcome="success",
        duration_seconds=time.monotonic() - inference_started,
    )
    # Do not return restricted candidates from a revoked/role-changed authority:
    # re-acquire the task row and locked membership at the response boundary.
    await _reacquire_write_boundary(db, task.id, current_user)
    encode_started = time.monotonic()
    try:
        response_body = _interactive_response(
            result,
            context=ctx,
            expected_size=expected_size,
            client=client,
            requested_backend_id=backend_id,
            model_id=model_id,
            task_id=task.id,
            frame_index=frame_index,
        )
    except Exception:
        observe_mask_ai_phase(
            operation=metric_operation,
            phase="encode",
            outcome="error",
            duration_seconds=time.monotonic() - encode_started,
        )
        record_mask_ai_operation(
            operation=metric_operation,
            prompt_family=metric_prompt,
            output_geometry=metric_geometry,
            outcome="error",
        )
        raise
    observe_mask_ai_phase(
        operation=metric_operation,
        phase="encode",
        outcome="success",
        duration_seconds=time.monotonic() - encode_started,
    )
    record_mask_ai_operation(
        operation=metric_operation,
        prompt_family=metric_prompt,
        output_geometry=metric_geometry,
        candidate_count=len(response_body["result"]),
        outcome="success",
    )
    return response_body


# ── v0.23.3 ADR-0050 §12.2 · 项目服务池绑定 API (pool-level) ──────────────────
# 与上面的 /{backend_id}/* (registry-level) 端点并存: 后者继续按 registry 实例操作
# (lifecycle / smoke), 本组按 pool 操作 (项目启用 / 可用清单)。off/observe 下每池
# 是 singleton, legacy_instance 指向唯一 registry, 行为与 registry-level 等价。


@router.get(
    "/pools/available",
    response_model=ProjectMLBackendPoolList,
    dependencies=[Depends(require_project_visible)],
)
async def list_project_ml_backend_pools(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """列出本项目可用的服务池 + 启用态 + 成员数 (ADR-0050 §12.2)。

    v0.23.3: off/observe 下每池 singleton; 完整池管理 UI 留给 v0.23.4。
    """
    svc = MLBackendService(db)
    rows = await svc.list_pools_available_for_project(project_id)
    items = [
        ProjectMLBackendPoolItem(
            pool=MLBackendPoolSummary(
                id=pool.id,
                name=pool.name,
                enabled=pool.enabled,
                legacy_instance_id=pool.legacy_instance_id,
                member_count=member_count,
                routing_generation=pool.routing_generation,
            ),
            enabled=assoc.enabled if assoc else False,
            default_variants=assoc.default_variants if assoc else None,
        )
        for pool, member_count, assoc in rows
    ]
    return ProjectMLBackendPoolList(items=items)


@router.put(
    "/pools/{pool_id}/enablement",
    response_model=ProjectMLBackendPoolItem,
    dependencies=[Depends(require_project_owner)],
)
async def set_project_ml_backend_pool_enablement(
    project_id: uuid.UUID,
    pool_id: uuid.UUID,
    data: ProjectMLBackendPoolEnablement,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """切换本项目对某服务池的启用 + 写项目级变体覆盖 (pool 级, ADR-0050 §12.2)。

    pool 必须存在且 enabled (disabled / 空 / 无 capability fingerprint 的 pool 不可启用)。
    """
    svc = MLBackendService(db)
    pool = await db.get(MLBackendServicePool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="service pool not found")
    if data.enabled and (not pool.enabled or pool.legacy_instance_id is None):
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "ml_backend_pool_not_enabled",
                "message": "pool is disabled or has no legacy instance; cannot enable for project",
            },
        )
    overrides = {
        k: v for k, v in (("default_variants", data.default_variants),) if v is not None
    }
    try:
        assoc = await svc.set_pool_enabled(
            project_id, pool_id, enabled=data.enabled, **overrides
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    member_count = await _pool_member_count(db, pool_id)
    await AuditService.log(
        db,
        actor=current_user,
        action="ml_backend_pool.enablement",
        target_type="ml_backend_pool",
        target_id=str(pool_id),
        request=request,
        status_code=200,
        detail={"project_id": str(project_id), "enabled": data.enabled},
    )
    await db.commit()
    await db.refresh(pool)
    await db.refresh(assoc)
    return ProjectMLBackendPoolItem(
        pool=MLBackendPoolSummary(
            id=pool.id,
            name=pool.name,
            enabled=pool.enabled,
            legacy_instance_id=pool.legacy_instance_id,
            member_count=member_count,
            routing_generation=pool.routing_generation,
        ),
        enabled=assoc.enabled,
        default_variants=assoc.default_variants,
    )


async def _pool_member_count(db: AsyncSession, pool_id: uuid.UUID) -> int:
    from sqlalchemy import func, select as _select

    from app.db.models.ml_backend_pool import MLBackendPoolMember

    result = await db.execute(
        _select(func.count(MLBackendPoolMember.id)).where(
            MLBackendPoolMember.pool_id == pool_id
        )
    )
    return int(result.scalar() or 0)
