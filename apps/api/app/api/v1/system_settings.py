from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.ratelimit import limiter
from app.db.enums import UserRole
from app.deps import get_db, require_roles
from app.api.v1.recovery_commands import get_registration_default
from app.schemas.me import (
    SmtpStatus,
    SystemSettingsOut,
    SystemSettingsReset,
    SystemSettingsUpdate,
)
from app.services.audit import AuditAction, AuditService
from app.services.email import SmtpConfigError, send_test_email
from app.services.system_settings_service import (
    SettingsVersionConflict,
    SystemSettingsService,
)
from app.services.identity_contracts import IdentityActorFacts

router = APIRouter()


async def _build_response(
    db: AsyncSession, registration_default: bool | None = None
) -> SystemSettingsOut:
    """Build management readback from committed DB state, bypassing TTL cache."""

    snapshot = await SystemSettingsService.snapshot(
        db,
        bypass_cache=True,
        registration_default=registration_default,
    )
    cur = snapshot.values
    smtp_password_set = bool(cur.get("smtp_password"))
    smtp_configured = bool(
        cur.get("smtp_host") and cur.get("smtp_port") and cur.get("smtp_from")
    )
    return SystemSettingsOut(
        environment=settings.environment,
        # Keep every value explicit: false, zero and empty strings are valid
        # effective values and must not be replaced with deployment defaults.
        invitation_ttl_days=cur["invitation_ttl_days"],
        frontend_base_url=cur["frontend_base_url"],
        smtp=SmtpStatus(
            host=cur.get("smtp_host"),
            port=cur.get("smtp_port"),
            user=cur.get("smtp_user"),
            from_address=cur.get("smtp_from"),
            password_set=smtp_password_set,
            configured=smtp_configured,
        ),
        allow_open_registration=cur["allow_open_registration"],
        max_invitations_per_day=cur["max_invitations_per_day"],
        offline_threshold_minutes=cur["offline_threshold_minutes"],
        dataset_import_max_files=cur["dataset_import_max_files"],
        dataset_import_max_total_bytes=cur["dataset_import_max_total_bytes"],
        task_create_sync_threshold=cur["task_create_sync_threshold"],
        video_chunk_warmup_lookahead=cur["video_chunk_warmup_lookahead"],
        version=snapshot.version,
        metadata=SystemSettingsService.metadata(snapshot),
    )


async def _version_conflict_response(
    db: AsyncSession,
    exc: SettingsVersionConflict,
    registration_default: bool | None = None,
) -> HTTPException:
    # The failed transaction must be rolled back before the readback query; the
    # advisory lock is then released and the response contains the winner's
    # effective values for the UI to reconcile.
    await db.rollback()
    latest = await _build_response(db, registration_default)
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": "系统设置已被其他管理员更新，请读取最新值后重试。",
            "expected_version": exc.expected,
            "version": latest.version,
            "settings": latest.model_dump(mode="json"),
        },
    )


@router.get("/system", response_model=SystemSettingsOut)
async def get_system_settings(
    db: AsyncSession = Depends(get_db),
    _: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    registration_default: bool = Depends(get_registration_default),
):
    return await _build_response(db, registration_default)


@router.patch("/system", response_model=SystemSettingsOut)
async def update_system_settings(
    payload: SystemSettingsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    registration_default: bool = Depends(get_registration_default),
):
    keys = (
        "allow_open_registration",
        "invitation_ttl_days",
        "frontend_base_url",
        "smtp_host",
        "smtp_port",
        "smtp_user",
        "smtp_password",
        "smtp_from",
        "max_invitations_per_day",
        "offline_threshold_minutes",
        "dataset_import_max_files",
        "dataset_import_max_total_bytes",
        "task_create_sync_threshold",
        "video_chunk_warmup_lookahead",
    )
    updates = {
        key: getattr(payload, key)
        for key in keys
        if key in payload.model_fields_set and getattr(payload, key) is not None
    }
    if not updates:
        return await _build_response(db, registration_default)

    try:
        changes = await SystemSettingsService.set_many(
            db,
            updates,
            actor.id,
            expected_version=payload.expected_version,
            registration_default=registration_default,
        )
    except SettingsVersionConflict as exc:
        raise await _version_conflict_response(db, exc, registration_default)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if changes:
        await AuditService.log(
            db,
            actor=actor,
            action=AuditAction.SYSTEM_SETTINGS_UPDATE,
            target_type="system",
            target_id="settings",
            request=request,
            status_code=200,
            detail=SystemSettingsService.safe_audit_detail(changes),
        )
    await db.commit()
    return await _build_response(db, registration_default)


@router.post("/system/reset", response_model=SystemSettingsOut)
async def reset_system_settings(
    payload: SystemSettingsReset,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    registration_default: bool = Depends(get_registration_default),
):
    try:
        changes = await SystemSettingsService.reset_many(
            db,
            payload.keys,
            actor.id,
            expected_version=payload.expected_version,
            registration_default=registration_default,
        )
    except SettingsVersionConflict as exc:
        raise await _version_conflict_response(db, exc, registration_default)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    # An explicit reset is auditable even if the requested key already had no
    # override.  Password values are still represented only as changed=True.
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.SYSTEM_SETTINGS_UPDATE,
        target_type="system",
        target_id="settings",
        request=request,
        status_code=200,
        detail={
            "operation": "reset",
            "keys": payload.keys,
            "changes": SystemSettingsService.safe_audit_detail(changes),
        },
    )
    await db.commit()
    return await _build_response(db, registration_default)


@router.post("/system/test-smtp", status_code=200)
@limiter.limit("3/minute")
async def test_smtp(
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    """用当前 DB override 的 SMTP 配置发一封测试邮件到 actor.email。"""
    try:
        result = await send_test_email(db, actor.email)
    except SmtpConfigError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {"ok": True, **result}
