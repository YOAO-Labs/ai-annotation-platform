"""REST endpoints for external notification channels and user channel bindings."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import (
    BaseModel,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)
import redis.asyncio as aioredis
from redis.asyncio.connection import ConnectionPool
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.enums import UserRole
from app.deps import get_current_user, get_db, require_roles
from app.services.audit import AuditService
from app.services.identity_contracts import IdentityActorFacts
from app.services.notification_channel_delivery import (
    build_channel_http_client,
    channel_encryption_key,
)
from yoao_notifications.channel_contract import (
    ChannelVerifyAddressRequest,
    ExtensionManifestV1,
    validate_channel_id,
    validate_description,
    validate_display_name,
    validate_homepage_url,
    validate_locale,
    validate_recipient_address,
)
from yoao_notifications.channel_crypto import (
    NotificationChannelCryptoError,
    NotificationChannelKeyNotConfigured,
    channel_token_sha256,
    decrypt_channel_token,
    encrypt_channel_token,
    validate_channel_token_plaintext,
)
from yoao_notifications.channel_host_guard import ChannelHostDenied
from yoao_notifications.channel_http_client import ChannelSetupError
from yoao_notifications.channel_store_postgres import (
    NotificationChannelStorePostgresData,
)

router = APIRouter()
log = logging.getLogger(__name__)

_VERIFY_RATE_REDIS_POOL: ConnectionPool | None = None
_VERIFY_RATE_REDIS_POOL_LOOP: asyncio.AbstractEventLoop | None = None


def _get_verify_redis_pool() -> ConnectionPool:
    global _VERIFY_RATE_REDIS_POOL, _VERIFY_RATE_REDIS_POOL_LOOP
    loop = asyncio.get_running_loop()
    if _VERIFY_RATE_REDIS_POOL is None or _VERIFY_RATE_REDIS_POOL_LOOP is not loop:
        _VERIFY_RATE_REDIS_POOL = ConnectionPool.from_url(
            settings.redis_url,
            max_connections=8,
            decode_responses=True,
        )
        _VERIFY_RATE_REDIS_POOL_LOOP = loop
    return _VERIFY_RATE_REDIS_POOL


def _get_verify_redis() -> aioredis.Redis:
    return aioredis.Redis(connection_pool=_get_verify_redis_pool())


async def _enforce_verify_rate_limit(user_id: uuid.UUID, channel_id: str) -> None:
    key = f"notif:ch:verify:{user_id}:{channel_id}"
    limit = settings.notification_channel_verify_rate_limit_per_minute
    client = _get_verify_redis()
    try:
        count = await client.incr(key)
        ttl = await client.ttl(key)
        if count == 1 or ttl == -1:
            await client.expire(key, 60)
            ttl = 60
    except Exception:
        log.warning("verify-address rate limit backend unavailable", exc_info=True)
        raise HTTPException(
            status_code=503, detail="verify-address rate limiter unavailable"
        ) from None
    finally:
        await client.aclose(close_connection_pool=False)
    if count > limit:
        retry_after = str(max(1, int(ttl) if ttl and ttl > 0 else 60))
        raise HTTPException(
            status_code=429,
            detail="verify-address rate limit exceeded",
            headers={"Retry-After": retry_after},
        )


def _encrypt_or_503(plaintext: str) -> tuple[str, str]:
    try:
        validated = validate_channel_token_plaintext(plaintext)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        key = channel_encryption_key()
        return encrypt_channel_token(validated, key), channel_token_sha256(validated)
    except (NotificationChannelKeyNotConfigured, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


def _decrypt_or_503(ciphertext: str | None) -> str:
    try:
        key = channel_encryption_key()
        return decrypt_channel_token(ciphertext or "", key)
    except (
        NotificationChannelKeyNotConfigured,
        NotificationChannelCryptoError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


def _validated_channel_id(channel_id: str) -> str:
    try:
        return validate_channel_id(channel_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _map_transition_error(reply: dict[str, Any]) -> None:
    if reply.get("ok") is True:
        return
    code = reply.get("error")
    if code == "not_found":
        raise HTTPException(status_code=404, detail="Notification channel not found")
    if code == "revision_conflict":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "revision_conflict",
                "current_revision": reply.get("current_revision"),
            },
        )
    if code == "invalid_transition":
        raise HTTPException(
            status_code=409,
            detail="Invalid notification channel status transition",
        )
    raise HTTPException(
        status_code=400, detail=f"Invalid channel update: {code or 'unknown'}"
    )


class NotificationChannelAdminOut(BaseModel):
    id: str
    channel_id: str
    display_name: str
    description: str | None = None
    homepage: str | None = None
    source_tier: Literal["official", "third_party"]
    mode: Literal["direct", "managed"]
    status: Literal["prepared", "active", "disabled", "retired"]
    revision: int
    endpoint_url: str
    auth_token_sha256: str
    protocol_version: str
    capabilities: list[str]
    address_schema: dict[str, Any]
    manifest: dict[str, Any] | None = None
    rate_limit_per_minute: int
    consecutive_auth_errors: int
    created_at: str
    updated_at: str
    disabled_at: str | None = None
    retired_at: str | None = None


class NotificationChannelAdminListOut(BaseModel):
    items: list[NotificationChannelAdminOut]


class NotificationChannelRegisterIn(BaseModel):
    endpoint_url: StrictStr = Field(min_length=1, max_length=2048)
    auth_token: StrictStr = Field(min_length=1, max_length=4096)
    source_tier: Literal["official", "third_party"] = "third_party"
    mode: Literal["direct", "managed"] = "direct"
    status: Literal["prepared", "active"] = "active"
    rate_limit_per_minute: StrictInt = Field(default=60, ge=1, le=600)
    display_name: StrictStr | None = None
    description: StrictStr | None = None
    homepage: StrictStr | None = None
    manifest: ExtensionManifestV1 | None = None

    @field_validator("display_name")
    @classmethod
    def _valid_display_name(cls, value: str | None) -> str | None:
        return None if value is None else validate_display_name(value)

    @field_validator("description")
    @classmethod
    def _valid_description(cls, value: str | None) -> str | None:
        return validate_description(value)

    @field_validator("homepage")
    @classmethod
    def _valid_homepage(cls, value: str | None) -> str | None:
        return validate_homepage_url(value)


class NotificationChannelUpdateIn(BaseModel):
    expected_revision: StrictInt = Field(ge=1)
    display_name: StrictStr | None = None
    description: StrictStr | None = None
    homepage: StrictStr | None = None
    source_tier: Literal["official", "third_party"] | None = None
    endpoint_url: StrictStr | None = Field(default=None, min_length=1, max_length=2048)
    auth_token: StrictStr | None = Field(default=None, min_length=1, max_length=4096)
    rate_limit_per_minute: StrictInt | None = Field(default=None, ge=1, le=600)

    @model_validator(mode="before")
    @classmethod
    def _reject_null_required_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in (
                "display_name",
                "source_tier",
                "endpoint_url",
                "auth_token",
                "rate_limit_per_minute",
            ):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} must not be null")
        return data

    @field_validator("display_name")
    @classmethod
    def _valid_display_name(cls, value: str | None) -> str | None:
        return None if value is None else validate_display_name(value)

    @field_validator("description")
    @classmethod
    def _valid_description(cls, value: str | None) -> str | None:
        return validate_description(value)

    @field_validator("homepage")
    @classmethod
    def _valid_homepage(cls, value: str | None) -> str | None:
        return validate_homepage_url(value)


class NotificationChannelTransitionIn(BaseModel):
    expected_revision: StrictInt = Field(ge=1)


class NotificationChannelProbeOut(BaseModel):
    healthy: bool
    health_detail: dict[str, Any] | None = None
    setup_ok: bool
    protocol_version: str | None = None
    channel_id: str | None = None
    display_name: str | None = None
    capabilities: list[str] | None = None
    error: str | None = None


class UserNotificationChannelOut(BaseModel):
    channel_id: str
    display_name: str
    description: str | None = None
    homepage: str | None = None
    source_tier: Literal["official", "third_party"]
    status: Literal["active", "disabled"]
    capabilities: list[str]
    address_schema: dict[str, Any]
    bound: bool
    address: dict[str, Any] | None = None
    locale: str = "zh-CN"
    verified_at: str | None = None
    updated_at: str | None = None


class UserNotificationChannelListOut(BaseModel):
    items: list[UserNotificationChannelOut]


class UserChannelBindingUpsertIn(BaseModel):
    address: dict[str, Any]
    locale: StrictStr = "zh-CN"

    @field_validator("locale")
    @classmethod
    def _valid_locale(cls, value: str) -> str:
        return validate_locale(value)


class UserNotificationChannelBindingOut(BaseModel):
    channel_id: str
    address: dict[str, Any]
    locale: str
    verified_at: str | None = None
    updated_at: str


class VerifyChannelAddressOut(BaseModel):
    ok: bool = True
    verified_at: str | None = None
    provider_message_id: str | None = None


class DeleteChannelBindingOut(BaseModel):
    ok: bool = True
    deleted: bool


@router.get(
    "/admin/notification-channels",
    response_model=NotificationChannelAdminListOut,
)
async def list_admin_notification_channels(
    include_retired: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    rows = await NotificationChannelStorePostgresData(db).list_channels(
        include_retired=include_retired
    )
    return NotificationChannelAdminListOut(
        items=[NotificationChannelAdminOut.model_validate(row) for row in rows]
    )


@router.post(
    "/admin/notification-channels",
    response_model=NotificationChannelAdminOut,
    status_code=201,
)
async def register_admin_notification_channel(
    data: NotificationChannelRegisterIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    ciphertext, token_sha = _encrypt_or_503(data.auth_token)

    client = build_channel_http_client()
    try:
        setup = await client.setup(
            endpoint_url=data.endpoint_url, auth_token=data.auth_token
        )
    except ChannelHostDenied as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ChannelSetupError as exc:
        status_code = 400 if isinstance(exc.__cause__, ChannelHostDenied) else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from None

    if data.manifest is not None:
        if data.manifest.extension_point != "notification_channel":
            raise HTTPException(
                status_code=422,
                detail="manifest.extension_point must be notification_channel",
            )
        if data.manifest.extension.channel_id != setup.channel_id:
            raise HTTPException(
                status_code=422,
                detail="manifest.extension.channel_id does not match /setup channel_id",
            )

    display_name = (
        data.display_name
        or (data.manifest.display_name if data.manifest else None)
        or setup.display_name
    )
    description = (
        data.description
        if data.description is not None
        else (
            data.manifest.description
            if data.manifest and data.manifest.description is not None
            else setup.description
        )
    )
    homepage = (
        data.homepage
        if data.homepage is not None
        else (
            data.manifest.homepage
            if data.manifest and data.manifest.homepage is not None
            else setup.homepage
        )
    )

    reply = await NotificationChannelStorePostgresData(db).register_channel(
        {
            "channel_id": setup.channel_id,
            "display_name": display_name,
            "description": description,
            "homepage": homepage,
            "source_tier": data.source_tier,
            "mode": data.mode,
            "status": data.status,
            "endpoint_url": data.endpoint_url,
            "auth_token_ciphertext": ciphertext,
            "auth_token_sha256": token_sha,
            "protocol_version": setup.protocol_version,
            "capabilities": list(setup.capabilities),
            "address_schema": setup.address_schema,
            "manifest": (
                data.manifest.model_dump(mode="json")
                if data.manifest is not None
                else None
            ),
            "rate_limit_per_minute": data.rate_limit_per_minute,
        }
    )
    if reply.get("ok") is not True:
        code = reply.get("error")
        if code == "conflict":
            raise HTTPException(
                status_code=409,
                detail=f"Notification channel {setup.channel_id!r} already exists",
            )
        raise HTTPException(
            status_code=400, detail=f"Channel registration rejected: {code}"
        )

    channel = NotificationChannelAdminOut.model_validate(reply["channel"])
    await AuditService.log(
        db,
        actor=admin,
        action="notification_channel.registered",
        target_type="notification_channel",
        target_id=channel.id,
        request=request,
        status_code=201,
        detail={
            "channel_id": channel.channel_id,
            "source_tier": channel.source_tier,
            "mode": channel.mode,
            "status": channel.status,
        },
    )
    await db.commit()
    return channel


@router.get(
    "/admin/notification-channels/{channel_id}",
    response_model=NotificationChannelAdminOut,
)
async def get_admin_notification_channel(
    channel_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    validated = _validated_channel_id(channel_id)
    row = await NotificationChannelStorePostgresData(db).get_channel(validated)
    if row is None:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    return NotificationChannelAdminOut.model_validate(row)


@router.post(
    "/admin/notification-channels/{channel_id}/probe",
    response_model=NotificationChannelProbeOut,
)
async def probe_admin_notification_channel(
    channel_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    validated = _validated_channel_id(channel_id)
    store = NotificationChannelStorePostgresData(db)
    meta = await store.get_channel(validated)
    if meta is None:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    if meta.get("status") == "retired":
        raise HTTPException(status_code=409, detail="Notification channel is retired")

    secret = await store.open_channel_secret(validated)
    if secret is None:
        raise HTTPException(status_code=409, detail="Channel credentials unavailable")
    token = _decrypt_or_503(secret.get("auth_token_ciphertext"))

    client = build_channel_http_client()
    healthy = await client.health(endpoint_url=str(secret["endpoint_url"]))
    health_detail: dict[str, Any] | None = {"status": "ok"} if healthy else None
    setup_ok = False
    error_message: str | None = None
    protocol_version: str | None = None
    probed_channel_id: str | None = None
    probed_display_name: str | None = None
    probed_capabilities: list[str] | None = None

    try:
        setup = await client.setup(
            endpoint_url=str(secret["endpoint_url"]), auth_token=token
        )
        setup_ok = True
        protocol_version = setup.protocol_version
        probed_channel_id = setup.channel_id
        probed_display_name = setup.display_name
        probed_capabilities = list(setup.capabilities)
    except (ChannelHostDenied, ChannelSetupError) as exc:
        error_message = str(exc)

    return NotificationChannelProbeOut(
        healthy=healthy,
        health_detail=health_detail,
        setup_ok=setup_ok,
        protocol_version=protocol_version,
        channel_id=probed_channel_id,
        display_name=probed_display_name,
        capabilities=probed_capabilities,
        error=error_message,
    )


@router.patch(
    "/admin/notification-channels/{channel_id}",
    response_model=NotificationChannelAdminOut,
)
async def update_admin_notification_channel(
    channel_id: str,
    data: NotificationChannelUpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    validated = _validated_channel_id(channel_id)
    store = NotificationChannelStorePostgresData(db)
    existing = await store.get_channel(validated)
    if existing is None:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    if existing.get("status") == "retired":
        raise HTTPException(status_code=409, detail="Notification channel is retired")

    fields_set = data.model_fields_set - {"expected_revision"}
    if not fields_set:
        raise HTTPException(
            status_code=400, detail="At least one update field is required"
        )

    payload: dict[str, Any] = {
        "channel_id": validated,
        "action": "update",
        "expected_revision": data.expected_revision,
    }

    if "endpoint_url" in fields_set or "auth_token" in fields_set:
        target_url = (
            data.endpoint_url
            if data.endpoint_url is not None
            else str(existing["endpoint_url"])
        )
        if data.auth_token is not None:
            ciphertext, token_sha = _encrypt_or_503(data.auth_token)
            probe_token = data.auth_token
            payload["auth_token_ciphertext"] = ciphertext
            payload["auth_token_sha256"] = token_sha
        else:
            secret = await store.open_channel_secret(validated)
            if secret is None:
                raise HTTPException(
                    status_code=409, detail="Channel credentials unavailable"
                )
            probe_token = _decrypt_or_503(secret.get("auth_token_ciphertext"))

        try:
            setup = await build_channel_http_client().setup(
                endpoint_url=target_url, auth_token=probe_token
            )
        except ChannelHostDenied as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except ChannelSetupError as exc:
            status_code = 400 if isinstance(exc.__cause__, ChannelHostDenied) else 502
            raise HTTPException(status_code=status_code, detail=str(exc)) from None

        if setup.channel_id != validated:
            raise HTTPException(
                status_code=422,
                detail="Re-probed /setup channel_id does not match existing channel",
            )
        if data.endpoint_url is not None:
            payload["endpoint_url"] = data.endpoint_url
        payload["capabilities"] = list(setup.capabilities)
        payload["address_schema"] = setup.address_schema

    for key in (
        "display_name",
        "description",
        "homepage",
        "source_tier",
        "rate_limit_per_minute",
    ):
        if key in fields_set:
            payload[key] = getattr(data, key)

    reply = await store.transition_channel(payload)
    _map_transition_error(reply)
    channel = NotificationChannelAdminOut.model_validate(reply["channel"])
    await AuditService.log(
        db,
        actor=admin,
        action="notification_channel.updated",
        target_type="notification_channel",
        target_id=channel.id,
        request=request,
        status_code=200,
        detail={
            "channel_id": channel.channel_id,
            "revision": channel.revision,
            "fields": sorted(fields_set),
        },
    )
    await db.commit()
    return channel


async def _run_status_transition(
    *,
    channel_id: str,
    action: Literal["activate", "disable", "retire"],
    expected_revision: int,
    audit_action: str,
    request: Request,
    db: AsyncSession,
    admin: IdentityActorFacts,
) -> NotificationChannelAdminOut:
    validated = _validated_channel_id(channel_id)
    reply = await NotificationChannelStorePostgresData(db).transition_channel(
        {
            "channel_id": validated,
            "action": action,
            "expected_revision": expected_revision,
        }
    )
    _map_transition_error(reply)
    channel = NotificationChannelAdminOut.model_validate(reply["channel"])
    await AuditService.log(
        db,
        actor=admin,
        action=audit_action,
        target_type="notification_channel",
        target_id=channel.id,
        request=request,
        status_code=200,
        detail={
            "channel_id": channel.channel_id,
            "status": channel.status,
            "revision": channel.revision,
        },
    )
    await db.commit()
    return channel


@router.post(
    "/admin/notification-channels/{channel_id}/enable",
    response_model=NotificationChannelAdminOut,
)
async def enable_admin_notification_channel(
    channel_id: str,
    data: NotificationChannelTransitionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    return await _run_status_transition(
        channel_id=channel_id,
        action="activate",
        expected_revision=data.expected_revision,
        audit_action="notification_channel.enabled",
        request=request,
        db=db,
        admin=admin,
    )


@router.post(
    "/admin/notification-channels/{channel_id}/disable",
    response_model=NotificationChannelAdminOut,
)
async def disable_admin_notification_channel(
    channel_id: str,
    data: NotificationChannelTransitionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    return await _run_status_transition(
        channel_id=channel_id,
        action="disable",
        expected_revision=data.expected_revision,
        audit_action="notification_channel.disabled",
        request=request,
        db=db,
        admin=admin,
    )


@router.post(
    "/admin/notification-channels/{channel_id}/retire",
    response_model=NotificationChannelAdminOut,
)
async def retire_admin_notification_channel(
    channel_id: str,
    data: NotificationChannelTransitionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    return await _run_status_transition(
        channel_id=channel_id,
        action="retire",
        expected_revision=data.expected_revision,
        audit_action="notification_channel.retired",
        request=request,
        db=db,
        admin=admin,
    )


@router.get(
    "/notification-channels",
    response_model=UserNotificationChannelListOut,
)
async def list_user_notification_channels(
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    rows = await NotificationChannelStorePostgresData(db).list_user_bindings(user.id)
    return UserNotificationChannelListOut(
        items=[UserNotificationChannelOut.model_validate(row) for row in rows]
    )


@router.put(
    "/notification-channels/{channel_id}/binding",
    response_model=UserNotificationChannelBindingOut,
)
async def upsert_user_notification_channel_binding(
    channel_id: str,
    data: UserChannelBindingUpsertIn,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    validated = _validated_channel_id(channel_id)
    store = NotificationChannelStorePostgresData(db)
    channel = await store.get_channel(validated)
    if channel is None or channel.get("status") in {"prepared", "retired"}:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    if channel.get("status") != "active":
        raise HTTPException(
            status_code=409, detail="Notification channel is not active"
        )
    try:
        validate_recipient_address(data.address, channel["address_schema"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    reply = await store.upsert_user_binding(
        {
            "user_id": str(user.id),
            "channel_id": validated,
            "address": data.address,
            "locale": data.locale,
        }
    )
    if reply.get("ok") is not True:
        code = reply.get("error")
        if code == "not_found":
            raise HTTPException(
                status_code=404, detail="Notification channel not found"
            )
        if code == "channel_not_active":
            raise HTTPException(
                status_code=409, detail="Notification channel is not active"
            )
        raise HTTPException(status_code=422, detail=f"Invalid binding: {code}")

    await db.commit()
    return UserNotificationChannelBindingOut.model_validate(reply["binding"])


@router.post(
    "/notification-channels/{channel_id}/verify-address",
    response_model=VerifyChannelAddressOut,
)
async def verify_user_notification_channel_address(
    channel_id: str,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    validated = _validated_channel_id(channel_id)
    store = NotificationChannelStorePostgresData(db)
    secret = await store.open_channel_secret(validated)
    if secret is None or secret.get("status") in {"prepared", "retired"}:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    if secret.get("status") != "active":
        raise HTTPException(
            status_code=409, detail="Notification channel is not active"
        )
    capabilities = secret.get("capabilities") or []
    if "verify_address" not in capabilities:
        raise HTTPException(
            status_code=400,
            detail="Notification channel does not support verify_address",
        )

    bindings = await store.list_user_bindings(user.id)
    current = next(
        (item for item in bindings if item.get("channel_id") == validated), None
    )
    if (
        current is None
        or not current.get("bound")
        or not isinstance(current.get("address"), dict)
    ):
        raise HTTPException(
            status_code=404,
            detail="Bind an address before verifying this channel",
        )

    try:
        validate_recipient_address(current["address"], secret["address_schema"])
        req = ChannelVerifyAddressRequest(
            channel_id=validated,
            verification_id=uuid.uuid4(),
            recipient_address=current["address"],
            locale=str(current.get("locale") or "zh-CN"),
        )
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    await _enforce_verify_rate_limit(user.id, validated)
    token = _decrypt_or_503(secret.get("auth_token_ciphertext"))

    outcome = await build_channel_http_client().verify_address(
        endpoint_url=str(secret["endpoint_url"]),
        auth_token=token,
        request=req,
    )
    if outcome.outcome == "delivered":
        verified = await store.mark_user_binding_verified(user.id, validated)
        if verified.get("ok") is not True:
            raise HTTPException(
                status_code=404,
                detail="Channel binding was removed during verification",
            )
        await db.commit()
        return VerifyChannelAddressOut(
            ok=True,
            verified_at=verified.get("verified_at"),
            provider_message_id=outcome.provider_message_id,
        )
    if outcome.outcome == "permanent":
        raise HTTPException(
            status_code=422,
            detail=outcome.reason or "Recipient address rejected by channel",
        )
    headers = (
        {"Retry-After": str(outcome.retry_after_seconds)}
        if outcome.retry_after_seconds is not None
        else None
    )
    raise HTTPException(
        status_code=503,
        detail=outcome.reason or "Notification channel verification unavailable",
        headers=headers,
    )


@router.delete(
    "/notification-channels/{channel_id}/binding",
    response_model=DeleteChannelBindingOut,
)
async def delete_user_notification_channel_binding(
    channel_id: str,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    validated = _validated_channel_id(channel_id)
    deleted = await NotificationChannelStorePostgresData(db).delete_user_binding(
        user.id, validated
    )
    await db.commit()
    return DeleteChannelBindingOut(ok=True, deleted=deleted)
