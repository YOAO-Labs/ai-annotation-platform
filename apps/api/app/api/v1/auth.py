import uuid
from math import ceil, isfinite

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.ratelimit import limiter
from app.core.password import validate_password_strength
from app.deps import get_db, get_current_user
from app.db.models.user import User
from app.db.base import async_session
from app.schemas.user import Token, LoginRequest, UserOut
from app.schemas.invitation import OpenRegisterRequest, RegisterResponse
from app.core.security import (
    verify_password,
    create_access_token,
    hash_password,
    decode_access_token,
)
from app.services.audit import AuditAction, AuditService
from app.services.captcha_service import verify_turnstile_token
from app.services import login_failed_counter
from app.services.password_reset import PasswordResetService
from app.services.email_verification import EmailVerificationService
from app.services.email import (
    SmtpConfigError,
    send_password_reset_email,
    send_verification_email,
)
from app.services.system_settings_service import SystemSettingsService
from app.services.identity_commands import IdentityCommandService

from app.config import settings
from aap_identity.contract import REFRESH_GRACE_SECONDS
from app.services.identity_gateway import (
    IdentityGatewayUnavailable,
)
from app.services.identity_session_data import IdentitySessionData
import logging

logger = logging.getLogger("anno-api.auth")
router = APIRouter()


class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    # v0.8.7 · Cloudflare Turnstile token；TURNSTILE_ENABLED=False 时忽略。
    captcha_token: str | None = None

    @field_validator("email")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return (v or "").strip().lower()


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _strength(cls, v: str) -> str:
        errors = validate_password_strength(v)
        if errors:
            raise ValueError("; ".join(errors))
        return v


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=1)


class ResendVerificationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)

    @field_validator("email")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return (v or "").strip().lower()


async def _dispatch_verification_email(db: AsyncSession, user: User) -> None:
    """生成验证 token + 发信。SMTP 未配置 / 发送失败时仅记日志（dev 友好），不阻断注册。"""
    token = await EmailVerificationService(db).create_token(user)
    base_url = (
        await SystemSettingsService.get(db, "frontend_base_url")
        or settings.frontend_base_url
    )
    verify_url = f"{str(base_url).rstrip('/')}/verify-email?token={token}"
    smtp_host = await SystemSettingsService.get(db, "smtp_host")
    # 含明文一次性 token 的 url 仅在非 production 落日志（dev 友好）；production 只记
    # user_id + 原因，避免 token 经集中式日志扩散后被用于 24h 内冒充验证。
    is_prod = settings.environment == "production"
    if not smtp_host:
        if is_prod:
            logger.info(
                "Email verification pending for user %s: SMTP not configured", user.id
            )
        else:
            logger.info(
                "Email verification token for %s (SMTP not configured): %s",
                user.email,
                verify_url,
            )
        return
    try:
        await send_verification_email(db, user.email, verify_url)
    except SmtpConfigError as e:
        if is_prod:
            logger.warning("发送验证邮件失败 (user %s): %s", user.id, e)
        else:
            logger.warning(
                "发送验证邮件失败 (%s): %s; url=%s", user.email, e, verify_url
            )


@router.post("/login", response_model=Token)
@limiter.limit("5/minute")
async def login(
    data: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    client_ip = request.client.host if request.client else ""
    threshold = settings.login_captcha_threshold

    # v0.9.3 · progressive CAPTCHA：失败 ≥ 阈值后必须带 Turnstile token
    failed_before = await login_failed_counter.get_count(client_ip)
    if failed_before >= threshold:
        ok = await verify_turnstile_token(data.captcha_token, client_ip)
        if not ok:
            raise HTTPException(
                status_code=400,
                detail="captcha_required",
                headers={"X-Login-Failed-Count": str(failed_before)},
            )

    # Password verification and login presence share the bounded lifecycle row lock
    # with password resets and account deactivation.
    identity = IdentityCommandService(db)
    user = await identity.lock_account_by_email(data.email)
    if user is None or not verify_password(data.password, user.password_hash):
        new_count = await login_failed_counter.increment(client_ip)
        await AuditService.log(
            db,
            actor=None,
            action=AuditAction.AUTH_LOGIN,
            target_type="user",
            target_id=data.email,
            request=request,
            status_code=401,
            detail={
                "email": data.email,
                "result": "invalid_credentials",
                "user_agent": request.headers.get("user-agent", "")[:256],
                "ip_failed_count": new_count,
            },
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误",
            headers={
                "WWW-Authenticate": "Bearer",
                "X-Login-Failed-Count": str(new_count),
            },
        )
    if not user.is_active:
        await AuditService.log(
            db,
            actor=user,
            action=AuditAction.AUTH_LOGIN,
            target_type="user",
            target_id=str(user.id),
            request=request,
            status_code=403,
            detail={"email": user.email, "result": "deactivated"},
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="账户已被停用"
        )

    # v0.12.0 · 邮箱验证 gate（仅开放注册场景生效；邀请/管理员建号恒已验证）。
    # 用 400 + 结构化 detail.code，让前端识别后显示「重发验证邮件」入口而非通用 toast。
    if settings.email_verification_required and user.email_verified_at is None:
        await AuditService.log(
            db,
            actor=user,
            action=AuditAction.AUTH_LOGIN,
            target_type="user",
            target_id=str(user.id),
            request=request,
            status_code=400,
            detail={"email": user.email, "result": "email_not_verified"},
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "email_not_verified",
                "message": "邮箱未验证，请查收验证邮件后再登录",
            },
        )

    # 凭据通过 → 清零失败计数
    await login_failed_counter.reset(client_ip)

    now = datetime.now(timezone.utc)
    identity.record_login_for_locked_user(user, now)
    from app.core.token_blacklist import get_user_generation

    gen = await get_user_generation(str(user.id), db=db)
    token = create_access_token(subject=str(user.id), role=user.role, gen=gen)
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.AUTH_LOGIN,
        target_type="user",
        target_id=str(user.id),
        request=request,
        status_code=200,
        detail={"email": user.email, "result": "success"},
    )
    await db.commit()
    return Token(access_token=token)


@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/forgot-password", status_code=202)
@limiter.limit("3/minute")
async def forgot_password(
    data: ForgotPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
):
    if not await verify_turnstile_token(
        data.captcha_token, request.client.host if request.client else None
    ):
        raise HTTPException(status_code=400, detail="captcha_failed")

    # Run both existing and unknown addresses through the same post-response
    # path so SMTP latency cannot identify registered accounts.
    background_tasks.add_task(_complete_password_recovery, data.email)
    return {
        "message": (
            "如果该邮箱已注册，您将收到一封包含重置链接的邮件。"
            "若未收到，请联系管理员协助重置。"
        )
    }


async def _complete_password_recovery(email: str) -> None:
    async with async_session() as db:
        svc = PasswordResetService(db)
        token = await svc.create_token(email)
        if not token:
            return
        # 先持久化 token，再触发外部 SMTP 副作用，避免「邮件已发送但提交失败」
        # 产生用户永远无法使用的链接。发送失败时在后续事务中立即标记失效。
        await db.commit()
        base_url = (
            await SystemSettingsService.get(db, "frontend_base_url")
            or settings.frontend_base_url
        )
        reset_url = f"{str(base_url).rstrip('/')}/reset-password?token={token}"
        try:
            await send_password_reset_email(
                db,
                email,
                reset_url,
                expires_in_hours=svc.TOKEN_EXPIRY_HOURS,
            )
        except SmtpConfigError as exc:
            # 保持统一 202 响应以防邮箱枚举；仅记录不含 token 的定位信息。
            await svc.invalidate_token(token)
            logger.warning("Password reset email unavailable for %s: %s", email, exc)
        else:
            # 只有 SMTP 调用正常返回后才记录已发送；token 已在调用前持久化。
            logger.info("Password reset email sent for %s", email)

        await db.commit()


@router.post("/reset-password")
async def reset_password(
    data: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    svc = PasswordResetService(db)
    user = await svc.consume_token(data.token)
    if not user:
        # consume_token 会将已关联停用账号的 token 标记为已消费；提交这个结果，
        # 防止账号日后重新启用后仍可使用停用期间签发的旧链接。
        await db.commit()
        raise HTTPException(status_code=400, detail="重置链接无效或已过期")

    IdentityCommandService(db).set_password_for_locked_user(
        user, hash_password(data.new_password)
    )
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.USER_PASSWORD_CHANGE,
        target_type="user",
        target_id=str(user.id),
        request=request,
        status_code=200,
        detail={"method": "reset_token"},
    )
    # 密码恢复必须让之前签发的所有会话立即失效；新登录会读取递增后的代际号。
    from app.core.token_blacklist import increment_user_generation

    await increment_user_generation(str(user.id), db=db)
    await db.commit()
    return {"message": "密码已重置，请使用新密码登录"}


@router.post("/verify-email")
async def verify_email(
    data: VerifyEmailRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """v0.12.0 · 消费验证 token，置 email_verified_at。"""
    user = await EmailVerificationService(db).consume_token(data.token)
    if not user:
        raise HTTPException(status_code=400, detail="验证链接无效或已过期")
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.USER_REGISTER,
        target_type="user",
        target_id=str(user.id),
        request=request,
        status_code=200,
        detail={"email": user.email, "result": "email_verified"},
    )
    await db.commit()
    return {"message": "邮箱已验证，请登录"}


@router.post("/send-verification-email", status_code=202)
@limiter.limit("3/minute")
async def send_verification_email_endpoint(
    data: ResendVerificationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """v0.12.0 · 重发验证邮件。无论邮箱是否存在 / 已验证都返回 202（防枚举）。"""
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()
    if user is not None and user.email_verified_at is None:
        await _dispatch_verification_email(db, user)
        await db.commit()
    return {"message": "如果该邮箱待验证，您将收到一封包含验证链接的邮件"}


@router.get("/registration-status")
async def registration_status(db: AsyncSession = Depends(get_db)):
    enabled = bool(await SystemSettingsService.get(db, "allow_open_registration"))
    return {"open_registration_enabled": enabled}


@router.post("/register-open", response_model=RegisterResponse, status_code=201)
@limiter.limit("3/minute")
async def register_open(
    payload: OpenRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    if not await SystemSettingsService.get(db, "allow_open_registration"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="开放注册未启用",
        )

    if not await verify_turnstile_token(
        payload.captcha_token, request.client.host if request.client else None
    ):
        raise HTTPException(status_code=400, detail="captcha_failed")

    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该邮箱已被注册",
        )

    require_verification = settings.email_verification_required
    now = datetime.now(timezone.utc)
    user = await IdentityCommandService(db).create_open_registration_account(
        email=payload.email,
        name=payload.name,
        password_hash=hash_password(payload.password),
        email_verification_required=require_verification,
        now=now,
    )

    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.USER_REGISTER,
        target_type="user",
        target_id=str(user.id),
        request=request,
        status_code=201,
        detail={
            "email": user.email,
            "role": user.role,
            "method": "open_registration",
            "email_verification_required": require_verification,
        },
    )

    if require_verification:
        await _dispatch_verification_email(db, user)
        await db.commit()
        await db.refresh(user)
        return RegisterResponse(
            access_token=None,
            user=UserOut.model_validate(user),
            email_verification_required=True,
        )

    await db.commit()
    await db.refresh(user)
    token = create_access_token(subject=str(user.id), role=user.role)
    return RegisterResponse(
        access_token=token,
        token_type="bearer",
        user=UserOut.model_validate(user),
    )


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.core.token_blacklist import blacklist_token

    current_user = await IdentityCommandService(db).lock_active_account(current_user.id)
    principal = getattr(request.state, "identity_principal", None)
    if (
        principal is None
        or principal.credential_type != "jwt"
        or principal.expires_at is None
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    remaining_seconds = ceil(
        (principal.expires_at - datetime.now(timezone.utc)).total_seconds()
    )
    await blacklist_token(str(principal.credential_id), remaining_seconds, db=db)

    IdentityCommandService.record_logout_for_locked_user(current_user)

    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.AUTH_LOGOUT,
        target_type="user",
        target_id=str(current_user.id),
        request=request,
        status_code=204,
    )
    await db.commit()


# v0.8.8 · 鉴权过期重连：WebSocket / 长会话标注员 token 到期时拿旧 token 换新 token，
# 不需要用户重新输入密码。前端 useNotificationSocket onclose 1008/4001 时调用。
#
# Grace 期：旧 token 过期不超过 7 天即可 refresh；超出强制重新登录。
# 安全闭环：jti 黑名单（logout 后旧 token 立即失效）+ user.gen 比对（logout-all
# 后旧 token 全失效）+ user.is_active = True + 速率 5/min。
_REFRESH_GRACE = timedelta(seconds=REFRESH_GRACE_SECONDS)


@router.post("/refresh", response_model=Token)
@limiter.limit("5/minute")
async def refresh_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    db: AsyncSession = Depends(get_db),
):
    """v0.8.8 · 用即将 / 已过期的 token 换新 token（grace 7 天）。

    返回 401 时前端应跳转登录页（grace 已过、被 logout、被 deactivate）。
    """
    raw = credentials.credentials
    try:
        payload = decode_access_token(raw, verify_exp=False)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    subject = payload.get("sub")
    jti_value = payload.get("jti")
    exp_value = payload.get("exp")
    generation = payload.get("gen", 0)
    try:
        if not isinstance(subject, str) or not isinstance(jti_value, str):
            raise ValueError("Missing token identity")
        actor_id = uuid.UUID(subject)
        jti = uuid.UUID(jti_value)
        if (
            type(exp_value) not in (int, float)
            or not isfinite(exp_value)
            or int(exp_value) != exp_value
            or type(generation) is not int
            or not 0 <= generation < 2**63
        ):
            raise ValueError("Invalid token claims")
        expired_at = datetime.fromtimestamp(int(exp_value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="malformed_token"
        )

    # Grace 检查：超过 7 天的过期 token 一律拒绝
    now = datetime.now(timezone.utc)
    if now > expired_at + _REFRESH_GRACE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="grace_expired"
        )

    user = await IdentityCommandService(db).lock_account(actor_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="user_inactive"
        )

    from aap_identity.contract import (
        IdentityFailure,
        IdentityUnavailable,
        decode_principal_reply,
    )

    try:
        reply = await IdentitySessionData(db).read_jwt_principal(
            actor_id, jti, generation, expired_at, for_refresh=True
        )
        principal = decode_principal_reply(reply)
    except IdentityFailure as exc:
        if exc.code in {"credential_busy", "invalid_request"}:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="身份认证服务暂不可用",
            ) from None
        detail = {
            "credential_revoked": "token_revoked",
            "generation_outdated": "generation_outdated",
            "token_expired": "grace_expired",
            "user_inactive": "user_inactive",
            "invalid_account_role": "invalid_account_role",
        }.get(exc.code, "invalid_token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=detail
        ) from None
    except (IdentityUnavailable, IdentityGatewayUnavailable):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="身份认证服务暂不可用",
        ) from None

    cur_gen = principal.generation
    if cur_gen is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="身份认证服务暂不可用",
        )

    new_token = create_access_token(
        subject=str(user.id), role=principal.role, gen=cur_gen
    )

    IdentityCommandService.record_seen_for_locked_user(user, now)
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.AUTH_TOKEN_REFRESH,
        target_type="user",
        target_id=str(user.id),
        request=request,
        status_code=200,
        detail={
            "old_jti": str(jti),
            "expired_seconds_ago": int((now - expired_at).total_seconds()),
        },
    )
    await db.commit()
    return Token(access_token=new_token)


@router.post("/logout-all", response_model=Token)
async def logout_all(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.core.token_blacklist import increment_user_generation

    current_user = await IdentityCommandService(db).lock_active_account(current_user.id)
    new_gen = await increment_user_generation(str(current_user.id), db=db)
    IdentityCommandService.record_logout_for_locked_user(current_user)

    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.AUTH_LOGOUT_ALL,
        target_type="user",
        target_id=str(current_user.id),
        request=request,
        status_code=200,
        detail={"new_generation": new_gen},
    )
    await db.commit()

    new_token = create_access_token(
        subject=str(current_user.id), role=current_user.role, gen=new_gen
    )
    return Token(access_token=new_token)
