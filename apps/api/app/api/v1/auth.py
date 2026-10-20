import uuid

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.ratelimit import limiter
from app.core.password import validate_password_strength
from app.deps import bearer_scheme, get_db, get_current_principal
from app.db.models.user import User
from app.db.base import async_session
from app.schemas.user import Token, LoginRequest, UserOut
from app.schemas.invitation import OpenRegisterRequest, RegisterResponse
from app.core.security import (
    create_access_token,
    hash_password,
)
from app.services.audit import AuditAction, AuditService, audit_origin_from_request
from app.middleware.request_id import request_id_var
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
from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.session_contract import SessionFailure, decode_session_request
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway, get_identity_gateway
from app.services.identity_session_commands import (
    IdentitySessionCommands,
    SessionCommandsPort,
)
from app.api.v1.account_commands import execute_account, get_account_commands
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


def get_session_commands(
    request: Request,
    db: AsyncSession = Depends(get_db),
    identity: IdentityGateway = Depends(get_identity_gateway),
) -> SessionCommandsPort:
    return IdentitySessionCommands(
        db,
        identity,
        expected_principal=getattr(request.state, "identity_principal", None),
    )


def _session_error(operation, error):
    code = error.code
    if code == "account_busy":
        raise HTTPException(status_code=409, detail="账号正在更新，请刷新后重试")
    if code == "operation_conflict":
        raise HTTPException(status_code=409, detail={"reason": "operation_conflict"})
    if code in {"credential_busy", "invalid_request"}:
        raise HTTPException(
            status_code=503,
            detail="身份认证服务暂不可用"
            if operation == "refresh"
            else "身份会话服务暂不可用",
        )
    if operation == "login":
        if code == "captcha_required":
            raise HTTPException(
                status_code=400,
                detail="captcha_required",
                headers={"X-Login-Failed-Count": str(error.failed_count)},
            )
        if code == "user_inactive":
            raise HTTPException(status_code=403, detail="账户已被停用")
        if code == "email_not_verified":
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "email_not_verified",
                    "message": "邮箱未验证，请查收验证邮件后再登录",
                },
            )
        headers = {"WWW-Authenticate": "Bearer"}
        if error.failed_count is not None:
            headers["X-Login-Failed-Count"] = str(error.failed_count)
        raise HTTPException(status_code=401, detail="邮箱或密码错误", headers=headers)
    detail = "无效的认证凭据"
    if operation == "refresh":
        detail = {
            "credential_revoked": "token_revoked",
            "token_expired": "grace_expired",
            "credential_invalid": "user_inactive",
        }.get(code, code)
    raise HTTPException(
        status_code=401,
        detail=detail,
        headers=(
            {"WWW-Authenticate": "Bearer"}
            if operation != "refresh" or code == "invalid_token"
            else None
        ),
    )


async def _execute_session(request, commands, operation, payload, *, token=None):
    origin = audit_origin_from_request(request)
    try:
        command = decode_session_request(
            {
                "schema": "aap-identity-sessions/v1",
                "operation": operation,
                "operation_id": str(uuid.uuid4()),
                "token": token,
                "payload": payload,
                "audit_context": {
                    "method": origin.method,
                    "path": origin.path,
                    "ip": origin.ip,
                    "request_id": request_id_var.get() or None,
                },
                "user_agent": request.headers.get("user-agent", "")[:256],
            }
        )
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(status_code=422, detail="会话请求无效") from None
    try:
        reply = await commands.execute(command)
    except SessionFailure as error:
        _session_error(operation, error)
    except (IdentityUnavailable, IdentityGatewayUnavailable):
        raise HTTPException(
            status_code=503,
            detail="身份认证服务暂不可用"
            if operation == "refresh"
            else "身份会话服务暂不可用",
        ) from None
    principal = reply.principal
    admitted = getattr(request.state, "identity_principal", None)
    if admitted is not None and (
        any(
            getattr(principal, field) != getattr(admitted, field)
            for field in ("actor_id", "credential_type", "credential_id", "generation")
        )
        or (
            principal.credential_type == "jwt"
            and principal.expires_at != admitted.expires_at
        )
    ):
        raise HTTPException(status_code=503, detail="身份会话服务返回了不匹配的主体")
    request.state.identity_principal = principal
    is_key = principal.credential_type == "api_key"
    request.state.api_key_scopes = list(principal.scopes or []) if is_key else None
    request.state.api_key_id = str(principal.credential_id) if is_key else None
    return reply


@router.post("/login", response_model=Token)
@limiter.limit("5/minute")
async def login(
    data: LoginRequest,
    request: Request,
    commands: SessionCommandsPort = Depends(get_session_commands),
):
    client_ip = request.client.host if request.client else ""
    failed_before = await login_failed_counter.get_count(client_ip)
    captcha_verified = not settings.turnstile_enabled
    if failed_before >= settings.login_captcha_threshold:
        captcha_verified = await verify_turnstile_token(data.captcha_token, client_ip)
        if not captcha_verified:
            raise HTTPException(
                status_code=400,
                detail="captcha_required",
                headers={"X-Login-Failed-Count": str(failed_before)},
            )
    reply = await _execute_session(
        request,
        commands,
        "login",
        {
            "email": data.email,
            "password": data.password,
            "captcha_verified": captcha_verified,
            "client_ip": client_ip,
        },
    )
    return Token(access_token=reply.data.access_token)


@router.get("/me", response_model=UserOut)
async def get_me(
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands=Depends(get_account_commands),
):
    data = await execute_account(
        request, credentials, commands, "read_self_account", {}
    )
    return data["account"]


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
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: SessionCommandsPort = Depends(get_session_commands),
):
    await _execute_session(
        request, commands, "logout", {}, token=credentials.credentials
    )


@router.post("/refresh", response_model=Token)
@limiter.limit("5/minute")
async def refresh_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    commands: SessionCommandsPort = Depends(get_session_commands),
):
    """用有效或七天宽限内的 JWT 换取新 token；401 时应重新登录。"""
    reply = await _execute_session(
        request, commands, "refresh", {}, token=credentials.credentials
    )
    return Token(access_token=reply.data.access_token)


@router.post("/logout-all", response_model=Token)
async def logout_all(
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: SessionCommandsPort = Depends(get_session_commands),
):
    reply = await _execute_session(
        request, commands, "logout_all", {}, token=credentials.credentials
    )
    return Token(access_token=reply.data.access_token)
