import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.ratelimit import limiter
from app.core.password import validate_password_strength
from app.deps import bearer_scheme, get_db, get_current_principal
from app.schemas.user import Token, LoginRequest, UserOut
from app.schemas.invitation import OpenRegisterRequest, RegisterResponse
from app.services.audit import audit_origin_from_request
from app.middleware.request_id import request_id_var
from app.services.captcha_service import verify_turnstile_token
from app.services import login_failed_counter
from app.services.system_settings_service import SystemSettingsService
from app.services.identity_mail_delivery import kick_credential_mail
from app.services.identity_recovery_commands import RecoveryCommandsPort

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
from app.api.v1.recovery_commands import execute_recovery, get_recovery_commands

router = APIRouter()


class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    # v0.8.7 · Cloudflare Turnstile token；TURNSTILE_ENABLED=False 时忽略。
    captcha_token: str | None = Field(default=None, repr=False)

    @field_validator("email")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return (v or "").strip().lower()


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, repr=False)
    new_password: str = Field(min_length=8, max_length=128, repr=False)

    @field_validator("new_password")
    @classmethod
    def _strength(cls, v: str) -> str:
        errors = validate_password_strength(v)
        if errors:
            raise ValueError("; ".join(errors))
        return v


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=1, repr=False)


class ResendVerificationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)

    @field_validator("email")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return (v or "").strip().lower()


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
    commands: RecoveryCommandsPort = Depends(get_recovery_commands),
):
    if not await verify_turnstile_token(
        data.captcha_token, request.client.host if request.client else None
    ):
        raise HTTPException(status_code=400, detail="captcha_failed")

    operation_id = uuid.uuid4()
    await execute_recovery(
        request,
        commands,
        "request_password_reset",
        {"email": data.email, "captcha_verified": True},
        operation_id=operation_id,
    )
    background_tasks.add_task(kick_credential_mail, operation_id)
    return {
        "message": (
            "如果该邮箱已注册，您将收到一封包含重置链接的邮件。"
            "若未收到，请联系管理员协助重置。"
        )
    }


@router.post("/reset-password")
async def reset_password(
    data: ResetPasswordRequest,
    request: Request,
    commands: RecoveryCommandsPort = Depends(get_recovery_commands),
):
    result = await execute_recovery(
        request,
        commands,
        "consume_password_reset",
        {"token": data.token, "new_password": data.new_password},
    )
    if not result["completed"]:
        raise HTTPException(status_code=400, detail="重置链接无效或已过期")
    return {"message": "密码已重置，请使用新密码登录"}


@router.post("/verify-email")
async def verify_email(
    data: VerifyEmailRequest,
    request: Request,
    commands: RecoveryCommandsPort = Depends(get_recovery_commands),
):
    """在身份所有者事务中单次消费验证链接并记录审计。"""
    result = await execute_recovery(
        request, commands, "consume_email_verification", {"token": data.token}
    )
    if not result["completed"]:
        raise HTTPException(status_code=400, detail="验证链接无效或已过期")
    return {"message": "邮箱已验证，请登录"}


@router.post("/send-verification-email", status_code=202)
@limiter.limit("3/minute")
async def send_verification_email_endpoint(
    data: ResendVerificationRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    commands: RecoveryCommandsPort = Depends(get_recovery_commands),
):
    """持久受理验证邮件投递，响应不等待 SMTP。"""
    operation_id = uuid.uuid4()
    await execute_recovery(
        request,
        commands,
        "resend_email_verification",
        {"email": data.email},
        operation_id=operation_id,
    )
    background_tasks.add_task(kick_credential_mail, operation_id)
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
    background_tasks: BackgroundTasks,
    commands: RecoveryCommandsPort = Depends(get_recovery_commands),
):
    captcha_verified = await verify_turnstile_token(
        payload.captcha_token, request.client.host if request.client else None
    )

    operation_id = uuid.uuid4()
    result = await execute_recovery(
        request,
        commands,
        "register_open",
        {
            "email": payload.email,
            "name": payload.name,
            "password": payload.password,
            "captcha_verified": captcha_verified,
        },
        operation_id=operation_id,
    )
    if result["email_verification_required"]:
        background_tasks.add_task(kick_credential_mail, operation_id)
    return RegisterResponse(
        access_token=result["access_token"],
        user=UserOut.model_validate(result["account"]),
        email_verification_required=result["email_verification_required"],
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
