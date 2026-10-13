import uuid
from typing import AsyncGenerator, Callable
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.base import async_session
from app.db.models.user import User
from app.db.models.project import Project
from app.core.security import decode_access_token
from app.core.token_blacklist import is_blacklisted, get_user_generation
from app.services.project_access import ProjectAccess, resolve_project_access_by_id
from app.services.gpu_arbitration.contracts import (
    GPUDispatchContextFactory,
    GPUShadowSessionFactory,
)
from app.services.gpu_arbitration.dispatch import build_gpu_dispatch_context_factory

bearer_scheme = HTTPBearer()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session


def get_gpu_shadow_session_factory() -> GPUShadowSessionFactory:
    """可覆写的 observe 短会话工厂；业务请求 session 不交给旁路关闭。"""

    return async_session


def get_gpu_dispatch_context_factory() -> GPUDispatchContextFactory:
    """可覆写的 workload authority；构造本身不访问 DB、Redis 或私钥。"""

    return build_gpu_dispatch_context_factory(async_session)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效的认证凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = credentials.credentials

    # v0.15.11 · None = JWT/密码登录 principal（视为 full-access，不受 require_scopes 约束）；
    # 非 None = api_key principal 的 scopes 列表，供 require_scopes 校验。
    request.state.api_key_scopes = None

    # v0.9.3 · ak_ 前缀走 api_key 路径；不走 JWT 解码（避免 jose 抛形错日志）
    from app.services import api_key_service

    if api_key_service.is_api_key_token(token):
        resolved = await api_key_service.resolve_token(db, token)
        if resolved is None:
            raise exc
        key, user = resolved
        request.state.api_key_scopes = list(key.scopes or [])
        await db.commit()  # 持久化 last_used_at
        return user

    try:
        payload = decode_access_token(token)
        user_id: str | None = payload.get("sub")
        if user_id is None:
            raise exc
    except JWTError:
        raise exc

    jti: str | None = payload.get("jti")
    token_gen: int = payload.get("gen", 0)

    if jti:
        try:
            if await is_blacklisted(jti):
                raise exc
        except HTTPException:
            raise
        except Exception:
            pass

        try:
            current_gen = await get_user_generation(user_id)
            if current_gen > token_gen:
                raise exc
        except HTTPException:
            raise
        except Exception:
            pass

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise exc
    return user


def require_roles(*roles: str) -> Callable:
    """工厂函数：返回一个依赖，要求当前用户持有指定角色之一。"""

    async def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要角色权限: {'或'.join(roles)}",
            )
        return current_user

    return checker


async def require_active_task_actor(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
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
        current_user: User = Depends(get_current_user),
    ) -> User:
        assert_request_scopes(request, *needed)
        return current_user

    return checker


async def assert_project_visible(
    project_id: uuid.UUID,
    db: AsyncSession,
    user: User,
) -> Project:
    """
    统一可见性：超级管理员、合法的管理型负责人，或校验通过的成员。
    非管理型 owner 不再有隐式可见性；平台 project_admin 若只是他人项目的成员，
    按成员可见性处理（不获得管理权）。不可见则 404 隐藏存在性。
    """
    project, _access = await resolve_project_access_by_id(
        db, user=user, project_id=project_id
    )
    return project


async def require_project_visible(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Project:
    return await assert_project_visible(project_id, db, user)


async def require_project_owner(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Project:
    """super_admin 或具备合法管理身份的负责人可执行写操作。

    通过统一访问解析器判定，确保停用账号、被转移项目与非管理型 owner 不会
    仅凭旧字段获得管理权。
    """
    project, access = await resolve_project_access_by_id(
        db, user=user, project_id=project_id
    )
    if not access.is_manager:
        raise HTTPException(status_code=403, detail="仅项目负责人或超级管理员可执行")
    return project


async def project_access_for_path(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProjectAccess:
    """Resolve the immutable project access context for the path project.

    Downstream routes depend on this (or ``require_project_capability``) instead
    of re-deriving authority from a global role.
    """

    _, access = await resolve_project_access_by_id(db, user=user, project_id=project_id)
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
