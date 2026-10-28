"""Per-installation, Native-fenced trusted-empty GPU lifecycle bootstrap."""
from __future__ import annotations

import secrets
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from aap_protocol_v2.lifecycle import (
    BackendResidency,
    AdmissionScope, AdmissionTokenClaims, LifecycleModeRequest, LifecycleResetRequest,
)
from aap_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER


async def _fresh_health(endpoint, backend_transport, request_id):
    challenge = secrets.token_hex(32)
    healthy, metadata = await backend_transport.health_meta(
        endpoint, request_id=request_id, gpu_health_challenge=challenge, timeout_seconds=8.0,
    )
    if not healthy or not isinstance(metadata, Mapping) or metadata.get(GPU_HEALTH_CHALLENGE_ECHO_MARKER) != challenge:
        raise ValueError("GPU bootstrap health challenge failed")
    residency = metadata.get("residency")
    if not isinstance(residency, Mapping):
        raise ValueError("GPU bootstrap residency unavailable")
    return dict(residency)


def _validate_ack(response, request, epoch, phase):
    residency = response.residency
    identity = residency.identity
    if (
        response.ok is not True or response.control_epoch != epoch
        or residency.control_epoch != epoch or residency.boot_id != request.boot_id
        or identity is None or identity.audience != "aap-gpu-lifecycle"
        or identity.backend_registry_id != str(request.registry_id)
        or identity.gpu_resource_id != request.resource_id
        or sorted(residency.pools) != request.pool_ids
        or residency.state.value != "unloaded" or residency.gpu_loaded is not False
        or residency.generation is not None or residency.evictable
        or residency.draining or residency.active_requests != 0
        or residency.builders != 0 or residency.borrowers != 0
        or any(pool.resident is not False for pool in residency.pools.values())
        or residency.lifecycle_gate.value != ("legacy" if phase == "reset" else "enforce")
    ):
        raise ValueError("GPU bootstrap acknowledgement mismatch")
    if phase == "reset" and response.unloaded is not True:
        raise ValueError("GPU bootstrap reset was not unloaded")
    if phase == "mode_enforce" and response.gate.value != "enforce":
        raise ValueError("GPU bootstrap mode was not enforced")


async def bootstrap_gpu_installation(
    request, *, data, signer_factory, endpoint_source, backend_transport, clock,
    readiness_demoter,
):
    """Bind/reset, then enforce, with fresh owner-checked proof before each token.

    Native serializes resource and membership state, control epoch and token
    horizons. The HTTP route carries only signed control, never a workload permit.
    """
    document = request.model_dump(mode="json", exclude={"protocol"})
    request_id = uuid.uuid4()
    endpoint = await endpoint_source.open_owned(
        request.registry_id, request_id=request_id, purpose="health_observation", binding=None,
    )
    if endpoint.url != request.endpoint or endpoint.gpu_resource_id != request.resource_id:
        raise ValueError("GPU bootstrap endpoint changed")
    deadline = clock.monotonic() + 240.0
    for phase in ("reset", "mode_enforce"):
        while True:
            probe_started_at = datetime.fromtimestamp(clock.time(), UTC)
            residency = await _fresh_health(endpoint, backend_transport, request_id)
            expires = datetime.fromtimestamp(clock.time(), UTC) + timedelta(seconds=30)
            async with data.transaction() as transaction:
                result = await transaction.step("installation_control", {
                    "request": document, "phase": phase, "residency": residency,
                    "probe_started_at": probe_started_at.isoformat(),
                    "token_expires_at": expires.isoformat(),
                })
                if result.get("status") == "prepared":
                    await readiness_demoter(request.resource_id)
            if result.get("status") in {"prepared", "acknowledged", "complete"}:
                break
            if result.get("reason") != "control_token_horizon" or clock.monotonic() >= deadline:
                raise ValueError("GPU bootstrap control unavailable")
            await clock.sleep(min(0.25, max(0.0, deadline - clock.monotonic())))
        preparation = result.get("preparation")
        if not isinstance(preparation, dict):
            raise ValueError("GPU bootstrap preparation unavailable")
        epoch = preparation["control_epoch"]
        if result["status"] == "complete":
            break
        if result["status"] == "acknowledged":
            continue
        token_expiry = datetime.fromisoformat(preparation["token_expires_at"].replace("Z", "+00:00"))
        if token_expiry.timestamp() <= clock.time():
            raise ValueError("GPU bootstrap token expired")
        claims = AdmissionTokenClaims(
            backend_registry_id=str(request.registry_id), gpu_resource_id=request.resource_id,
            boot_id=request.boot_id, generation=None, control_epoch=epoch,
            scope=AdmissionScope.RESET if phase == "reset" else AdmissionScope.MODE,
            jti=f"installation:{request.operation_id}:{phase}:{epoch}",
            exp=int(token_expiry.timestamp()), owner=f"installation:{request.operation_id}",
            operation=f"installation:{phase}",
        )
        route_operation = "reset" if phase == "reset" else "mode"
        control_request_id = uuid.uuid4()
        async with backend_transport.control_binding(
            request.registry_id, request_id=control_request_id, operation=route_operation,
        ) as binding:
            target = await endpoint_source.open_owned(
                request.registry_id, request_id=control_request_id, purpose="gpu_control", binding=binding,
            )
            if target.endpoint_sha256 != endpoint.endpoint_sha256:
                raise ValueError("GPU bootstrap endpoint changed")
            token = signer_factory().sign(claims)
            if phase == "reset":
                response = await backend_transport.lifecycle_reset(
                    target, LifecycleResetRequest(control_epoch=epoch), token,
                    request_id=control_request_id, timeout_seconds=8.0,
                )
            else:
                response = await backend_transport.lifecycle_mode(
                    target, LifecycleModeRequest(gate="enforce", control_epoch=epoch), token,
                    request_id=control_request_id, timeout_seconds=8.0,
                )
            _validate_ack(response, request, epoch, phase)
    current = await endpoint_source.open_owned(
        request.registry_id, request_id=request_id, purpose="health_observation", binding=None,
    )
    if current.endpoint_sha256 != endpoint.endpoint_sha256:
        raise ValueError("GPU bootstrap endpoint changed")
    residency = await _fresh_health(current, backend_transport, request_id)
    identity = residency.get("identity")
    if (
        residency.get("boot_id") != request.boot_id or residency.get("control_epoch") != epoch
        or residency.get("lifecycle_gate") != "enforce"
        or not isinstance(identity, Mapping)
        or identity.get("audience") != "aap-gpu-lifecycle"
        or identity.get("backend_registry_id") != str(request.registry_id)
        or identity.get("gpu_resource_id") != request.resource_id
        or not isinstance(residency.get("pools"), Mapping)
        or sorted(residency["pools"]) != request.pool_ids
        or residency.get("draining") is not False
        or residency.get("state") not in {"unloaded", "resident"}
        or type(residency.get("gpu_loaded")) is not bool
        or any(not isinstance(pool, Mapping) or type(pool.get("resident")) is not bool
               for pool in residency["pools"].values())
        or (
            result["status"] != "complete"
            and (residency.get("state") != "unloaded"
                 or residency.get("gpu_loaded") is not False
                 or residency.get("generation") is not None
                 or residency.get("evictable") is not False
                 or residency.get("active_requests") != 0
                 or residency.get("builders") != 0
                 or residency.get("borrowers") != 0
                 or any(pool.get("resident") is not False for pool in residency["pools"].values()))
        )
    ):
        raise ValueError("GPU bootstrap live identity mismatch")
    BackendResidency.model_validate(residency)
    return epoch
