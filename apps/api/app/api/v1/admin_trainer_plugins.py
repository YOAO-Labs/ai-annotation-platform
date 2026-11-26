"""Super-admin REST endpoints for registered yoao-trainer/v1 trainer plugins."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.deps import get_db, require_roles
from app.schemas.trainer_plugin import (
    QualifyTrainerPluginRequest,
    RegisterTrainerPluginRequest,
    TrainerPluginRecord,
    TrainerPluginStatus,
    TransitionTrainerPluginRequest,
)
from app.services.audit import AuditService
from app.services.identity_contracts import IdentityActorFacts
from app.services.trainer_plugins import (
    validate_qualification_report,
    validate_registration_payload,
)
from app.services.trainer_plugins_data import (
    TrainerPluginsPostgresData,
    TrainerPluginStoreError,
)

router = APIRouter()


@router.get(
    "/admin/trainer-plugins",
    response_model=list[TrainerPluginRecord],
)
async def list_trainer_plugins(
    include_retired: bool = Query(default=False),
    status: TrainerPluginStatus | None = Query(default=None),
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[TrainerPluginRecord]:
    try:
        return await TrainerPluginsPostgresData(db).list_plugins(
            include_retired=include_retired, status=status
        )
    except TrainerPluginStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from None


@router.get(
    "/admin/trainer-plugins/{plugin_id}",
    response_model=TrainerPluginRecord,
)
async def get_trainer_plugin(
    plugin_id: str,
    _admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> TrainerPluginRecord:
    record = await TrainerPluginsPostgresData(db).get_plugin(plugin_id)
    if record is None:
        raise HTTPException(status_code=404, detail="trainer plugin not found")
    return record


@router.post(
    "/admin/trainer-plugins",
    response_model=TrainerPluginRecord,
    status_code=201,
)
async def register_trainer_plugin(
    body: RegisterTrainerPluginRequest,
    request: Request,
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> TrainerPluginRecord:
    try:
        payload = validate_registration_payload(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    try:
        record = await TrainerPluginsPostgresData(db).register_plugin(payload)
    except TrainerPluginStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from None

    await AuditService.log(
        db,
        actor=admin,
        action="trainer_plugin.register",
        target_type="trainer_plugin",
        target_id=record.plugin_id,
        request=request,
        status_code=201,
        detail={
            "framework_id": record.framework_id,
            "version": record.version,
            "execution_mode": record.execution_mode,
            "source_tier": record.source_tier,
            "status": record.status,
            "capabilities_digest": record.capabilities_digest,
        },
    )
    await db.commit()
    return record


@router.post(
    "/admin/trainer-plugins/{plugin_id}/qualify",
    response_model=TrainerPluginRecord,
)
async def qualify_trainer_plugin(
    plugin_id: str,
    body: QualifyTrainerPluginRequest,
    request: Request,
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> TrainerPluginRecord:
    store = TrainerPluginsPostgresData(db)
    existing = await store.get_plugin(plugin_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="trainer plugin not found")

    try:
        validate_qualification_report(existing, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    try:
        record = await store.qualify_plugin(
            {
                "plugin_id": plugin_id,
                "expected_revision": body.expected_revision,
                "qualification_report_sha256": body.qualification_report_sha256,
                "qualification_report": body.qualification_report,
            }
        )
    except TrainerPluginStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from None

    await AuditService.log(
        db,
        actor=admin,
        action="trainer_plugin.qualify",
        target_type="trainer_plugin",
        target_id=record.plugin_id,
        request=request,
        status_code=200,
        detail={
            "status": record.status,
            "revision": record.revision,
            "qualification_report_sha256": record.qualification_report_sha256,
        },
    )
    await db.commit()
    return record


async def _transition(
    *,
    db: AsyncSession,
    admin: IdentityActorFacts,
    request: Request,
    plugin_id: str,
    expected_revision: int,
    target_status: Literal["enabled", "disabled", "retired"],
    action: str,
) -> TrainerPluginRecord:
    try:
        record = await TrainerPluginsPostgresData(db).transition_plugin(
            {
                "plugin_id": plugin_id,
                "expected_revision": expected_revision,
                "target_status": target_status,
            }
        )
    except TrainerPluginStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from None

    await AuditService.log(
        db,
        actor=admin,
        action=action,
        target_type="trainer_plugin",
        target_id=record.plugin_id,
        request=request,
        status_code=200,
        detail={"status": record.status, "revision": record.revision},
    )
    await db.commit()
    return record


@router.post(
    "/admin/trainer-plugins/{plugin_id}/enable",
    response_model=TrainerPluginRecord,
)
async def enable_trainer_plugin(
    plugin_id: str,
    body: TransitionTrainerPluginRequest,
    request: Request,
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> TrainerPluginRecord:
    return await _transition(
        db=db,
        admin=admin,
        request=request,
        plugin_id=plugin_id,
        expected_revision=body.expected_revision,
        target_status="enabled",
        action="trainer_plugin.enable",
    )


@router.post(
    "/admin/trainer-plugins/{plugin_id}/disable",
    response_model=TrainerPluginRecord,
)
async def disable_trainer_plugin(
    plugin_id: str,
    body: TransitionTrainerPluginRequest,
    request: Request,
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> TrainerPluginRecord:
    return await _transition(
        db=db,
        admin=admin,
        request=request,
        plugin_id=plugin_id,
        expected_revision=body.expected_revision,
        target_status="disabled",
        action="trainer_plugin.disable",
    )


@router.post(
    "/admin/trainer-plugins/{plugin_id}/retire",
    response_model=TrainerPluginRecord,
)
async def retire_trainer_plugin(
    plugin_id: str,
    body: TransitionTrainerPluginRequest,
    request: Request,
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> TrainerPluginRecord:
    return await _transition(
        db=db,
        admin=admin,
        request=request,
        plugin_id=plugin_id,
        expected_revision=body.expected_revision,
        target_status="retired",
        action="trainer_plugin.retire",
    )
