"""Verify a fresh registered plugin observation without accepting launch claims as probes."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from yoao_lifecycle_contract import validate_managed_lifecycle_evidence
from yoao_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER
from yoao_inference.host_guard import InferenceHostDenied
from yoao_inference.gpu.policy import (
    direct_remote_gpu_collides_with_local,
    observed_gpu_device_uuids,
)
from yoao_inference.gpu.settings import GPUArbiterMode, GPUAuthoritySettings
from yoao_inference_contract.plugin import (
    DirectPluginProbeResponse,
    INFERENCE_CAPABILITIES,
    ManagedModelCompatibility,
    PluginInstallationResponse,
    PluginLaunchReceipt,
    PluginLifecycleQualification,
    PluginManifest,
    PluginModelCapability,
    PluginPoolTopology,
    PluginResourceBinding,
    PluginResourceRequirements,
    RegisterDirectPluginRequest,
)
from yoao_protocol_v2.lifecycle import (
    BackendResidency,
    LifecycleState,
    canonical_managed_lifecycle_capabilities,
)
from yoao_protocol_v2.version import PROTOCOL_VERSION
from yoao_protocol_v2.vocab import GEOMETRY_VALUES, INPUT_VALUES, PROMPT_VALUES

from app.config import settings
from app.services.inference_health_observation import HealthObservationClient
from app.services.inference_topology_data import InferenceTopologyData


class PluginObservationRejected(ValueError):
    """Endpoint identity, capabilities, or resource did not match the reservation."""


class PluginLocalGPUCollision(PluginObservationRejected):
    """Direct plugin reported a physical GPU UUID managed by the local GPU arbiter."""


def is_host_denied_error(exc: BaseException) -> bool:
    return isinstance(exc, InferenceHostDenied) or (
        isinstance(exc, RuntimeError) and str(exc).startswith("inference_host_denied:")
    )


_TASK_TO_CAPABILITY = {
    "detect": "detection",
    "segment": "segmentation",
    "classify": "classification",
    "obb": "obb",
    "pose": "pose",
    "ocr": "ocr",
    "doc_layout": "doc_layout",
    "tracker": "video_tracking",
    "interactive_seg": "interactive",
}


def _direct_gpu_settings() -> GPUAuthoritySettings:
    mode = getattr(settings, "gpu_arbiter_mode", GPUArbiterMode.OFF)
    if not isinstance(mode, GPUArbiterMode):
        mode = GPUArbiterMode(str(mode))
    return GPUAuthoritySettings(
        gpu_arbiter_mode=mode,
        gpu_arbiter_resources=settings.gpu_arbiter_resources,
        gpu_arbiter_config_errors=settings.gpu_arbiter_config_errors,
        gpu_arbiter_rollout_enabled=bool(
            getattr(settings, "gpu_arbiter_rollout_enabled", False)
        ),
    )


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
        manifest.managed_model_compatibility
        and compute["cpu_fallback_supported"] is not False
    ):
        raise PluginObservationRejected(
            "managed model compatibility requires cpu_fallback_supported=false"
        )
    compatibility = [
        entry.model_dump(mode="json") for entry in manifest.managed_model_compatibility
    ]
    if setup.get("managed_model_compatibility") != compatibility:
        raise PluginObservationRejected(
            "managed model capabilities differ from manifest"
        )
    if (
        "managed_evaluation" in manifest.capabilities
        and setup.get("managed_evaluation") is not True
    ):
        raise PluginObservationRejected("managed evaluation capability unavailable")
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
    required = {
        "plugin_id": item.manifest.plugin_id,
        "plugin_version": item.manifest.version,
        "artifact": receipt.artifact,
        "asset_manifest_sha256": item.manifest.asset_manifest_sha256,
        "registry_id": str(installation.registry_id),
        "gpu_resource_id": resource.gpu_resource_id,
        "driver_version": gpu["driver_version"],
    }
    if live_versions is not None:
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
    else:
        matched_version_keys: set[str] = set()
        non_version_keys = set(required) | {"device"}
        for key, value in runtime.items():
            if key not in versions:
                continue
            if type(value) is bool:
                normalized = "true" if value else "false"
            elif type(value) is str:
                normalized = value
            else:
                continue
            if versions[key] != normalized:
                raise PluginObservationRejected(
                    "qualification runtime versions differ from endpoint"
                )
            if key not in non_version_keys:
                matched_version_keys.add(key)
        if not matched_version_keys or (
            is_onnx and "onnxruntime" not in matched_version_keys
        ):
            raise PluginObservationRejected(
                "qualification runtime versions differ from endpoint"
            )
    if versions.get("tf32") == "false" and runtime.get("tf32") is True:
        raise PluginObservationRejected(
            "qualification runtime versions differ from endpoint"
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


def _parse_observed_models(
    models: object, *, expected_profile_device: str
) -> list[PluginModelCapability]:
    if type(models) is not list or not (1 <= len(models) <= 128):
        raise PluginObservationRejected("endpoint must advertise 1..128 models")
    parsed: list[PluginModelCapability] = []
    seen: set[str] = set()
    for model in models:
        if type(model) is not dict:
            raise PluginObservationRejected("invalid model capability payload")
        try:
            capability = PluginModelCapability.model_validate(
                {key: model[key] for key in PluginModelCapability.model_fields}
            )
        except (KeyError, TypeError, ValueError):
            raise PluginObservationRejected(
                "invalid model capability payload"
            ) from None
        if capability.id in seen:
            raise PluginObservationRejected("duplicate model id in setup")
        profile = model.get("resource_profile")
        if (
            type(profile) is not dict
            or profile.get("device") != expected_profile_device
        ):
            raise PluginObservationRejected(
                "model resource_profile differs from device"
            )
        seen.add(capability.id)
        parsed.append(capability)
    return parsed


def _parse_observed_pools(pools: object) -> dict[str, PluginPoolTopology]:
    if type(pools) is not dict or not (1 <= len(pools) <= 128):
        raise PluginObservationRejected("endpoint must advertise 1..128 pools")
    parsed: dict[str, PluginPoolTopology] = {}
    for name, value in pools.items():
        if type(name) is not str or not name or len(name) > 128 or name.strip() != name:
            raise PluginObservationRejected("invalid pool id")
        try:
            parsed[name] = PluginPoolTopology.model_validate(value)
        except (TypeError, ValueError):
            raise PluginObservationRejected("invalid pool topology") from None
    return parsed


def _parse_observed_compatibility(
    raw: object,
) -> list[ManagedModelCompatibility]:
    if raw is None:
        return []
    if type(raw) is not list:
        raise PluginObservationRejected("invalid managed_model_compatibility")
    try:
        return [ManagedModelCompatibility.model_validate(entry) for entry in raw]
    except (TypeError, ValueError):
        raise PluginObservationRejected("invalid managed_model_compatibility") from None


def _check_direct_gpu_boundary(
    *,
    device: str,
    health: dict[str, Any],
    residency: BackendResidency,
    gpu_config: GPUAuthoritySettings,
) -> None:
    gpu_info = health.get("gpu_info")
    if device == "cpu":
        if gpu_info is not None and (
            type(gpu_info) is not dict or bool(observed_gpu_device_uuids(health))
        ):
            raise PluginObservationRejected("CPU direct plugin reported GPU identity")
        if residency.gpu_loaded is True:
            raise PluginObservationRejected("CPU direct plugin reported GPU residency")
        return
    if gpu_config.gpu_arbiter_resources:
        if direct_remote_gpu_collides_with_local(health, config=gpu_config):
            raise PluginLocalGPUCollision(
                "direct GPU plugin collides with a locally managed GPU arbiter resource or unverifiable index:* host GPU config"
            )
        if not observed_gpu_device_uuids(health):
            raise PluginLocalGPUCollision(
                "direct GPU plugin must report gpu_info.device_uuid when local GPU arbiter resources are configured"
            )


def _derive_direct_capabilities(
    models: list[PluginModelCapability],
    *,
    has_managed_models: bool,
    has_managed_evaluation: bool,
) -> list[str]:
    selected: set[str] = {"inference"}
    for model in models:
        mapped = _TASK_TO_CAPABILITY.get(model.task)
        if mapped:
            selected.add(mapped)
        if model.is_interactive:
            selected.add("interactive")
    if has_managed_models:
        selected.add("managed_models")
    if has_managed_evaluation:
        selected.add("managed_evaluation")
    return [cap for cap in INFERENCE_CAPABILITIES if cap in selected]


def _validate_direct_boot_id(residency: BackendResidency) -> str:
    boot_id = residency.boot_id
    if (
        type(boot_id) is not str
        or not boot_id
        or len(boot_id) > 128
        or boot_id.strip() != boot_id
    ):
        raise PluginObservationRejected("residency boot_id is missing or invalid")
    return boot_id


def _direct_terminal_drain_supported(setup: dict[str, Any]) -> bool:
    terminal = setup.get("terminal_lifecycle")
    return type(terminal) is dict and (
        terminal.get("terminal_drain_endpoint") == "/lifecycle/terminal-drain"
    )


def derive_direct_plugin_observation(
    request: RegisterDirectPluginRequest,
    *,
    endpoint: str,
    challenge: str,
    healthy: bool,
    health: Any,
    setup: Any,
    gpu_config: GPUAuthoritySettings | None = None,
) -> dict[str, Any]:
    """Validate live D1 direct endpoint facts and freeze the approved manifest."""
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
    identity = setup.get("plugin_identity")
    if (
        type(identity) is not dict
        or set(identity) != {"plugin_id", "version"}
        or type(identity.get("plugin_id")) is not str
        or type(identity.get("version")) is not str
    ):
        raise PluginObservationRejected("plugin_identity is missing or invalid")
    runtime = setup.get("runtime_info")
    compute = health.get("compute")
    if (
        type(runtime) is not dict
        or runtime.get("device") not in {"cpu", "cuda"}
        or type(compute) is not dict
        or type(compute.get("configured_device")) is not str
        or type(compute.get("cpu_fallback_supported")) is not bool
    ):
        raise PluginObservationRejected("runtime device metadata is invalid")
    device = runtime["device"]
    configured_device = compute["configured_device"]
    if device == "cpu" and configured_device != "cpu":
        raise PluginObservationRejected("CPU runtime device mismatch")
    if device == "cuda" and not (
        configured_device == "cuda" or configured_device.startswith("cuda:")
    ):
        raise PluginObservationRejected("CUDA runtime device mismatch")
    if compute.get("effective_device") not in (None, configured_device, device):
        raise PluginObservationRejected("effective compute device mismatch")
    if (
        request.approved_managed_model_compatibility
        and compute["cpu_fallback_supported"] is not False
    ):
        raise PluginObservationRejected(
            "managed model compatibility requires cpu_fallback_supported=false"
        )
    models = _parse_observed_models(
        setup.get("models"),
        expected_profile_device="gpu" if device == "cuda" else "cpu",
    )
    setup_pools = _parse_observed_pools(setup.get("pool_topology"))
    health_pools = _parse_observed_pools(health.get("pool_topology"))
    if setup_pools != health_pools:
        raise PluginObservationRejected("setup and health pool topologies differ")
    try:
        residency = _parse_residency(health.get("residency"))
    except (TypeError, ValueError):
        raise PluginObservationRejected("residency evidence is invalid") from None
    if residency.state is LifecycleState.UNKNOWN or set(residency.pools) != set(
        setup_pools
    ):
        raise PluginObservationRejected("runtime pool state differs from pool topology")
    if residency.control_epoch is not None:
        raise PluginObservationRejected(
            "unarbitrated direct plugin must not report a managed control_epoch"
        )
    boot_id = _validate_direct_boot_id(residency)
    _check_direct_gpu_boundary(
        device=device,
        health=health,
        residency=residency,
        gpu_config=gpu_config or _direct_gpu_settings(),
    )
    observed_compat = _parse_observed_compatibility(
        setup.get("managed_model_compatibility")
    )
    observed_compat_set = set(observed_compat)
    for entry in request.approved_managed_model_compatibility:
        if entry.runtime_device != device or entry not in observed_compat_set:
            raise PluginObservationRejected(
                "approved managed_model_compatibility is not advertised by live endpoint"
            )
    observed_managed_evaluation = setup.get("managed_evaluation") is True
    if request.approved_managed_evaluation and not observed_managed_evaluation:
        raise PluginObservationRejected(
            "approved managed_evaluation is not advertised by live endpoint"
        )
    if request.asset_manifest_sha256 is not None:
        if setup.get("asset_manifest_sha256") != request.asset_manifest_sha256:
            raise PluginObservationRejected(
                "asset_manifest_sha256 differs from live endpoint"
            )
    capabilities = _derive_direct_capabilities(
        models,
        has_managed_models=bool(request.approved_managed_model_compatibility),
        has_managed_evaluation=request.approved_managed_evaluation,
    )
    logical_device = "cpu" if device == "cpu" else configured_device
    try:
        manifest = PluginManifest.model_validate(
            {
                "manifest_schema": "yoao-extension-manifest/v1",
                "plugin_id": identity["plugin_id"],
                "version": identity["version"],
                "display_name": request.display_name,
                "description": request.description,
                "homepage": request.homepage,
                "publisher": request.publisher or "direct",
                "license": request.license or "unspecified",
                "protocol_version": "yoao-inference/v2",
                "execution_mode": "direct",
                "sdk_compatibility": None,
                "entrypoint": None,
                "capabilities": capabilities,
                "models": [model.model_dump(mode="python") for model in models],
                "pools": {
                    key: value.model_dump(mode="python")
                    for key, value in setup_pools.items()
                },
                "resource": PluginResourceRequirements(
                    device=device, memory_mb=None, gpu_memory_mb=0
                ).model_dump(mode="python"),
                "asset_manifest_sha256": request.asset_manifest_sha256,
                "managed_model_compatibility": [
                    entry.model_dump(mode="python")
                    for entry in request.approved_managed_model_compatibility
                ],
                "allowed_asset_sources": [],
                "artifact": None,
            }
        )
        resource = PluginResourceBinding(
            gpu_resource_id=None,
            physical_gpu_uuid=None,
            logical_device=logical_device,
        )
    except (TypeError, ValueError) as exc:
        raise PluginObservationRejected(str(exc)) from None
    validate_manifest_capabilities(manifest)
    return {
        "endpoint": endpoint,
        "boot_id": boot_id,
        "control_epoch": None,
        "terminal_drain_supported": _direct_terminal_drain_supported(setup),
        "manifest": manifest.model_dump(mode="json"),
        "resource": resource.model_dump(mode="json"),
    }


def verify_direct_endpoint_observation(
    installation: PluginInstallationResponse,
    *,
    endpoint: str,
    challenge: str,
    healthy: bool,
    health: Any,
    setup: Any,
    for_drain: bool = False,
    gpu_config: GPUAuthoritySettings | None = None,
) -> dict[str, Any]:
    """Validate live facts for an already-frozen D1 direct plugin installation."""
    item = installation.installation
    manifest = item.manifest
    resource = item.resource
    if item.execution_mode != "direct" or resource.gpu_resource_id is not None:
        raise PluginObservationRejected(
            "installation is not an unarbitrated direct plugin"
        )
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
    runtime = setup.get("runtime_info")
    compute = health.get("compute")
    if (
        type(runtime) is not dict
        or runtime.get("device") != manifest.resource.device
        or type(compute) is not dict
        or compute.get("configured_device") != resource.logical_device
        or type(compute.get("cpu_fallback_supported")) is not bool
        or compute.get("effective_device")
        not in (None, resource.logical_device, manifest.resource.device)
    ):
        raise PluginObservationRejected("runtime device differs from manifest")
    if (
        manifest.managed_model_compatibility
        and compute["cpu_fallback_supported"] is not False
    ):
        raise PluginObservationRejected(
            "managed model compatibility requires cpu_fallback_supported=false"
        )
    observed_compat = _parse_observed_compatibility(
        setup.get("managed_model_compatibility")
    )
    if not set(manifest.managed_model_compatibility) <= set(observed_compat):
        raise PluginObservationRejected(
            "approved managed_model_compatibility is no longer advertised"
        )
    if (
        "managed_evaluation" in manifest.capabilities
        and setup.get("managed_evaluation") is not True
    ):
        raise PluginObservationRejected("managed evaluation capability unavailable")
    if (
        manifest.asset_manifest_sha256 is not None
        and setup.get("asset_manifest_sha256") != manifest.asset_manifest_sha256
    ):
        raise PluginObservationRejected("asset_manifest_sha256 differs from manifest")
    validate_manifest_capabilities(manifest)
    if not for_drain:
        if not _model_capabilities_match(manifest, setup.get("models")):
            raise PluginObservationRejected("model capabilities differ from manifest")
        if not _pool_topology_matches(manifest, setup.get("pool_topology")):
            raise PluginObservationRejected("pool topology differs from manifest")
        if not _pool_topology_matches(manifest, health.get("pool_topology")):
            raise PluginObservationRejected("live pool topology differs from manifest")
    try:
        residency = _parse_residency(health.get("residency"))
    except (TypeError, ValueError):
        raise PluginObservationRejected("residency evidence is invalid") from None
    if residency.state is LifecycleState.UNKNOWN or set(residency.pools) != set(
        manifest.pools
    ):
        raise PluginObservationRejected("runtime pool state differs from manifest")
    if residency.control_epoch is not None:
        raise PluginObservationRejected(
            "unarbitrated direct plugin must not report a managed control_epoch"
        )
    boot_id = _validate_direct_boot_id(residency)
    _check_direct_gpu_boundary(
        device=manifest.resource.device,
        health=health,
        residency=residency,
        gpu_config=gpu_config or _direct_gpu_settings(),
    )
    return {
        "endpoint": endpoint,
        "boot_id": boot_id,
        "control_epoch": None,
        "terminal_drain_supported": _direct_terminal_drain_supported(setup),
    }


async def observe_direct_plugin_registration(
    db,
    reserved: PluginInstallationResponse,
    request: RegisterDirectPluginRequest,
) -> dict[str, Any]:
    registry = await InferenceTopologyData(db).registry_facts(reserved.registry_id)
    if registry is None:
        raise PluginObservationRejected("reserved registry no longer exists")
    client = HealthObservationClient(db, registry)
    challenge = secrets.token_hex(32)
    healthy, health = await client.health_meta(gpu_health_challenge=challenge)
    try:
        setup = await client.setup()
    except Exception:
        raise PluginObservationRejected("authenticated setup unavailable") from None
    return derive_direct_plugin_observation(
        request,
        endpoint=registry.url,
        challenge=challenge,
        healthy=healthy,
        health=health,
        setup=setup,
    )


async def observe_direct_plugin_endpoint(
    db,
    installation: PluginInstallationResponse,
    *,
    for_drain: bool = False,
) -> dict[str, Any]:
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
    return verify_direct_endpoint_observation(
        installation,
        endpoint=registry.url,
        challenge=challenge,
        healthy=healthy,
        health=health,
        setup=setup,
        for_drain=for_drain,
    )


async def probe_plugin_installation(
    db,
    installation: PluginInstallationResponse,
) -> DirectPluginProbeResponse:
    """Read-only probe of an installed plugin; never mutates extra_params->'plugin'."""
    registry = await InferenceTopologyData(db).registry_facts(installation.registry_id)
    if registry is None:
        raise PluginObservationRejected("reserved registry no longer exists")
    item = installation.installation
    client = HealthObservationClient(db, registry)
    challenge = secrets.token_hex(32)
    try:
        healthy, health = await client.health_meta(gpu_health_challenge=challenge)
    except (InferenceHostDenied, RuntimeError) as exc:
        return DirectPluginProbeResponse(
            registry_id=installation.registry_id,
            installation_id=item.installation_id,
            execution_mode=item.execution_mode,
            status=item.status,
            healthy=False,
            matches_manifest=False,
            mismatch_reason=str(exc),
        )
    setup = None
    if healthy:
        try:
            setup = await client.setup()
        except Exception:
            setup = None
    if not healthy or type(health) is not dict or type(setup) is not dict:
        return DirectPluginProbeResponse(
            registry_id=installation.registry_id,
            installation_id=item.installation_id,
            execution_mode=item.execution_mode,
            status=item.status,
            healthy=bool(healthy),
            matches_manifest=False,
            mismatch_reason="endpoint health or setup observation unavailable",
        )
    runtime = setup.get("runtime_info")
    device = (
        runtime.get("device")
        if type(runtime) is dict and runtime.get("device") in {"cpu", "cuda"}
        else None
    )
    boot_id = None
    try:
        boot_id = _validate_direct_boot_id(_parse_residency(health.get("residency")))
    except (TypeError, ValueError):
        boot_id = None
    observed_models: list[PluginModelCapability] = []
    if device is not None:
        try:
            observed_models = _parse_observed_models(
                setup.get("models"),
                expected_profile_device="gpu" if device == "cuda" else "cpu",
            )
        except PluginObservationRejected:
            observed_models = []
    try:
        observed_pools = _parse_observed_pools(setup.get("pool_topology"))
    except PluginObservationRejected:
        observed_pools = {}
    try:
        observed_compat = _parse_observed_compatibility(
            setup.get("managed_model_compatibility")
        )
    except PluginObservationRejected:
        observed_compat = []
    observed_eval = setup.get("managed_evaluation") is True
    terminal_drain = _direct_terminal_drain_supported(setup)
    matches = True
    mismatch_reason = None
    if item.execution_mode == "direct":
        try:
            verify_direct_endpoint_observation(
                installation,
                endpoint=registry.url,
                challenge=challenge,
                healthy=healthy,
                health=health,
                setup=setup,
            )
        except PluginObservationRejected as exc:
            matches = False
            mismatch_reason = str(exc)
    else:
        manifest = item.manifest
        if health.get(GPU_HEALTH_CHALLENGE_ECHO_MARKER) != challenge:
            matches = False
            mismatch_reason = "health challenge echo mismatch"
        elif setup.get("protocol_version") != PROTOCOL_VERSION:
            matches = False
            mismatch_reason = "backend protocol differs from the supported version"
        elif setup.get("plugin_identity") != {
            "plugin_id": manifest.plugin_id,
            "version": manifest.version,
        }:
            matches = False
            mismatch_reason = "plugin identity differs from manifest"
        elif not _model_capabilities_match(manifest, setup.get("models")):
            matches = False
            mismatch_reason = "model capabilities differ from manifest"
        elif not _pool_topology_matches(
            manifest, setup.get("pool_topology")
        ) or not _pool_topology_matches(manifest, health.get("pool_topology")):
            matches = False
            mismatch_reason = "pool topology differs from manifest"
        elif (
            "managed_evaluation" in manifest.capabilities
            and setup.get("managed_evaluation") is not True
        ):
            matches = False
            mismatch_reason = "managed evaluation capability unavailable"
    return DirectPluginProbeResponse(
        registry_id=installation.registry_id,
        installation_id=item.installation_id,
        execution_mode=item.execution_mode,
        status=item.status,
        healthy=True,
        protocol_version=setup.get("protocol_version")
        if type(setup.get("protocol_version")) is str
        else None,
        boot_id=boot_id,
        device=device,
        observed_models=observed_models,
        observed_pools=observed_pools,
        observed_managed_model_compatibility=observed_compat,
        observed_managed_evaluation=observed_eval,
        terminal_drain_supported=terminal_drain,
        matches_manifest=matches,
        mismatch_reason=mismatch_reason,
    )
