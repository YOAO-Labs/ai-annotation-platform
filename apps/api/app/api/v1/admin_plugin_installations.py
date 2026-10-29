"""Super-admin installation commands for external inference plugins."""

from __future__ import annotations

from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_inference.gpu.contracts import GPUArbiterDispatchError
from aap_inference_contract.gpu import GPUInstallationControlRequest
from aap_inference_contract.backend_control import BackendRequestOnlyEndpoint
from aap_inference_contract.backend_control_client import BackendControlUnavailable
from aap_inference_contract.routing import RoutingEpochView, RoutingUnavailable
from aap_inference_contract.plugin import (
    PluginActivateRequest,
    PluginInstallationCommand,
    PluginInstallationResponse,
    PluginLaunchReceipt,
    PluginQuiesceResponse,
    PreparePluginInstallation,
)
from app.db.enums import UserRole
from app.deps import get_db, require_roles
from app.schemas.ml_backend import _validate_ml_backend_url
from app.services.audit import AuditService
from app.services.identity_contracts import IdentityActorFacts
from app.services.inference_backend_control import (
    BackendControlOperationError,
    backend_control_service,
)
from app.services.inference_gpu import gpu_authority_client
from app.services.inference_topology_data import InferenceTopologyData
from app.services.ml_routing.authority import RoutingAuthority, RoutingRejected
from app.services.ml_routing.contracts import MLBackendQuiescenceError
from app.services.plugin_installation_observation import (
    PluginObservationRejected,
    observe_plugin_endpoint,
    verify_launch_receipt,
)

router = APIRouter(
    prefix="/admin/ml-integrations/plugins/installations",
    tags=["admin-plugin-installations"],
)


def _conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error_code": "plugin_installation_conflict",
            "message": "Installation state or revision changed; read the installation and retry.",
        },
    )


def _operation_id(
    installation_id: UUID, container_id: str, boot_id: str, action: str
) -> UUID:
    return uuid5(installation_id, f"plugin:{action}:{container_id}:{boot_id}")


def _gpu_control_request(installation, launch, evidence, action):
    return GPUInstallationControlRequest(
        registry_id=installation.registry_id,
        resource_id=installation.installation.resource.gpu_resource_id,
        endpoint=evidence["endpoint"],
        boot_id=evidence["boot_id"],
        operation_id=_operation_id(
            installation.installation.installation_id,
            launch.container_id,
            evidence["boot_id"],
            action,
        ),
        container_id=launch.container_id,
        pool_ids=["models"],
    )


def _gpu_error(error: GPUArbiterDispatchError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail=error.detail,
        headers=error.headers or None,
    )


async def _installation(
    db: AsyncSession, installation_id: UUID
) -> PluginInstallationResponse:
    result = await InferenceTopologyData(db).get_plugin_installation(installation_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail={"error_code": "plugin_installation_not_found"}
        )
    return result


async def _audit(db, admin, request, action, result):
    await AuditService.log(
        db,
        actor=admin,
        action=f"ml_plugin_installation.{action}",
        target_type="ml_backend",
        target_id=str(result.registry_id),
        request=request,
        status_code=200,
        detail={
            "installation_id": str(result.installation.installation_id),
            "revision": result.installation.revision,
        },
    )


@router.post("", response_model=PluginInstallationResponse)
async def prepare(
    data: PreparePluginInstallation,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    data = data.model_copy(update={"url": data.url.rstrip("/")})
    try:
        _validate_ml_backend_url(data.url)
        BackendRequestOnlyEndpoint(
            url=data.url, auth_method="token", auth_token=data.auth_token
        )
    except (ValueError, ValidationError):
        raise HTTPException(
            status_code=422,
            detail={"error_code": "plugin_installation_invalid_endpoint"},
        ) from None
    try:
        result = await InferenceTopologyData(db).prepare_plugin_installation(data)
    except ValueError:
        raise _conflict() from None
    await _audit(db, admin, request, "prepared", result)
    await db.commit()
    return result


@router.get("", response_model=list[PluginInstallationResponse])
async def list_installations(
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    return await InferenceTopologyData(db).list_plugin_installations()


@router.get("/{installation_id}", response_model=PluginInstallationResponse)
async def get_installation(
    installation_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    return await _installation(db, installation_id)


@router.post("/{installation_id}/activate", response_model=PluginInstallationResponse)
async def activate(
    installation_id: UUID,
    data: PluginActivateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    topology = InferenceTopologyData(db)
    existing = await _installation(db, installation_id)
    try:
        verify_launch_receipt(existing, data.receipt)
        reserved = await topology.command_plugin_installation(
            installation_id,
            data.expected_revision,
            "record_launch",
            data.receipt.model_dump(mode="json"),
        )
    except (ValueError, PluginObservationRejected):
        raise _conflict() from None
    # Persist the inspected launch before probing: failed activation must remain
    # recoverable and cannot authorize an unrecorded container stop.
    await _audit(db, admin, request, "launch_recorded", reserved)
    await db.commit()
    try:
        evidence = await observe_plugin_endpoint(
            db, reserved, data.receipt, data.qualification
        )
    except PluginObservationRejected:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "plugin_endpoint_mismatch",
                "message": "Authenticated endpoint facts do not match the installation.",
            },
        ) from None
    if data.qualification is not None:
        try:
            reserved = await topology.command_plugin_installation(
                installation_id,
                reserved.installation.revision,
                "approve_qualification",
                data.qualification.model_dump(mode="json"),
            )
        except ValueError:
            raise _conflict() from None
        await _audit(db, admin, request, "qualification_approved", reserved)
    # Both owners must see the committed reservation without a Registry
    # transaction remaining open during authenticated backend control.
    await db.commit()
    routing_id = _operation_id(
        installation_id, data.receipt.container_id, evidence["boot_id"], "bind-routing"
    )
    if reserved.installation.manifest.resource.device == "cuda":
        try:
            await topology.authorize_plugin_routing_replacement(
                installation_id,
                reserved.installation.revision,
                routing_id,
                endpoint=evidence["endpoint"],
                container_id=data.receipt.container_id,
                boot_id=evidence["boot_id"],
            )
        except ValueError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "plugin_routing_replacement_rejected",
                    "reason": str(error),
                },
            ) from None
        await db.commit()
    try:
        routing = await RoutingAuthority().execute(
            "reconcile",
            {"registry_id": str(reserved.registry_id), "control_id": str(routing_id)},
            request_id=routing_id,
        )
    except RoutingRejected as error:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "plugin_routing_control_rejected",
                "reason": error.reason,
            },
        ) from None
    except RoutingUnavailable:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "plugin_routing_control_unavailable",
            },
            headers={"Retry-After": "1"},
        ) from None
    if (
        not isinstance(routing, RoutingEpochView)
        or routing.registry_id != reserved.registry_id
        or routing.control_id != routing_id
        or routing.phase != "completed"
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "plugin_routing_control_incomplete",
            },
        )
    if reserved.installation.manifest.resource.device == "cuda":
        control = _gpu_control_request(reserved, data.receipt, evidence, "activate")
        try:
            reply = await gpu_authority_client().activate_installation(control)
        except GPUArbiterDispatchError as error:
            raise _gpu_error(error) from None
        if (
            reply.registry_id != control.registry_id
            or reply.resource_id != control.resource_id
            or reply.endpoint != control.endpoint
            or reply.boot_id != control.boot_id
            or list(reply.pool_ids) != control.pool_ids
        ):
            raise HTTPException(
                status_code=503, detail={"error_code": "plugin_control_invalid"}
            )
        evidence["control_epoch"] = reply.control_epoch
    try:
        result = await topology.command_plugin_installation(
            installation_id, reserved.installation.revision, "activate", evidence
        )
    except ValueError:
        raise _conflict() from None
    await _audit(db, admin, request, "activated", result)
    await db.commit()
    return result


@router.post("/{installation_id}/disable", response_model=PluginInstallationResponse)
async def disable(
    installation_id: UUID,
    data: PluginInstallationCommand,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    await _installation(db, installation_id)
    try:
        result = await InferenceTopologyData(db).command_plugin_installation(
            installation_id, data.expected_revision, "disable"
        )
    except ValueError:
        raise _conflict() from None
    await _audit(db, admin, request, "disabled", result)
    await db.commit()
    return result


@router.post("/{installation_id}/quiesce", response_model=PluginQuiesceResponse)
async def quiesce(
    installation_id: UUID,
    data: PluginInstallationCommand,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    topology = InferenceTopologyData(db)
    installation = await _installation(db, installation_id)
    if (
        installation.installation.revision != data.expected_revision
        or installation.installation.status not in ("prepared", "disabled")
    ):
        raise _conflict()
    lifecycle = await topology.get_plugin_installation_evidence(installation_id)
    if not lifecycle or not isinstance(lifecycle.get("launch"), dict):
        raise HTTPException(
            status_code=409, detail={"error_code": "plugin_launch_unrecorded"}
        )
    prior = lifecycle.get("proof")
    if isinstance(prior, dict) and prior.get("revision") == data.expected_revision:
        try:
            launch = PluginLaunchReceipt.model_validate(lifecycle["launch"])
            if {k: prior.get(k) for k in launch.model_fields} != launch.model_dump(
                mode="json"
            ):
                raise ValueError("proof does not bind recorded launch")
        except ValueError:
            raise _conflict() from None
        await db.commit()
        return PluginQuiesceResponse(
            **installation.model_dump(), safe_to_stop=True, launch=launch
        )
    pool = await topology.pool_for_registry(installation.registry_id)
    if pool is not None:
        member = await topology.drain_member(pool.id, installation.registry_id)
        if member is None:
            raise _conflict()
        await AuditService.log(
            db,
            actor=admin,
            action="ml_service_pool.member_drained",
            target_type="ml_service_pool",
            target_id=str(pool.id),
            request=request,
            status_code=200,
            detail={"registry_id": str(installation.registry_id)},
        )
        await db.commit()
    try:
        launch = PluginLaunchReceipt.model_validate(lifecycle["launch"])
        evidence = await observe_plugin_endpoint(
            db, installation, launch, for_drain=True
        )
    except (ValueError, PluginObservationRejected):
        raise HTTPException(
            status_code=409, detail={"error_code": "plugin_endpoint_mismatch"}
        ) from None
    # The long drain is fenced by the recorded launch and final revision CAS,
    # never by a Registry row lock held over remote I/O.
    await db.commit()
    operation_id = _operation_id(
        installation_id, launch.container_id, evidence["boot_id"], "quiesce"
    )
    if installation.installation.manifest.resource.device == "cuda":
        control = _gpu_control_request(installation, launch, evidence, "quiesce")
        try:
            reply = await gpu_authority_client().quiesce_installation(control)
        except GPUArbiterDispatchError as error:
            raise _gpu_error(error) from None
        if (
            reply.registry_id != control.registry_id
            or reply.resource_id != control.resource_id
            or reply.endpoint != control.endpoint
            or reply.boot_id != control.boot_id
            or reply.operation_id != control.operation_id
            or reply.container_id != control.container_id
            or list(reply.pool_ids) != control.pool_ids
            or reply.unloaded is not True
        ):
            raise HTTPException(
                status_code=503, detail={"error_code": "plugin_control_invalid"}
            )
        evidence.update(
            control_epoch=reply.control_epoch,
            generation=reply.generation,
            operation_id=str(reply.operation_id),
            pool_ids=list(reply.pool_ids),
        )
    else:
        try:
            result = await backend_control_service(db).terminal_drain(
                installation.registry_id,
                boot_id=evidence["boot_id"],
                operation_id=operation_id,
            )
        except MLBackendQuiescenceError as error:
            raise HTTPException(
                status_code=503 if error.unavailable else 409, detail=error.as_detail()
            ) from None
        except BackendControlOperationError as error:
            failure = error.failure
            raise HTTPException(
                status_code=failure.status_code or 502,
                detail=failure.detail or failure.code,
                headers={"Retry-After": failure.retry_after}
                if failure.retry_after
                else None,
            ) from None
        except BackendControlUnavailable:
            raise HTTPException(
                status_code=503,
                detail={"error_code": "inference_backend_control_unavailable"},
            ) from None
        if (
            type(result) is not dict
            or set(result)
            != {
                "registry_id",
                "boot_id",
                "operation_id",
                "control_epoch",
                "generation",
                "pool_ids",
                "unloaded",
            }
            or result["registry_id"] != str(installation.registry_id)
            or result["boot_id"] != evidence["boot_id"]
            or result["operation_id"] != str(operation_id)
            or result["control_epoch"] is not None
            or result["generation"] is not None
            or result["pool_ids"] != ["models"]
            or result["unloaded"] is not True
        ):
            raise HTTPException(
                status_code=503, detail={"error_code": "plugin_control_invalid"}
            )
        evidence.update(
            control_epoch=None,
            generation=None,
            operation_id=str(operation_id),
            pool_ids=result["pool_ids"],
        )
    try:
        finalized = await topology.command_plugin_installation(
            installation_id,
            data.expected_revision,
            "quiesce",
            evidence,
        )
    except ValueError:
        raise _conflict() from None
    await _audit(db, admin, request, "quiesced", finalized)
    await db.commit()
    return PluginQuiesceResponse(
        **finalized.model_dump(), safe_to_stop=True, launch=launch
    )


@router.post("/{installation_id}/retire", response_model=PluginInstallationResponse)
async def retire(
    installation_id: UUID,
    data: PluginInstallationCommand,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    from app.services import trained_models

    installation = await _installation(db, installation_id)
    topology = InferenceTopologyData(db)
    if await topology.lock_plugin_registry(installation.registry_id) is None:
        raise HTTPException(
            status_code=404, detail={"error_code": "plugin_installation_not_found"}
        )
    usage = await trained_models.registry_usage(db, installation.registry_id)
    if usage.active_releases or usage.frozen_executions:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "plugin_installation_in_use",
                "active_releases": usage.active_releases,
                "frozen_executions": usage.frozen_executions,
            },
        )
    try:
        result = await topology.command_plugin_installation(
            installation_id, data.expected_revision, "retire"
        )
    except ValueError:
        raise _conflict() from None
    await _audit(db, admin, request, "retired", result)
    await db.commit()
    return result


@router.post(
    "/{installation_id}/cancel-prepared", response_model=PluginInstallationResponse
)
async def cancel_prepared(
    installation_id: UUID,
    data: PluginInstallationCommand,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    from app.services import trained_models

    installation = await _installation(db, installation_id)
    topology = InferenceTopologyData(db)
    if await topology.lock_plugin_registry(installation.registry_id) is None:
        raise HTTPException(
            status_code=404, detail={"error_code": "plugin_installation_not_found"}
        )
    usage = await trained_models.registry_usage(db, installation.registry_id)
    if usage.active_releases or usage.frozen_executions:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "plugin_installation_in_use",
                "active_releases": usage.active_releases,
                "frozen_executions": usage.frozen_executions,
            },
        )
    try:
        result = await topology.command_plugin_installation(
            installation_id, data.expected_revision, "cancel_prepared"
        )
    except ValueError:
        raise _conflict() from None
    await _audit(db, admin, request, "cancelled_prepared", result)
    await db.commit()
    return result
