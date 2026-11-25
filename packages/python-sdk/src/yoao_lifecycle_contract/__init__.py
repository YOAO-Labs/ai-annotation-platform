"""Portable, secret-free managed lifecycle evidence contract."""

from __future__ import annotations

import math
from datetime import datetime
from statistics import median
from typing import Any, Mapping, Sequence


EVIDENCE_SCHEMA_VERSION = "1"
MAX_UNLOADED_SPREAD_MB = 64
MIN_WORKING_SET_RECOVERY_RATIO = 0.90
REQUIRED_CONTRACT_CHECKS = frozenset(
    {
        "managed_lifecycle_advertised",
        "real_inference",
        "provider_or_device_gpu",
        "busy_unload_rejected",
        "cancel_accounting",
        "stale_generation_rejected",
        "token_replay_rejected",
        "partial_headers_rejected",
        "full_cleanup",
    }
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "backend_name",
        "generated_at",
        "passed",
        "blockers",
        "deployment",
        "artifacts",
        "gpu",
        "cycles",
        "contract_checks",
        "final_residency",
        "runtime_ephemera_clean",
    }
)
_DEPLOYMENT_FIELDS = frozenset(
    {"git_commit", "image_id", "runtime_versions", "pool_topology"}
)
_GPU_FIELDS = frozenset(
    {
        "uuid",
        "total_memory_mb",
        "driver_version",
        "runtime_version",
        "visible_device_count",
    }
)
_ARTIFACT_FIELDS = frozenset({"kind", "name", "size_bytes", "sha256", "approval_ref"})
_CYCLE_FIELDS = frozenset(
    {
        "cycle",
        "generation",
        "context_samples_mb",
        "loaded_samples_mb",
        "unloaded_samples_mb",
        "unloaded_spread_mb",
        "working_set_recovery_ratio",
    }
)
_BLOCKER_FIELDS = frozenset({"code", "message"})
_FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "admission_token",
        "file_path",
        "image_bytes",
        "path",
        "presigned_url",
        "private_key",
        "private_seed",
        "raw_image",
        "signed_url",
        "signing_key",
        "token",
        "url",
    }
)


def memory_cycle_evidence(
    *,
    cycle: int,
    generation: str,
    context_samples_mb: Sequence[int],
    loaded_samples_mb: Sequence[int],
    unloaded_samples_mb: Sequence[int],
) -> dict[str, Any]:
    """Normalize one load/unload cycle and calculate its qualification metrics."""

    context = _memory_samples("context_samples_mb", context_samples_mb)
    loaded = _memory_samples("loaded_samples_mb", loaded_samples_mb)
    unloaded = _memory_samples("unloaded_samples_mb", unloaded_samples_mb)
    if len(context) < 5 or len(unloaded) < 5:
        raise ValueError("context and unloaded stability windows require five samples")
    if cycle < 1:
        raise ValueError("cycle must be positive")
    if not generation:
        raise ValueError("generation must be non-empty")
    working_set_mb = median(loaded) - median(context)
    recovered_mb = median(loaded) - median(unloaded)
    recovery_ratio = recovered_mb / working_set_mb if working_set_mb > 0 else 0.0
    return {
        "cycle": cycle,
        "generation": generation,
        "context_samples_mb": context,
        "loaded_samples_mb": loaded,
        "unloaded_samples_mb": unloaded,
        "unloaded_spread_mb": max(unloaded) - min(unloaded),
        "working_set_recovery_ratio": round(recovery_ratio, 6),
    }


def validate_managed_lifecycle_evidence(value: object) -> dict[str, Any]:
    """Validate exact envelope fields, nested types, and pass/fail invariants."""

    if not isinstance(value, dict):
        raise ValueError("lifecycle evidence must be a JSON object")
    fields = _TOP_LEVEL_FIELDS
    if "workload_evidence" in value:
        fields = fields | {"workload_evidence"}
        _object(value["workload_evidence"], "workload_evidence")
    _require_exact_fields(value, fields, "evidence")
    if value["schema_version"] != EVIDENCE_SCHEMA_VERSION:
        raise ValueError("unsupported lifecycle evidence schema_version")
    if not isinstance(value["backend_name"], str):
        raise ValueError("backend_name must be a string")
    if not isinstance(value["generated_at"], str):
        raise ValueError("generated_at must be a string")
    _parse_timestamp(value["generated_at"])
    if type(value["passed"]) is not bool:  # noqa: E721 - strict bool, not int
        raise ValueError("passed must be a JSON boolean")
    if type(value["runtime_ephemera_clean"]) is not bool:  # noqa: E721
        raise ValueError("runtime_ephemera_clean must be a JSON boolean")

    deployment = _object(value["deployment"], "deployment")
    _require_exact_fields(deployment, _DEPLOYMENT_FIELDS, "deployment")
    _optional_string(deployment["git_commit"], "deployment.git_commit")
    _optional_string(deployment["image_id"], "deployment.image_id")
    _string_map(deployment["runtime_versions"], "deployment.runtime_versions")
    _object(deployment["pool_topology"], "deployment.pool_topology")

    gpu = _object(value["gpu"], "gpu")
    _require_exact_fields(gpu, _GPU_FIELDS, "gpu")
    for key in ("uuid", "driver_version", "runtime_version"):
        _optional_string(gpu[key], f"gpu.{key}")
    for key in ("total_memory_mb", "visible_device_count"):
        field = gpu[key]
        if field is not None and (type(field) is not int or field < 0):
            raise ValueError(f"gpu.{key} must be a non-negative integer or null")

    artifacts = _object_list(value["artifacts"], "artifacts")
    for artifact in artifacts:
        _require_exact_fields(artifact, _ARTIFACT_FIELDS, "artifact")
        if artifact["kind"] not in {"weight", "fixture"}:
            raise ValueError("artifact.kind must be weight or fixture")
        if not isinstance(artifact["name"], str) or not artifact["name"]:
            raise ValueError("artifact.name must be a non-empty string")
        if "/" in artifact["name"] or "\\" in artifact["name"]:
            raise ValueError("artifact.name must not contain a path")
        if type(artifact["size_bytes"]) is not int or artifact["size_bytes"] <= 0:
            raise ValueError("artifact.size_bytes must be a positive integer")
        digest = artifact["sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("artifact.sha256 must be a lowercase SHA-256 digest")
        if any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("artifact.sha256 must be a lowercase SHA-256 digest")
        _optional_string(artifact["approval_ref"], "artifact.approval_ref")

    cycles = _object_list(value["cycles"], "cycles")
    for cycle in cycles:
        fields = _CYCLE_FIELDS
        if "session_observations" in cycle:
            fields = fields | {"session_observations"}
            _validate_session_observations(cycle["session_observations"])
        _require_exact_fields(cycle, fields, "cycle")
        if type(cycle["cycle"]) is not int or cycle["cycle"] < 1:
            raise ValueError("cycle.cycle must be a positive integer")
        if not isinstance(cycle["generation"], str) or not cycle["generation"]:
            raise ValueError("cycle.generation must be a non-empty string")
        expected_cycle = memory_cycle_evidence(
            cycle=cycle["cycle"],
            generation=cycle["generation"],
            context_samples_mb=cycle["context_samples_mb"],
            loaded_samples_mb=cycle["loaded_samples_mb"],
            unloaded_samples_mb=cycle["unloaded_samples_mb"],
        )
        if type(cycle["unloaded_spread_mb"]) is not int:
            raise ValueError("cycle.unloaded_spread_mb must be an integer")
        ratio = cycle["working_set_recovery_ratio"]
        if type(ratio) not in {int, float} or not math.isfinite(ratio):
            raise ValueError("cycle.working_set_recovery_ratio must be numeric")
        if cycle["unloaded_spread_mb"] != expected_cycle["unloaded_spread_mb"]:
            raise ValueError("cycle.unloaded_spread_mb does not match its samples")
        if ratio != expected_cycle["working_set_recovery_ratio"]:
            raise ValueError(
                "cycle.working_set_recovery_ratio does not match its samples"
            )

    blockers = _object_list(value["blockers"], "blockers")
    for blocker in blockers:
        _require_exact_fields(blocker, _BLOCKER_FIELDS, "blocker")
        if not all(isinstance(blocker[key], str) and blocker[key] for key in blocker):
            raise ValueError("blocker code and message must be non-empty strings")

    checks = _object(value["contract_checks"], "contract_checks")
    if any(
        not isinstance(key, str) or type(item) is not bool
        for key, item in checks.items()
    ):
        raise ValueError("contract_checks must map strings to JSON booleans")
    if value["final_residency"] is not None:
        _object(value["final_residency"], "final_residency")
    if value["passed"] != (not blockers):
        raise ValueError("passed must be true exactly when blockers is empty")
    if value["passed"]:
        _validate_passing_qualification(value)
    _ensure_json_and_secret_free(value)
    return dict(value)


def _validate_passing_qualification(value: Mapping[str, Any]) -> None:
    deployment = value["deployment"]
    gpu = value["gpu"]
    cycles = value["cycles"]
    checks = value["contract_checks"]
    if (
        not deployment["git_commit"]
        or not deployment["image_id"]
        or not deployment["runtime_versions"]
        or not deployment["pool_topology"]
    ):
        raise ValueError("passing evidence requires deployment identity")
    artifact_kinds = {item["kind"] for item in value["artifacts"]}
    if (
        not value["backend_name"]
        or not {"weight", "fixture"}.issubset(artifact_kinds)
        or any(not item["approval_ref"] for item in value["artifacts"])
    ):
        raise ValueError("passing evidence requires backend identity and artifacts")
    if (
        not gpu["uuid"]
        or not gpu["total_memory_mb"]
        or not gpu["driver_version"]
        or not gpu["runtime_version"]
        or gpu["visible_device_count"] != 1
    ):
        raise ValueError("passing evidence requires one physical GPU identity")
    if len(cycles) < 2:
        raise ValueError("passing evidence requires at least two cycles")
    if len({cycle["cycle"] for cycle in cycles}) != len(cycles) or len(
        {cycle["generation"] for cycle in cycles}
    ) != len(cycles):
        raise ValueError("passing evidence requires distinct cycles and generations")
    if any(
        cycle["unloaded_spread_mb"] > MAX_UNLOADED_SPREAD_MB
        or cycle["working_set_recovery_ratio"] < MIN_WORKING_SET_RECOVERY_RATIO
        for cycle in cycles
    ):
        raise ValueError("passing evidence does not meet memory recovery gates")
    if "onnxruntime" in deployment["runtime_versions"]:
        pools = deployment["pool_topology"].get("pools")
        expected_counts = deployment["pool_topology"].get("session_counts")
        if (
            not isinstance(pools, dict)
            or not pools
            or not isinstance(expected_counts, dict)
            or set(expected_counts) != set(pools)
            or any(
                type(count) is not int or not 1 <= count <= 128
                for count in expected_counts.values()
            )
        ):
            raise ValueError(
                "ORT qualification requires complete factory session counts"
            )
        for cycle in cycles:
            observations = cycle.get("session_observations")
            if not isinstance(observations, dict) or set(observations) != set(pools):
                raise ValueError(
                    "ORT qualification requires observations for every pool"
                )
            counts = {name: len(chains) for name, chains in observations.items()}
            if counts != expected_counts:
                raise ValueError("ORT session observations differ from factory counts")
            if any(
                chain[0] != "CUDAExecutionProvider"
                for chains in observations.values()
                for chain in chains
            ):
                raise ValueError(
                    "ORT business sessions must use CUDA as primary provider"
                )
    if REQUIRED_CONTRACT_CHECKS - checks.keys() or not all(checks.values()):
        raise ValueError("passing evidence does not meet contract checks")
    if not is_unloaded_residency(value["final_residency"]):
        raise ValueError("passing evidence requires fully unloaded final residency")
    if value["runtime_ephemera_clean"] is not True:
        raise ValueError("passing evidence requires clean runtime ephemera")


def _validate_session_observations(value: object) -> None:
    if not isinstance(value, dict) or not value or len(value) > 128:
        raise ValueError("session_observations must be a non-empty pool map")
    for name, chains in value.items():
        if (
            not isinstance(name, str)
            or not name
            or name.strip() != name
            or len(name) > 128
            or not isinstance(chains, list)
            or not chains
            or len(chains) > 128
        ):
            raise ValueError(
                "session observations contain an invalid pool or session list"
            )
        for chain in chains:
            if (
                not isinstance(chain, list)
                or not chain
                or len(chain) > 128
                or any(
                    not isinstance(provider, str)
                    or not provider
                    or provider.strip() != provider
                    for provider in chain
                )
            ):
                raise ValueError(
                    "session provider chain must contain canonical provider names"
                )


def is_unloaded_residency(value: Mapping[str, Any] | None) -> bool:
    if not isinstance(value, Mapping):
        return False
    pools = value.get("pools")
    return (
        value.get("state") == "unloaded"
        and value.get("gpu_loaded") is False
        and value.get("evictable") is False
        and value.get("active_requests") == 0
        and value.get("builders") == 0
        and value.get("borrowers") == 0
        and isinstance(pools, Mapping)
        and bool(pools)
        and all(
            isinstance(pool, Mapping) and pool.get("resident") is False
            for pool in pools.values()
        )
    )


def _memory_samples(name: str, value: Sequence[int] | object) -> list[int]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a non-empty integer array")
    samples = list(value)
    if not samples or any(type(item) is not int or item < 0 for item in samples):
        raise ValueError(f"{name} must be a non-empty non-negative integer array")
    return samples


def _require_exact_fields(
    value: Mapping[str, Any], expected: frozenset[str], name: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{name} fields do not match schema: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _object_list(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{name} must be an array of JSON objects")
    return value


def _optional_string(value: object, name: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")


def _string_map(value: object, name: str) -> None:
    mapping = _object(value, name)
    if any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in mapping.items()
    ):
        raise ValueError(f"{name} must map strings to strings")


def _ensure_json_and_secret_free(value: object, *, key: str | None = None) -> None:
    if key in _FORBIDDEN_EVIDENCE_KEYS:
        raise ValueError(f"evidence field {key!r} is forbidden")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("evidence contains a non-finite number")
    if value is None or type(value) in {bool, int, float, str}:
        return
    if isinstance(value, list):
        for item in value:
            _ensure_json_and_secret_free(item)
        return
    if isinstance(value, dict):
        for child_key, item in value.items():
            if not isinstance(child_key, str):
                raise ValueError("evidence object keys must be strings")
            _ensure_json_and_secret_free(item, key=child_key.lower())
        return
    raise ValueError(f"evidence contains non-JSON value {type(value).__name__}")


def _parse_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("generated_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("generated_at must include a timezone")
