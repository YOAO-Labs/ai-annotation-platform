import uuid
from typing import AsyncGenerator, Callable
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from aap_identity.contract import Principal
from app.db.base import async_session
from app.db.models.project import Project
from aap_workspace_contract.access import ProjectAccess
from app.services.identity_contracts import (
    IdentityActorFacts,
    IdentityCredentialRejected,
    IdentityGatewayUnavailable,
)
from app.services.identity_gateway import (
    IdentityGateway,
    get_identity_gateway,
)
from app.services.project_access import resolve_project_access_by_id
from app.services.quality_job_dispatch import QualityJobDispatcher
from app.services.media_job_dispatch import MediaJobDispatcher
from app.services.training_dataset_dispatch import TrainingDatasetDispatcher
from app.services.dataset_task_facts import ProjectTaskSourceReader
from app.services.training_authority_contract import TrainingAuthorityPort
from app.services.job_execution_control import JobExecutionControl
from app.services.prediction_retry import PredictionRetryDispatcher
from app.application.preannotation_contract import BatchPredictionDispatcher
from app.services.video_tracking.dispatch import VideoTrackerJobDispatcher
from app.application.async_job_contracts import (
    AsyncJobReadPort,
    JobCancellationPort,
    PredictionRetryPort,
)

bearer_scheme = HTTPBearer()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        try:
            yield session
        except SQLAlchemyError as exc:
            from app.services.mask_gc import mask_fence_error

            rejection = mask_fence_error(exc)
            if rejection is None:
                raise
            await session.rollback()
            raise HTTPException(
                status_code=rejection.status_code, detail=rejection.detail
            ) from None


def get_prediction_input_session_factory():
    """Independent commits retain cleanup intent outside the request transaction."""
    return async_session


def get_quality_job_dispatcher() -> QualityJobDispatcher:
    """Bind the quality execution port without importing tasks into services."""
    from app.services.jobs_queue import build_quality_job_dispatcher

    return build_quality_job_dispatcher()


def get_media_job_dispatcher() -> MediaJobDispatcher:
    """Bind media execution without importing tasks into domain services."""
    from app.workers.media_dispatch import CeleryMediaJobDispatcher

    return CeleryMediaJobDispatcher()


def get_training_dataset_dispatcher() -> TrainingDatasetDispatcher:
    """Bind asynchronous training-dataset transport at the HTTP boundary."""
    from app.workers.training_dataset_dispatch import CeleryTrainingDatasetDispatcher

    return CeleryTrainingDatasetDispatcher()


def get_labeling_task_source_reader(
    db: AsyncSession = Depends(get_db),
) -> ProjectTaskSourceReader:
    """Bind labeling's source-read port to the request's transaction."""
    from app.services.labeling_assets import SqlAlchemyProjectTaskSourceReader

    return SqlAlchemyProjectTaskSourceReader(db)


def get_training_authority(
    db: AsyncSession = Depends(get_db),
) -> TrainingAuthorityPort:
    """Bind current training authority checks to the request's Session."""
    from app.services.training_datasets import bind_training_authority

    return bind_training_authority(db)


def get_job_execution_control() -> JobExecutionControl:
    """Bind cooperative execution cancellation without importing brokers into services."""
    from app.services.jobs_queue import build_job_execution_control

    return build_job_execution_control()


def get_prediction_retry_dispatcher() -> PredictionRetryDispatcher:
    """Bind retry execution without importing worker tasks into use cases."""
    from app.services.jobs_queue import build_prediction_retry_dispatcher

    return build_prediction_retry_dispatcher()


def get_async_job_read_port(
    db: AsyncSession = Depends(get_db),
) -> AsyncJobReadPort:
    """Bind async-job reads to the request's SQL session."""
    from app.application.async_job_read import SqlAsyncJobReadAdapter

    return SqlAsyncJobReadAdapter(db)


def get_job_cancellation_port(
    db: AsyncSession = Depends(get_db),
    execution_control: JobExecutionControl = Depends(get_job_execution_control),
) -> JobCancellationPort:
    """Bind cancellation workflow to the request session and execution control."""
    from app.application.job_cancellation import SqlJobCancellation

    return SqlJobCancellation(db, execution_control=execution_control)


def get_prediction_retry_port(
    db: AsyncSession = Depends(get_db),
    dispatcher: PredictionRetryDispatcher = Depends(get_prediction_retry_dispatcher),
) -> PredictionRetryPort:
    """Bind retry workflow to the request's session and dispatch port."""
    from app.application.prediction_retry import SqlPredictionRetry

    return SqlPredictionRetry(db, dispatcher=dispatcher)


def get_batch_prediction_dispatcher() -> BatchPredictionDispatcher:
    """Bind batch prediction dispatch at the HTTP composition boundary."""
    from app.services.jobs_queue import build_batch_prediction_dispatcher

    return build_batch_prediction_dispatcher()


def get_video_tracker_job_dispatcher() -> VideoTrackerJobDispatcher:
    """Bind video tracker execution at the HTTP composition boundary."""
    from app.services.jobs_queue import build_video_tracker_job_dispatcher

    return build_video_tracker_job_dispatcher()


async def get_current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
    identity: IdentityGateway = Depends(get_identity_gateway),
) -> Principal:
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效的认证凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = credentials.credentials
    try:
        principal = await identity.authenticate(token, db=db)
    except IdentityCredentialRejected:
        raise exc
    except IdentityGatewayUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="身份认证服务暂不可用",
        ) from None

    request.state.identity_principal = principal
    is_api_key = principal.credential_type == "api_key"
    request.state.api_key_scopes = list(principal.scopes or []) if is_api_key else None
    request.state.api_key_id = str(principal.credential_id) if is_api_key else None
    if is_api_key and identity.uses_caller_session(db):
        await db.commit()
    return principal


async def get_current_user(
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
    identity: IdentityGateway = Depends(get_identity_gateway),
) -> IdentityActorFacts:
    try:
        actor = await identity.actor_context(principal, db=db)
    except IdentityCredentialRejected:
        for field in ("identity_principal", "api_key_scopes", "api_key_id"):
            if hasattr(request.state, field):
                delattr(request.state, field)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except IdentityGatewayUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="身份认证服务暂不可用",
        ) from None
    return IdentityActorFacts(**actor.model_dump())


async def get_current_user_id(
    current_user: IdentityActorFacts = Depends(get_current_user),
) -> uuid.UUID:
    """Return the authenticated account ID from the shared request resolution."""
    return current_user.id


def require_roles(*roles: str) -> Callable:
    """工厂函数：返回一个依赖，要求当前用户持有指定角色之一。"""

    async def checker(
        request: Request,
        current_user: IdentityActorFacts = Depends(get_current_user),
    ) -> IdentityActorFacts:
        principal = getattr(request.state, "identity_principal", None)
        role = principal.role if principal is not None else current_user.role
        if role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要角色权限: {'或'.join(roles)}",
            )
        return current_user

    return checker


async def require_active_task_actor(
    current_user: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IdentityActorFacts:
    """Recheck account state before a task handler takes any resource locks."""

    from app.services.task_lock import assert_task_user_active

    if not await assert_task_user_active(db, current_user.id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号已停用，请联系管理员",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return current_user


# v0.15.11 · full-access 通配 scope；含 "*" 的 api_key 视为全权，绕过 scope 校验。
WILDCARD_SCOPE = "*"


def assert_request_scopes(request: Request, *needed: str) -> None:
    """Enforce API-key scopes while treating JWT principals as full access."""

    scopes = getattr(request.state, "api_key_scopes", None)
    if scopes is None or WILDCARD_SCOPE in scopes:
        return
    missing = [scope for scope in needed if scope not in scopes]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key 缺少所需权限: {', '.join(missing)}",
            headers={"WWW-Authenticate": f'Bearer scope="{" ".join(needed)}"'},
        )


def require_scopes(*needed: str) -> Callable:
    """工厂函数：要求 api_key principal 持有全部 ``needed`` scope。

    - JWT / 密码登录（request.state.api_key_scopes is None）→ full-access，放行。
    - api_key 含 ``"*"``（full-access）→ 放行。
    - api_key scopes ⊇ needed → 放行；否则 403 insufficient_scope。
    """

    async def checker(
        request: Request,
        current_user: IdentityActorFacts = Depends(get_current_user),
    ) -> IdentityActorFacts:
        assert_request_scopes(request, *needed)
        return current_user

    return checker


async def assert_project_visible(
    project_id: uuid.UUID,
    db: AsyncSession,
    actor_id: uuid.UUID,
) -> Project:
    """
    统一可见性：超级管理员、合法的管理型负责人，或校验通过的成员。
    非管理型 owner 不再有隐式可见性；平台 project_admin 若只是他人项目的成员，
    按成员可见性处理（不获得管理权）。不可见则 404 隐藏存在性。
    """
    project, _access = await resolve_project_access_by_id(
        db, actor_id=actor_id, project_id=project_id
    )
    return project


async def require_project_visible(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
) -> Project:
    return await assert_project_visible(project_id, db, actor_id)


async def require_project_owner(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
) -> Project:
    """super_admin 或具备合法管理身份的负责人可执行写操作。

    通过统一访问解析器判定，确保停用账号、被转移项目与非管理型 owner 不会
    仅凭旧字段获得管理权。
    """
    project, access = await resolve_project_access_by_id(
        db, actor_id=actor_id, project_id=project_id
    )
    if not access.is_manager:
        raise HTTPException(status_code=403, detail="仅项目负责人或超级管理员可执行")
    return project


async def project_access_for_path(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
) -> ProjectAccess:
    """Resolve the immutable project access context for the path project.

    Downstream routes depend on this (or ``require_project_capability``) instead
    of re-deriving authority from a global role.
    """

    _, access = await resolve_project_access_by_id(
        db, actor_id=actor_id, project_id=project_id
    )
    return access


def require_project_capability(*capabilities: str) -> Callable:
    """Factory: require every listed :class:`ProjectCapability` value.

    Returns the resolved :class:`ProjectAccess` so handlers can read the
    membership identity/version without a second lookup.
    """

    async def checker(
        access: ProjectAccess = Depends(project_access_for_path),
    ) -> ProjectAccess:
        missing = sorted(cap for cap in capabilities if cap not in access.capabilities)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"缺少项目权限: {', '.join(missing)}",
            )
        return access

    return checker
