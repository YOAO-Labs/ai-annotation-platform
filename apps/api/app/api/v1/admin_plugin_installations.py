"""Super-admin installation commands for external inference plugins."""

from __future__ import annotations

from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from yoao_inference.gpu.contracts import GPUArbiterDispatchError
from yoao_inference.host_guard import (
    InferenceAllowlistInvalid,
    InferenceHostDenied,
    resolve_pinned_inference_ips,
    validate_inference_plugin_url,
)
from yoao_inference_contract.gpu import GPUInstallationControlRequest
from yoao_inference_contract.backend_control import BackendRequestOnlyEndpoint
from yoao_inference_contract.backend_control_client import BackendControlUnavailable
from yoao_inference_contract.routing import RoutingEpochView, RoutingUnavailable
from yoao_inference_contract.plugin import (
    DirectPluginProbeResponse,
    PluginActivateRequest,
    PluginInstallationCommand,
    PluginInstallationResponse,
    PluginLaunchReceipt,
    PluginQuiesceResponse,
    PreparePluginInstallation,
    RegisterDirectPluginRequest,
)
from yoao_inference_contract.plugin_assets import (
    PluginAssetApprovalState,
    PluginAssetApprovalUpdate,
)
from app.config import settings
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
    PluginLocalGPUCollision,
    PluginObservationRejected,
    is_host_denied_error,
    observe_direct_plugin_endpoint,
    observe_direct_plugin_registration,
    observe_plugin_endpoint,
    probe_plugin_installation,
    validate_manifest_capabilities,
    verify_launch_receipt,
)

router = APIRouter(
    prefix="/admin/ml-integrations/plugins",
    tags=["admin-plugin-installations"],
)


def _is_local_gpu(installation: PluginInstallationResponse) -> bool:
    item = installation.installation
    return item.resource.gpu_resource_id is not None or (
        item.execution_mode != "direct" and item.manifest.resource.device == "cuda"
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
        pool_ids=sorted(installation.installation.manifest.pools),
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


async def _reconcile_routing(registry_id: UUID, routing_id: UUID) -> RoutingEpochView:
    try:
        routing = await RoutingAuthority().execute(
            "reconcile",
            {"registry_id": str(registry_id), "control_id": str(routing_id)},
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
        or routing.registry_id != registry_id
        or routing.control_id != routing_id
        or routing.phase != "completed"
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "plugin_routing_control_incomplete",
            },
        )
    return routing


async def _cleanup_committed_direct_prepared(
    db: AsyncSession,
    topology: InferenceTopologyData,
    admin: IdentityActorFacts,
    request: Request,
    installation_id: UUID,
    expected_revision: int,
) -> None:
    await db.rollback()
    try:
        cancelled = await topology.command_plugin_installation(
            installation_id, expected_revision, "cancel_prepared"
        )
        await _audit(db, admin, request, "cancelled_prepared", cancelled)
        await db.commit()
    except Exception:
        await db.rollback()


@router.post("/direct", response_model=PluginInstallationResponse)
async def register_direct(
    data: RegisterDirectPluginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    try:
        normalized_url = validate_inference_plugin_url(data.url)
        resolve_pinned_inference_ips(
            normalized_url,
            private_host_allowlist=settings.inference_plugin_private_host_allowlist,
            loopback_host_allowlist=settings.inference_plugin_loopback_host_allowlist,
            enforce_private_and_loopback=True,
        )
    except (InferenceHostDenied, InferenceAllowlistInvalid) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "plugin_installation_host_denied",
                "message": str(exc),
            },
        ) from None
    data = data.model_copy(update={"url": normalized_url})
    try:
        BackendRequestOnlyEndpoint(
            url=data.url, auth_method="token", auth_token=data.auth_token
        )
    except (ValueError, ValidationError):
        raise HTTPException(
            status_code=422,
            detail={"error_code": "plugin_installation_invalid_endpoint"},
        ) from None
    installation_id = data.installation_id or uuid4()
    topology = InferenceTopologyData(db)
    try:
        async with db.begin_nested():
            try:
                reserved = await topology.prepare_direct_plugin_installation(
                    installation_id, data
                )
            except ValueError:
                raise _conflict() from None
            evidence = await observe_direct_plugin_registration(db, reserved, data)
    except PluginLocalGPUCollision as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "plugin_direct_gpu_local_collision",
                "message": str(exc),
            },
        ) from None
    except PluginObservationRejected as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "plugin_endpoint_mismatch",
                "message": str(exc),
            },
        ) from None
    except HTTPException:
        raise
    except (InferenceHostDenied, RuntimeError) as exc:
        if is_host_denied_error(exc):
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": "plugin_installation_host_denied",
                    "message": str(exc),
                },
            ) from None
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "plugin_endpoint_unavailable",
                "message": str(exc),
            },
        ) from None
    await _audit(db, admin, request, "prepared", reserved)
    await db.commit()
    routing_id = _operation_id(
        installation_id, "direct", evidence["boot_id"], "bind-routing"
    )
    try:
        await _reconcile_routing(reserved.registry_id, routing_id)
    except HTTPException:
        await _cleanup_committed_direct_prepared(
            db,
            topology,
            admin,
            request,
            installation_id,
            reserved.installation.revision,
        )
        raise
    try:
        result = await topology.command_plugin_installation(
            installation_id, reserved.installation.revision, "activate", evidence
        )
    except ValueError:
        await _cleanup_committed_direct_prepared(
            db,
            topology,
            admin,
            request,
            installation_id,
            reserved.installation.revision,
        )
        raise _conflict() from None
    await _audit(db, admin, request, "direct_registered", result)
    await db.commit()
    return result


@router.post("/installations", response_model=PluginInstallationResponse)
async def prepare(
    data: PreparePluginInstallation,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    data = data.model_copy(update={"url": data.url.rstrip("/")})
    try:
        validate_manifest_capabilities(data.manifest)
    except PluginObservationRejected:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "plugin_installation_invalid_capabilities"},
        ) from None
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


@router.get("/installations", response_model=list[PluginInstallationResponse])
async def list_installations(
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    return await InferenceTopologyData(db).list_plugin_installations()


@router.get(
    "/installations/{installation_id}", response_model=PluginInstallationResponse
)
async def get_installation(
    installation_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    return await _installation(db, installation_id)


@router.post(
    "/installations/{installation_id}/probe",
    response_model=DirectPluginProbeResponse,
)
async def probe_installation(
    installation_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    installation = await _installation(db, installation_id)
    if installation.installation.status == "retired":
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "plugin_installation_retired",
                "message": "Retired plugin installations cannot be probed.",
            },
        )
    try:
        return await probe_plugin_installation(db, installation)
    except PluginObservationRejected as exc:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "plugin_endpoint_mismatch", "message": str(exc)},
        ) from None


@router.get(
    "/installations/{installation_id}/assets", response_model=PluginAssetApprovalState
)
async def get_asset_approval(
    installation_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    result = await InferenceTopologyData(db).get_plugin_asset_approval(installation_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail={"error_code": "plugin_installation_not_found"}
        )
    return result


@router.put(
    "/installations/{installation_id}/assets", response_model=PluginAssetApprovalState
)
async def set_asset_approval(
    installation_id: UUID,
    data: PluginAssetApprovalUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    topology = InferenceTopologyData(db)
    installation = await _installation(db, installation_id)
    model_ids = {item.id for item in installation.installation.manifest.models}
    if any(item.model_id not in model_ids for item in data.descriptor.selections):
        raise HTTPException(
            status_code=422, detail={"error_code": "plugin_asset_model_unknown"}
        )
    try:
        result = await topology.set_plugin_asset_approval(installation_id, data)
    except ValueError:
        raise _conflict() from None
    await AuditService.log(
        db,
        actor=admin,
        action="ml_plugin_installation.assets_updated",
        target_type="ml_backend",
        target_id=str(installation_id),
        request=request,
        status_code=200,
        detail={
            "revision": result["revision"],
            "descriptor_sha256": data.descriptor_sha256,
            "approved": [item.model_dump(mode="json") for item in data.approved],
        },
    )
    await db.commit()
    return result


@router.post(
    "/installations/{installation_id}/activate",
    response_model=PluginInstallationResponse,
)
async def activate(
    installation_id: UUID,
    data: PluginActivateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    topology = InferenceTopologyData(db)
    existing = await _installation(db, installation_id)
    if existing.installation.execution_mode == "direct":
        if data.receipt is not None or data.qualification is not None:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "plugin_direct_receipt_unexpected"},
            )
        if (
            existing.installation.revision != data.expected_revision
            or existing.installation.status != "disabled"
        ):
            raise _conflict()
        try:
            observed = await observe_direct_plugin_endpoint(db, existing)
        except PluginLocalGPUCollision as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "plugin_direct_gpu_local_collision",
                    "message": str(exc),
                },
            ) from None
        except PluginObservationRejected:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "plugin_endpoint_mismatch",
                    "message": "Authenticated endpoint facts do not match the installation.",
                },
            ) from None
        except (InferenceHostDenied, RuntimeError) as exc:
            if is_host_denied_error(exc):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error_code": "plugin_installation_host_denied",
                        "message": str(exc),
                    },
                ) from None
            raise HTTPException(
                status_code=503,
                detail={
                    "error_code": "plugin_endpoint_unavailable",
                    "message": str(exc),
                },
            ) from None
        await db.commit()
        routing_id = _operation_id(
            installation_id, "direct", observed["boot_id"], "bind-routing"
        )
        try:
            await topology.authorize_plugin_routing_replacement(
                installation_id,
                data.expected_revision,
                routing_id,
                endpoint=observed["endpoint"],
                container_id="direct",
                boot_id=observed["boot_id"],
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
        await _reconcile_routing(existing.registry_id, routing_id)
        evidence = {
            "endpoint": observed["endpoint"],
            "boot_id": observed["boot_id"],
            "control_epoch": None,
            "terminal_drain_supported": observed["terminal_drain_supported"],
        }
        try:
            result = await topology.command_plugin_installation(
                installation_id, data.expected_revision, "activate", evidence
            )
        except ValueError:
            raise _conflict() from None
        await _audit(db, admin, request, "activated", result)
        await db.commit()
        return result

    if data.receipt is None:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "plugin_launch_receipt_required"},
        )
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
    await db.commit()
    routing_id = _operation_id(
        installation_id, data.receipt.container_id, evidence["boot_id"], "bind-routing"
    )
    if _is_local_gpu(reserved):
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
    await _reconcile_routing(reserved.registry_id, routing_id)
    if _is_local_gpu(reserved):
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


@router.post(
    "/installations/{installation_id}/disable",
    response_model=PluginInstallationResponse,
)
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


@router.post(
    "/installations/{installation_id}/quiesce", response_model=PluginQuiesceResponse
)
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
    if installation.installation.execution_mode == "direct":
        if not lifecycle:
            raise _conflict()
        try:
            await topology.check_plugin_tracker_retirement(
                installation_id, data.expected_revision
            )
        except ValueError:
            raise _conflict() from None
        prior = lifecycle.get("proof")
        if isinstance(prior, dict) and prior.get("revision") == data.expected_revision:
            await db.commit()
            return PluginQuiesceResponse(
                **installation.model_dump(), safe_to_stop=True, launch=None
            )
        await db.commit()
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
        registry = await topology.registry_facts(installation.registry_id)
        if registry is None:
            raise _conflict()
        activation = lifecycle.get("activation")
        active_boot_id = (
            activation.get("boot_id")
            if isinstance(activation, dict)
            and isinstance(activation.get("boot_id"), str)
            else None
        )
        active_drain_required = bool(
            isinstance(activation, dict)
            and activation.get("terminal_drain_supported") is True
        )
        observed = None
        try:
            observed = await observe_direct_plugin_endpoint(
                db, installation, for_drain=True
            )
        except PluginObservationRejected:
            if active_drain_required:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "plugin_endpoint_mismatch",
                        "message": "Authenticated endpoint facts do not match the installation.",
                    },
                ) from None
            observed = None
        except (InferenceHostDenied, RuntimeError) as exc:
            if active_drain_required:
                if is_host_denied_error(exc):
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error_code": "plugin_installation_host_denied",
                            "message": str(exc),
                        },
                    ) from None
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error_code": "plugin_endpoint_unavailable",
                        "message": str(exc),
                    },
                ) from None
            observed = None
        await db.commit()
        if active_drain_required and (
            observed is None or observed.get("terminal_drain_supported") is not True
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "plugin_terminal_drain_required",
                    "message": "Direct plugin requires live terminal drain proof before quiescence.",
                },
            )
        pool_ids = sorted(installation.installation.manifest.pools)
        if observed is not None and observed.get("terminal_drain_supported") is True:
            boot_id = observed["boot_id"]
            operation_id = _operation_id(installation_id, "direct", boot_id, "quiesce")
            try:
                result = await backend_control_service(db).terminal_drain(
                    installation.registry_id,
                    boot_id=boot_id,
                    operation_id=operation_id,
                )
            except MLBackendQuiescenceError as error:
                raise HTTPException(
                    status_code=503 if error.unavailable else 409,
                    detail=error.as_detail(),
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
                or result["boot_id"] != boot_id
                or result["operation_id"] != str(operation_id)
                or result["control_epoch"] is not None
                or result["generation"] is not None
                or result["pool_ids"] != pool_ids
                or result["unloaded"] is not True
            ):
                raise HTTPException(
                    status_code=503, detail={"error_code": "plugin_control_invalid"}
                )
            evidence = {
                "endpoint": registry.url,
                "boot_id": boot_id,
                "control_epoch": None,
                "generation": None,
                "operation_id": str(operation_id),
                "pool_ids": result["pool_ids"],
                "terminal_drain_supported": True,
            }
        else:
            boot_id = active_boot_id or (
                observed["boot_id"] if observed is not None else "unobserved"
            )
            operation_id = _operation_id(installation_id, "direct", boot_id, "quiesce")
            evidence = {
                "endpoint": registry.url,
                "boot_id": boot_id,
                "control_epoch": None,
                "generation": None,
                "operation_id": str(operation_id),
                "pool_ids": pool_ids,
                "terminal_drain_supported": False,
            }
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
            **finalized.model_dump(), safe_to_stop=True, launch=None
        )

    if not lifecycle or not isinstance(lifecycle.get("launch"), dict):
        raise HTTPException(
            status_code=409, detail={"error_code": "plugin_launch_unrecorded"}
        )
    try:
        await topology.check_plugin_tracker_retirement(
            installation_id, data.expected_revision
        )
    except ValueError:
        raise _conflict() from None
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
    await db.commit()
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
    await db.commit()
    operation_id = _operation_id(
        installation_id, launch.container_id, evidence["boot_id"], "quiesce"
    )
    if _is_local_gpu(installation):
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
            or result["pool_ids"] != sorted(installation.installation.manifest.pools)
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


@router.post(
    "/installations/{installation_id}/retire", response_model=PluginInstallationResponse
)
async def retire(
    installation_id: UUID,
    data: PluginInstallationCommand,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    from app.services import learning_registry_usage

    installation = await _installation(db, installation_id)
    topology = InferenceTopologyData(db)
    if await topology.lock_plugin_registry(installation.registry_id) is None:
        raise HTTPException(
            status_code=404, detail={"error_code": "plugin_installation_not_found"}
        )
    usage = await learning_registry_usage.registry_usage(db, installation.registry_id)
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
    "/installations/{installation_id}/cancel-prepared",
    response_model=PluginInstallationResponse,
)
async def cancel_prepared(
    installation_id: UUID,
    data: PluginInstallationCommand,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    from app.services import learning_registry_usage

    installation = await _installation(db, installation_id)
    topology = InferenceTopologyData(db)
    if await topology.lock_plugin_registry(installation.registry_id) is None:
        raise HTTPException(
            status_code=404, detail={"error_code": "plugin_installation_not_found"}
        )
    usage = await learning_registry_usage.registry_usage(db, installation.registry_id)
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
