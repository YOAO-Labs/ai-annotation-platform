"""Verify a fresh registered plugin observation without accepting launch claims as probes."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from aap_lifecycle_contract import validate_managed_lifecycle_evidence
from aap_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER
from aap_inference_contract.plugin import (
    PluginInstallationResponse,
    PluginLaunchReceipt,
    PluginManifest,
    PluginModelCapability,
    PluginPoolTopology,
    PluginLifecycleQualification,
)
from aap_protocol_v2.lifecycle import (
    BackendResidency,
    LifecycleState,
    canonical_managed_lifecycle_capabilities,
)
from aap_protocol_v2.version import PROTOCOL_VERSION
from aap_protocol_v2.vocab import GEOMETRY_VALUES, INPUT_VALUES, PROMPT_VALUES
from training_contract.inference_runtime import LIBRE_CUDA_RUNTIME
from training_contract.model_return import LIBRE_RUNTIME

from app.services.inference_health_observation import HealthObservationClient
from app.services.inference_topology_data import InferenceTopologyData


class PluginObservationRejected(ValueError):
    """Endpoint identity, capabilities, or resource did not match the reservation."""


def validate_manifest_capabilities(manifest: PluginManifest) -> None:
    """Reject values outside the protocol vocabulary before approving intent."""
    for model in manifest.models:
        if (
            not set(model.supported_inputs).issubset(INPUT_VALUES)
            or not set(model.supported_prompts).issubset(PROMPT_VALUES)
            or not set(model.supported_geometric_outputs).issubset(GEOMETRY_VALUES)
        ):
            raise PluginObservationRejected("unknown model capability")


def _model_capabilities_match(manifest: PluginManifest, models: object) -> bool:
    if type(models) is not list or len(models) != len(manifest.models):
        return False
    expected = {model.id: model for model in manifest.models}
    observed = {}
    try:
        for model in models:
            if type(model) is not dict:
                return False
            capability = PluginModelCapability.model_validate(
                {key: model[key] for key in PluginModelCapability.model_fields}
            )
            if capability.id in observed:
                return False
            profile = model.get("resource_profile")
            if type(profile) is not dict or profile.get("device") != (
                "gpu" if manifest.resource.device == "cuda" else "cpu"
            ):
                return False
            observed[capability.id] = capability
    except (KeyError, TypeError, ValueError):
        return False
    if observed.keys() != expected.keys():
        return False
    for key, model in expected.items():
        other = observed[key]
        if (
            model.task != other.task
            or model.is_interactive != other.is_interactive
            or set(model.supported_inputs) != set(other.supported_inputs)
            or set(model.supported_prompts) != set(other.supported_prompts)
            or set(model.supported_geometric_outputs)
            != set(other.supported_geometric_outputs)
        ):
            return False
    return True


def _pool_topology_matches(manifest: PluginManifest, pools: object) -> bool:
    if type(pools) is not dict or pools.keys() != manifest.pools.keys():
        return False
    try:
        return all(
            PluginPoolTopology.model_validate(value) == manifest.pools[name]
            for name, value in pools.items()
        )
    except (TypeError, ValueError):
        return False


def _parse_residency(value: object) -> BackendResidency:
    if (
        type(value) is not dict
        or any(
            type(value.get(key)) is not int
            for key in ("active_requests", "builders", "borrowers")
        )
        or any(type(value.get(key)) is not bool for key in ("draining", "evictable"))
        or (
            value.get("gpu_loaded") is not None
            and type(value["gpu_loaded"]) is not bool
        )
        or type(value.get("pools")) is not dict
        or any(
            type(pool) is not dict
            or (pool.get("resident") is not None and type(pool["resident"]) is not bool)
            for pool in value["pools"].values()
        )
    ):
        raise ValueError("residency has invalid JSON field types")
    return BackendResidency.model_validate(value)


def verify_launch_receipt(
    installation: PluginInstallationResponse, receipt: PluginLaunchReceipt
) -> None:
    reserved = installation.installation
    if (
        receipt.installation_id != reserved.installation_id
        or receipt.registry_id != installation.registry_id
        or receipt.artifact != reserved.manifest.artifact
        or receipt.resource != reserved.resource
    ):
        raise PluginObservationRejected(
            "launch receipt differs from reserved installation"
        )


def verify_endpoint_observation(
    installation: PluginInstallationResponse,
    receipt: PluginLaunchReceipt,
    *,
    endpoint: str,
    challenge: str,
    healthy: bool,
    health: Any,
    setup: Any,
    qualification: PluginLifecycleQualification | None = None,
    for_drain: bool = False,
) -> dict[str, Any]:
    """Validate live facts; the receipt is trusted-admin local inspection, not attestation."""
    verify_launch_receipt(installation, receipt)
    item = installation.installation
    manifest = item.manifest
    resource = item.resource
    if (
        not healthy
        or type(health) is not dict
        or health.get(GPU_HEALTH_CHALLENGE_ECHO_MARKER) != challenge
        or type(setup) is not dict
    ):
        raise PluginObservationRejected(
            "fresh authenticated endpoint observation unavailable"
        )
    if setup.get("protocol_version") != PROTOCOL_VERSION:
        raise PluginObservationRejected(
            "backend protocol differs from the supported version"
        )
    if setup.get("plugin_identity") != {
        "plugin_id": manifest.plugin_id,
        "version": manifest.version,
    }:
        raise PluginObservationRejected("plugin identity differs from manifest")
    if (
        setup.get("registry_id") != str(installation.registry_id)
        or setup.get("gpu_resource_id") != resource.gpu_resource_id
    ):
        raise PluginObservationRejected(
            "runtime registry or resource differs from reservation"
        )
    if setup.get("physical_gpu_uuid") != resource.physical_gpu_uuid:
        raise PluginObservationRejected("physical GPU differs from reservation")
    gpu_info = health.get("gpu_info")
    if not (
        (resource.physical_gpu_uuid is None and gpu_info is None)
        or (
            type(gpu_info) is dict
            and "device_uuid" in gpu_info
            and gpu_info["device_uuid"] == resource.physical_gpu_uuid
        )
    ):
        raise PluginObservationRejected("health GPU identity differs from reservation")
    compute = health.get("compute")
    runtime = setup.get("runtime_info")
    if (
        type(compute) is not dict
        or compute.get("configured_device") != resource.logical_device
        or (
            manifest.resource.device == "cuda"
            and compute.get("cpu_fallback_supported") is not False
        )
        or type(compute.get("cpu_fallback_supported")) is not bool
        or compute.get("effective_device") not in (None, resource.logical_device)
        or type(runtime) is not dict
        or runtime.get("device") != manifest.resource.device
    ):
        raise PluginObservationRejected("runtime device differs from reservation")
    if (
        any(
            entry.framework == "libreyolo"
            for entry in manifest.managed_model_compatibility
        )
        and compute["cpu_fallback_supported"] is not False
    ):
        raise PluginObservationRejected(
            "LibreYOLO fallback policy differs from reservation"
        )
    compatibility = [
        entry.model_dump(mode="json") for entry in manifest.managed_model_compatibility
    ]
    if setup.get("managed_model_compatibility") != compatibility:
        raise PluginObservationRejected(
            "managed model capabilities differ from manifest"
        )
    if not for_drain and any(
        entry.framework == "libreyolo" for entry in manifest.managed_model_compatibility
    ):
        baseline = (
            LIBRE_CUDA_RUNTIME if manifest.resource.device == "cuda" else LIBRE_RUNTIME
        )
        if any(
            runtime.get(key) != baseline[key]
            for key in ("libreyolo", "torch", "torchvision")
        ):
            raise PluginObservationRejected(
                "installed runtime versions are not qualified"
            )
        if manifest.resource.device == "cuda" and (
            runtime.get("cuda_build") != baseline["cuda_build"]
            or runtime.get("tf32") is not False
        ):
            raise PluginObservationRejected(
                "CUDA build or execution configuration is not qualified"
            )
    validate_manifest_capabilities(manifest)
    if not for_drain:
        if not _model_capabilities_match(manifest, setup.get("models")):
            raise PluginObservationRejected(
                "model capabilities differ from approved intent"
            )
        if not _pool_topology_matches(manifest, setup.get("pool_topology")):
            raise PluginObservationRejected(
                "pool topology differs from approved intent"
            )
        if not _pool_topology_matches(manifest, health.get("pool_topology")):
            raise PluginObservationRejected(
                "live pool topology differs from approved intent"
            )
    if manifest.resource.device == "cpu" and setup.get("terminal_lifecycle") != {
        "terminal_drain_endpoint": "/lifecycle/terminal-drain",
    }:
        raise PluginObservationRejected("CPU terminal lifecycle capability unavailable")
    if manifest.resource.device == "cuda" and setup.get("terminal_lifecycle") != {
        "terminal_drain_signed_endpoint": "/lifecycle/terminal-drain-signed",
    }:
        raise PluginObservationRejected("GPU terminal lifecycle capability unavailable")
    try:
        if manifest.resource.device == "cuda" and not for_drain:
            canonical_managed_lifecycle_capabilities(setup.get("managed_lifecycle"))
        residency = _parse_residency(health.get("residency"))
    except (TypeError, ValueError):
        raise PluginObservationRejected(
            "managed lifecycle evidence is invalid"
        ) from None
    if residency.state is LifecycleState.UNKNOWN or set(residency.pools) != set(
        manifest.pools
    ):
        raise PluginObservationRejected(
            "runtime pool state differs from approved intent"
        )
    if manifest.resource.device == "cpu" and (
        residency.gpu_loaded is not False
        or residency.identity is not None
        or residency.generation is not None
        or residency.control_epoch is not None
    ):
        raise PluginObservationRejected("CPU runtime has GPU lifecycle state")
    if residency.identity is not None and (
        residency.identity.backend_registry_id != str(installation.registry_id)
        or residency.identity.gpu_resource_id != resource.gpu_resource_id
    ):
        raise PluginObservationRejected("lifecycle identity differs from reservation")
    if manifest.resource.device == "cuda" and not for_drain:
        verify_qualification(
            installation, receipt, qualification, health, setup, residency
        )
    elif qualification is not None:
        raise PluginObservationRejected(
            "GPU qualification is not applicable to CPU installations"
        )
    return {
        **receipt.model_dump(mode="json"),
        "endpoint": endpoint,
        "boot_id": residency.boot_id,
        "control_epoch": residency.control_epoch,
    }


def verify_qualification(
    installation, receipt, qualification, health, setup, residency
):
    """Validate and bind the admin-approved report to this inspected launch and live endpoint."""
    if qualification is None:
        raise PluginObservationRejected("CUDA lifecycle qualification is required")
    try:
        report = validate_managed_lifecycle_evidence(qualification.report)
        encoded = json.dumps(
            report,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise PluginObservationRejected("invalid lifecycle qualification") from None
    if (
        not report["passed"]
        or hashlib.sha256(encoded).hexdigest() != qualification.sha256
    ):
        raise PluginObservationRejected(
            "lifecycle qualification did not pass or has changed"
        )
    item = installation.installation
    resource = item.resource
    deployment = report["deployment"]
    gpu = report["gpu"]
    runtime = setup["runtime_info"]
    gpu_info = health["gpu_info"]
    if (
        report["backend_name"] != item.manifest.plugin_id
        or deployment["image_id"] != receipt.image_id
        or gpu["uuid"] != resource.physical_gpu_uuid
        or gpu["driver_version"] != gpu_info.get("driver_version")
        or gpu["driver_version"] != runtime.get("driver_version")
        or gpu["total_memory_mb"] != gpu_info.get("total_memory_mb")
        or gpu["total_memory_mb"] != runtime.get("total_memory_mb")
        or gpu["runtime_version"] != runtime.get("cuda_build")
    ):
        raise PluginObservationRejected(
            "qualification hardware or image differs from observed launch"
        )
    topology = deployment["pool_topology"]
    live_versions = runtime.get("runtime_versions")
    is_onnx = (
        setup.get("infra") == "onnx"
        or (type(live_versions) is dict and "onnxruntime" in live_versions)
        or "onnxruntime" in deployment["runtime_versions"]
    )
    final_residency = report["final_residency"]
    if (
        set(topology)
        != (
            {"registry_id", "gpu_resource_id", "pools", "session_counts"}
            if is_onnx
            else {"registry_id", "gpu_resource_id", "pools"}
        )
        or topology["registry_id"] != str(installation.registry_id)
        or topology["gpu_resource_id"] != resource.gpu_resource_id
        or not _pool_topology_matches(item.manifest, topology["pools"])
        or set(residency.pools) != set(item.manifest.pools)
        or type(final_residency) is not dict
        or type(final_residency.get("pools")) is not dict
        or set(final_residency["pools"]) != set(item.manifest.pools)
    ):
        raise PluginObservationRejected(
            "qualification pool topology differs from endpoint"
        )
    if is_onnx:
        expected_counts = topology["session_counts"]
        for observed_counts in (
            runtime.get("ort_session_counts"),
            health.get("ort_session_counts"),
        ):
            if (
                type(observed_counts) is not dict
                or observed_counts != expected_counts
                or any(type(count) is not int for count in observed_counts.values())
            ):
                raise PluginObservationRejected(
                    "ORT session counts differ from live factory"
                )
    try:
        final_snapshot = _parse_residency(final_residency)
    except (TypeError, ValueError):
        raise PluginObservationRejected(
            "qualification final residency is incomplete"
        ) from None
    if (
        final_snapshot.identity is None
        or final_snapshot.identity.backend_registry_id != str(installation.registry_id)
        or final_snapshot.identity.gpu_resource_id != resource.gpu_resource_id
    ):
        raise PluginObservationRejected(
            "qualification final identity differs from reservation"
        )
    versions = deployment["runtime_versions"]
    is_libreyolo = any(
        entry.framework == "libreyolo"
        for entry in item.manifest.managed_model_compatibility
    )
    if live_versions is not None or not is_libreyolo:
        if (
            type(live_versions) is not dict
            or not live_versions
            or any(
                type(key) is not str or not key or type(value) is not str or not value
                for key, value in live_versions.items()
            )
            or any(versions.get(key) != value for key, value in live_versions.items())
            or (is_onnx and "onnxruntime" not in live_versions)
        ):
            raise PluginObservationRejected(
                "qualification runtime versions differ from endpoint"
            )
    required = {
        "plugin_id": item.manifest.plugin_id,
        "plugin_version": item.manifest.version,
        "artifact": receipt.artifact,
        "asset_manifest_sha256": item.manifest.asset_manifest_sha256,
        "registry_id": str(installation.registry_id),
        "gpu_resource_id": resource.gpu_resource_id,
        "driver_version": gpu["driver_version"],
    }
    if any(
        entry.framework == "libreyolo"
        for entry in item.manifest.managed_model_compatibility
    ):
        required.update(
            {
                key: str(value).lower() if isinstance(value, bool) else str(value)
                for key, value in LIBRE_CUDA_RUNTIME.items()
            }
        )
    if any(versions.get(key) != value for key, value in required.items()):
        raise PluginObservationRejected(
            "qualification identity or runtime differs from reservation"
        )


async def observe_plugin_endpoint(
    db,
    installation: PluginInstallationResponse,
    receipt: PluginLaunchReceipt,
    qualification: PluginLifecycleQualification | None = None,
    *,
    for_drain: bool = False,
) -> dict[str, Any]:
    """Use Inference's credential-owning observation service, never materialize tokens here."""
    registry = await InferenceTopologyData(db).registry_facts(installation.registry_id)
    if registry is None:
        raise PluginObservationRejected("reserved registry no longer exists")
    client = HealthObservationClient(db, registry)
    challenge = secrets.token_hex(32)
    healthy, health = await client.health_meta(gpu_health_challenge=challenge)
    try:
        setup = await client.setup()
    except Exception:
        raise PluginObservationRejected("authenticated setup unavailable") from None
    return verify_endpoint_observation(
        installation,
        receipt,
        endpoint=registry.url,
        challenge=challenge,
        healthy=healthy,
        health=health,
        setup=setup,
        qualification=qualification,
        for_drain=for_drain,
    )
