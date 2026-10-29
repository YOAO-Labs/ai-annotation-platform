"""Signed, boot-bound activation of an installation's existing GPU membership."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Mapping

from aap_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER
from aap_inference.gpu.contracts import GPUArbiterDispatchError, GPUArbiterErrorCode
from aap_inference.gpu.membership_activation import promote_gpu_backend_membership
from aap_inference.gpu.installation_bootstrap import bootstrap_gpu_installation
from aap_inference_contract.gpu import (
    GPUInstallationActivationReply,
    GPUInstallationControlRequest,
)


async def activate_gpu_installation(
    request: GPUInstallationControlRequest,
    *,
    data,
    signer_factory,
    endpoint_source,
    backend_transport,
    readiness_demoter,
    enforce=False,
    clock=None,
) -> GPUInstallationActivationReply:
    """Bind the exact prepared process before the Registry activation CAS.

    The durable membership promotion allocates the control epoch and signs the
    existing mode operation. A fresh authenticated challenge checks the resulting
    process and complete pool domain, not the plugin's unauthenticated setup claim.
    """
    request_id = uuid.uuid4()
    endpoint = await endpoint_source.open_owned(
        request.registry_id,
        request_id=request_id,
        purpose="health_observation",
        binding=None,
    )
    extra = endpoint.extra_params
    if not isinstance(extra, Mapping):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.NOT_READY, message="installation metadata unavailable"
        )
    plugin, lifecycle = extra.get("plugin"), extra.get("plugin_lifecycle")
    if not isinstance(plugin, Mapping) or not isinstance(lifecycle, Mapping):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.NOT_READY, message="installation metadata unavailable"
        )
    launch = lifecycle.get("launch")
    qualification = lifecycle.get("qualification")
    resource = plugin.get("resource")
    stopped = lifecycle.get("stopped")
    if (
        endpoint.registry_id != request.registry_id
        or endpoint.url != request.endpoint
        or endpoint.gpu_resource_id != request.resource_id
        or plugin.get("installation_id") != str(request.registry_id)
        or plugin.get("status") not in {"prepared", "disabled"}
        or not isinstance(resource, Mapping)
        or not isinstance(qualification, Mapping)
        or not isinstance(qualification.get("report"), Mapping)
        or not isinstance(qualification.get("sha256"), str)
        or resource.get("gpu_resource_id") != request.resource_id
        or not isinstance(launch, Mapping)
        or launch.get("registry_id") != str(request.registry_id)
        or launch.get("container_id") != request.container_id
        or launch.get("resource") != resource
        or not isinstance(stopped, (tuple, list))
        or any(
            not isinstance(item, Mapping)
            or item.get("container_id") == request.container_id
            or item.get("boot_id") == request.boot_id
            for item in stopped
        )
    ):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.NOT_READY, message="installation identity changed"
        )
    initial_challenge = secrets.token_hex(32)
    healthy, initial = await backend_transport.health_meta(
        endpoint,
        request_id=request_id,
        gpu_health_challenge=initial_challenge,
        timeout_seconds=8.0,
    )
    initial_residency = (
        initial.get("residency") if isinstance(initial, Mapping) else None
    )
    if (
        not healthy
        or not isinstance(initial, Mapping)
        or initial.get(GPU_HEALTH_CHALLENGE_ECHO_MARKER) != initial_challenge
        or not isinstance(initial_residency, Mapping)
        or initial_residency.get("boot_id") != request.boot_id
        or not isinstance(initial_residency.get("pools"), Mapping)
        or sorted(initial_residency["pools"]) != sorted(request.pool_ids)
    ):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.NOT_READY,
            message="GPU installation initial health identity mismatch",
        )
    if enforce:
        if clock is None:
            raise ValueError("GPU bootstrap clock unavailable")
        epoch = await bootstrap_gpu_installation(
            request,
            data=data,
            signer_factory=signer_factory,
            endpoint_source=endpoint_source,
            backend_transport=backend_transport,
            clock=clock,
            readiness_demoter=readiness_demoter,
        )
        return GPUInstallationActivationReply(
            registry_id=request.registry_id,
            resource_id=request.resource_id,
            endpoint=request.endpoint,
            boot_id=request.boot_id,
            control_epoch=epoch,
            pool_ids=request.pool_ids,
        )
    result = await data.step(
        "membership_candidates",
        {"gpu_resource_id": request.resource_id, "states": ["pending", "active"]},
    )
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.NOT_READY,
            message="GPU membership candidates unavailable",
        )
    matches = [
        item
        for item in candidates
        if isinstance(item, dict) and item.get("backend_id") == str(request.registry_id)
    ]
    if len(matches) != 1 or type(matches[0].get("membership_epoch")) is not int:
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.NOT_READY,
            message="GPU installation membership unavailable",
        )
    promotion = await promote_gpu_backend_membership(
        data,
        signer_factory(),
        endpoint_source,
        backend_transport,
        request.registry_id,
        gpu_resource_id=request.resource_id,
        membership_epoch=matches[0]["membership_epoch"],
        readiness_demoter=readiness_demoter,
    )
    if (
        promotion.status not in {"promoted", "acknowledged"}
        or not promotion.control_epoch
    ):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.NOT_READY,
            message="GPU installation signed activation unavailable",
        )
    challenge = secrets.token_hex(32)
    healthy, metadata = await backend_transport.health_meta(
        endpoint,
        request_id=request_id,
        gpu_health_challenge=challenge,
        timeout_seconds=8.0,
    )
    if (
        not healthy
        or not isinstance(metadata, Mapping)
        or metadata.get(GPU_HEALTH_CHALLENGE_ECHO_MARKER) != challenge
    ):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.UNAVAILABLE,
            message="GPU installation health challenge failed",
        )
    residency = metadata.get("residency")
    if not isinstance(residency, Mapping):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.NOT_READY,
            message="GPU installation residency unavailable",
        )
    current = await endpoint_source.open_owned(
        request.registry_id,
        request_id=request_id,
        purpose="health_observation",
        binding=None,
    )
    if current.endpoint_sha256 != endpoint.endpoint_sha256:
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.NOT_READY, message="GPU installation endpoint changed"
        )
    identity, pools = residency.get("identity"), residency.get("pools")
    if (
        residency.get("boot_id") != request.boot_id
        or residency.get("control_epoch") != promotion.control_epoch
        or not isinstance(identity, Mapping)
        or identity.get("audience") != "aap-gpu-lifecycle"
        or identity.get("backend_registry_id") != str(request.registry_id)
        or identity.get("gpu_resource_id") != request.resource_id
        or not isinstance(pools, Mapping)
        or sorted(pools) != sorted(request.pool_ids)
    ):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.NOT_READY,
            message="GPU installation live identity mismatch",
        )
    return GPUInstallationActivationReply(
        registry_id=request.registry_id,
        resource_id=request.resource_id,
        endpoint=request.endpoint,
        boot_id=request.boot_id,
        control_epoch=promotion.control_epoch,
        pool_ids=request.pool_ids,
    )
