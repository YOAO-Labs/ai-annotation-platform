"""Super-admin REST endpoints for external format adapter plugins."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from yoao_labeling.format_adapter_contract import (
    FormatAdapterManifestV1,
    FormatAdapterPluginRecord,
    validate_description,
    validate_display_name,
    validate_format_id,
    validate_homepage_url,
    validate_manifest_matches_setup,
)
from yoao_labeling.format_adapter_security import (
    FormatAdapterCryptoError,
    FormatAdapterHostDenied,
    FormatAdapterHttpError,
    FormatAdapterKeyNotConfigured,
    adapter_token_sha256,
    decrypt_adapter_token,
    encrypt_adapter_token,
    validate_adapter_endpoint_url,
    validate_adapter_token_plaintext,
)
from yoao_labeling.format_adapter_store_postgres import (
    FormatAdapterStoreError,
    FormatAdapterStorePostgresData,
)

from app.config import settings
from app.db.enums import UserRole
from app.deps import get_db, require_roles
from app.services.audit import AuditService
from app.services.identity_contracts import IdentityActorFacts
from app.services.mask_formats.dynamic_registry import (
    build_format_adapter_http_client,
    format_adapter_encryption_key,
)

router = APIRouter()


def _encrypt_or_503(plaintext: str) -> tuple[str, str]:
    try:
        validated = validate_adapter_token_plaintext(plaintext)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        key = format_adapter_encryption_key()
        return encrypt_adapter_token(validated, key), adapter_token_sha256(validated)
    except (FormatAdapterKeyNotConfigured, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


def _decrypt_or_503(ciphertext: str | None) -> str:
    try:
        return decrypt_adapter_token(ciphertext or "", format_adapter_encryption_key())
    except (FormatAdapterKeyNotConfigured, FormatAdapterCryptoError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


class RegisterFormatAdapterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_url: StrictStr = Field(min_length=1, max_length=512)
    auth_token: StrictStr = Field(min_length=1, max_length=4096)
    mode: Literal["direct", "managed"] = "direct"
    source_tier: Literal["official", "third_party"] = "third_party"
    activate: bool = True
    manifest: dict[str, Any] | None = None
    display_name: StrictStr | None = None
    description: StrictStr | None = None
    homepage: StrictStr | None = None


class TransitionFormatAdapterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(ge=1)


class FormatAdapterProbeResponse(BaseModel):
    format_id: str
    healthy: bool
    setup_ok: bool
    protocol_version: str | None = None
    adapter_version: str | None = None
    manifest_version: str | None = None
    media_types: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    detail: str | None = None


@router.get(
    "/admin/format-adapters",
    response_model=list[FormatAdapterPluginRecord],
)
async def list_format_adapter_plugins(
    include_retired: bool = Query(default=False),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[FormatAdapterPluginRecord]:
    return await FormatAdapterStorePostgresData(db).list_plugins(
        include_retired=include_retired
    )


@router.get(
    "/admin/format-adapters/{format_id}",
    response_model=FormatAdapterPluginRecord,
)
async def get_format_adapter_plugin(
    format_id: str,
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> FormatAdapterPluginRecord:
    record = await FormatAdapterStorePostgresData(db).get_plugin(format_id)
    if record is None:
        raise HTTPException(status_code=404, detail="format adapter plugin not found")
    return record


@router.post(
    "/admin/format-adapters",
    response_model=FormatAdapterPluginRecord,
    status_code=201,
)
async def register_format_adapter_plugin(
    body: RegisterFormatAdapterRequest,
    request: Request,
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> FormatAdapterPluginRecord:
    parsed_manifest: FormatAdapterManifestV1 | None = None
    if body.mode == "managed":
        if body.manifest is None:
            raise HTTPException(
                status_code=422, detail="managed mode requires manifest"
            )
        try:
            parsed_manifest = FormatAdapterManifestV1.model_validate(body.manifest)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if parsed_manifest.artifact is None:
            raise HTTPException(
                status_code=422,
                detail="managed manifest must bind an @sha256: artifact digest",
            )
    elif body.manifest is not None:
        try:
            parsed_manifest = FormatAdapterManifestV1.model_validate(body.manifest)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    try:
        validate_adapter_endpoint_url(
            body.endpoint_url,
            host_allowlist=settings.format_adapter_host_allowlist,
            allow_insecure_http_hosts=settings.format_adapter_allow_insecure_http_hosts,
        )
    except FormatAdapterHostDenied as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    ciphertext, token_sha256 = _encrypt_or_503(body.auth_token)
    client = build_format_adapter_http_client()
    try:
        setup = await client.fetch_setup(body.endpoint_url, body.auth_token)
    except FormatAdapterHostDenied as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except FormatAdapterHttpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None

    if parsed_manifest is not None:
        try:
            validate_manifest_matches_setup(parsed_manifest, setup)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    try:
        validate_format_id(setup.format_id)
        display_name = validate_display_name(
            body.display_name
            or (parsed_manifest.display_name if parsed_manifest else None)
            or setup.display_name
        )
        description = validate_description(
            body.description
            if body.description is not None
            else (
                parsed_manifest.description
                if parsed_manifest and parsed_manifest.description is not None
                else setup.description
            )
        )
        homepage = validate_homepage_url(
            body.homepage
            if body.homepage is not None
            else (
                parsed_manifest.homepage
                if parsed_manifest and parsed_manifest.homepage is not None
                else setup.homepage
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    try:
        record = await FormatAdapterStorePostgresData(db).register_plugin(
            {
                "format_id": setup.format_id,
                "display_name": display_name,
                "description": description,
                "homepage": homepage,
                "source_tier": body.source_tier,
                "mode": body.mode,
                "status": "active" if body.activate else "prepared",
                "endpoint_url": body.endpoint_url,
                "auth_token_ciphertext": ciphertext,
                "auth_token_sha256": token_sha256,
                "protocol_version": setup.protocol_version,
                "adapter_version": setup.adapter_version,
                "manifest_version": setup.manifest_version,
                "media_types": setup.media_types,
                "capabilities": setup.capabilities,
                "option_schema": setup.option_schema,
                "manifest": parsed_manifest.model_dump(mode="json")
                if parsed_manifest is not None
                else None,
            }
        )
    except FormatAdapterStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from None

    await AuditService.log(
        db,
        actor=admin,
        action="format_adapter.register",
        target_type="format_adapter_plugin",
        target_id=record.format_id,
        request=request,
        status_code=201,
        detail={
            "mode": record.mode,
            "source_tier": record.source_tier,
            "status": record.status,
            "adapter_version": record.adapter_version,
        },
    )
    await db.commit()
    return record


@router.post(
    "/admin/format-adapters/{format_id}/probe",
    response_model=FormatAdapterProbeResponse,
)
async def probe_format_adapter_plugin(
    format_id: str,
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> FormatAdapterProbeResponse:
    secret = await FormatAdapterStorePostgresData(db).open_secret(format_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="format adapter plugin not found")
    if secret.status == "retired":
        raise HTTPException(
            status_code=409, detail="retired format adapter plugin cannot be probed"
        )
    auth_token = _decrypt_or_503(secret.auth_token_ciphertext)
    client = build_format_adapter_http_client()
    healthy = False
    try:
        healthy = await client.check_health(secret.endpoint_url)
        setup = await client.fetch_setup(secret.endpoint_url, auth_token)
        return FormatAdapterProbeResponse(
            format_id=format_id,
            healthy=healthy,
            setup_ok=True,
            protocol_version=setup.protocol_version,
            adapter_version=setup.adapter_version,
            manifest_version=setup.manifest_version,
            media_types=setup.media_types,
            capabilities=setup.capabilities,
        )
    except (FormatAdapterHostDenied, FormatAdapterHttpError) as exc:
        return FormatAdapterProbeResponse(
            format_id=format_id,
            healthy=healthy,
            setup_ok=False,
            detail=str(exc),
        )


async def _transition(
    *,
    db: AsyncSession,
    admin: IdentityActorFacts,
    request: Request,
    format_id: str,
    expected_revision: int,
    target_status: Literal["active", "disabled", "retired"],
    action: str,
) -> FormatAdapterPluginRecord:
    try:
        record = await FormatAdapterStorePostgresData(db).transition_plugin(
            {
                "format_id": format_id,
                "expected_revision": expected_revision,
                "target_status": target_status,
            }
        )
    except FormatAdapterStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from None
    await AuditService.log(
        db,
        actor=admin,
        action=action,
        target_type="format_adapter_plugin",
        target_id=record.format_id,
        request=request,
        status_code=200,
        detail={"status": record.status, "revision": record.revision},
    )
    await db.commit()
    return record


@router.post(
    "/admin/format-adapters/{format_id}/activate",
    response_model=FormatAdapterPluginRecord,
)
async def activate_format_adapter_plugin(
    format_id: str,
    body: TransitionFormatAdapterRequest,
    request: Request,
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> FormatAdapterPluginRecord:
    return await _transition(
        db=db,
        admin=admin,
        request=request,
        format_id=format_id,
        expected_revision=body.expected_revision,
        target_status="active",
        action="format_adapter.activate",
    )


@router.post(
    "/admin/format-adapters/{format_id}/disable",
    response_model=FormatAdapterPluginRecord,
)
async def disable_format_adapter_plugin(
    format_id: str,
    body: TransitionFormatAdapterRequest,
    request: Request,
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> FormatAdapterPluginRecord:
    return await _transition(
        db=db,
        admin=admin,
        request=request,
        format_id=format_id,
        expected_revision=body.expected_revision,
        target_status="disabled",
        action="format_adapter.disable",
    )


@router.post(
    "/admin/format-adapters/{format_id}/retire",
    response_model=FormatAdapterPluginRecord,
)
async def retire_format_adapter_plugin(
    format_id: str,
    body: TransitionFormatAdapterRequest,
    request: Request,
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> FormatAdapterPluginRecord:
    return await _transition(
        db=db,
        admin=admin,
        request=request,
        format_id=format_id,
        expected_revision=body.expected_revision,
        target_status="retired",
        action="format_adapter.retire",
    )
