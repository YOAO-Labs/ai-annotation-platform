"""Produce managed GPU lifecycle evidence from collected backend observations."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from aap_lifecycle_contract import (
    EVIDENCE_SCHEMA_VERSION as _EVIDENCE_SCHEMA_VERSION,
    MAX_UNLOADED_SPREAD_MB as _MAX_UNLOADED_SPREAD_MB,
    MIN_WORKING_SET_RECOVERY_RATIO as _MIN_WORKING_SET_RECOVERY_RATIO,
    REQUIRED_CONTRACT_CHECKS as _REQUIRED_CONTRACT_CHECKS,
    is_unloaded_residency as _is_unloaded_residency,
    validate_managed_lifecycle_evidence as _validate_managed_lifecycle_evidence,
)


def artifact_evidence(
    path: str | Path,
    *,
    kind: str,
    approval_ref: str | None = None,
) -> dict[str, Any]:
    """Return a path-free artifact fingerprint suitable for persisted evidence."""

    artifact_path = Path(path)
    if kind not in {"weight", "fixture"}:
        raise ValueError("artifact kind must be 'weight' or 'fixture'")
    if not artifact_path.is_file() or artifact_path.stat().st_size <= 0:
        raise ValueError(f"artifact must be a non-empty file: {artifact_path}")
    digest = hashlib.sha256()
    with artifact_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return {
        "kind": kind,
        "name": artifact_path.name,
        "size_bytes": artifact_path.stat().st_size,
        "sha256": digest.hexdigest(),
        "approval_ref": approval_ref,
    }


def build_managed_lifecycle_evidence(
    *,
    backend_name: str,
    deployment: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
    gpu: Mapping[str, Any],
    cycles: Sequence[Mapping[str, Any]],
    contract_checks: Mapping[str, bool],
    final_residency: Mapping[str, Any] | None,
    runtime_ephemera_clean: bool,
    blockers: Sequence[Mapping[str, str]] = (),
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build and strictly validate one qualification document.

    ``passed`` is derived from evidence. Callers cannot set it independently.
    """

    normalized_blockers = [dict(item) for item in blockers]
    blocker_codes = {item.get("code") for item in normalized_blockers}

    def add_blocker(code: str, message: str) -> None:
        if code not in blocker_codes:
            normalized_blockers.append({"code": code, "message": message})
            blocker_codes.add(code)

    if not backend_name:
        add_blocker("backend_identity_missing", "backend_name is required")
    if (
        not deployment.get("git_commit")
        or not deployment.get("image_id")
        or not deployment.get("runtime_versions")
        or not deployment.get("pool_topology")
    ):
        add_blocker(
            "deployment_identity_missing",
            "git commit, image, runtime versions and pool topology are required",
        )
    artifact_kinds = {item.get("kind") for item in artifacts}
    if not {"weight", "fixture"}.issubset(artifact_kinds) or any(
        not item.get("approval_ref") for item in artifacts
    ):
        add_blocker(
            "artifacts_missing",
            "approved weight and fixture fingerprints are required",
        )
    if (
        not gpu.get("uuid")
        or not isinstance(gpu.get("total_memory_mb"), int)
        or gpu.get("total_memory_mb", 0) <= 0
        or not gpu.get("driver_version")
        or not gpu.get("runtime_version")
        or gpu.get("visible_device_count") != 1
    ):
        add_blocker(
            "gpu_identity_invalid",
            "one physical GPU UUID and its total memory are required",
        )
    if len(cycles) < 2:
        add_blocker("cycles_incomplete", "at least two load/unload cycles are required")
    for item in cycles:
        if item.get("unloaded_spread_mb", _MAX_UNLOADED_SPREAD_MB + 1) > (
            _MAX_UNLOADED_SPREAD_MB
        ):
            add_blocker(
                "memory_unstable",
                "unloaded GPU memory spread exceeds 64 MiB",
            )
        if item.get("working_set_recovery_ratio", 0) < (
            _MIN_WORKING_SET_RECOVERY_RATIO
        ):
            add_blocker(
                "memory_recovery_insufficient",
                "less than 90% of the model working set was recovered",
            )
    missing_checks = _REQUIRED_CONTRACT_CHECKS - contract_checks.keys()
    failed_checks = {name for name, passed in contract_checks.items() if not passed}
    if missing_checks:
        add_blocker(
            "contract_checks_missing",
            "missing required checks: " + ", ".join(sorted(missing_checks)),
        )
    if failed_checks:
        add_blocker(
            "contract_checks_failed",
            "failed checks: " + ", ".join(sorted(failed_checks)),
        )
    if not _is_unloaded_residency(final_residency):
        add_blocker(
            "final_residency_invalid",
            "final residency must be an idle, fully unloaded pool set",
        )
    if not runtime_ephemera_clean:
        add_blocker(
            "runtime_ephemera_dirty",
            "validation left runtime artifacts or active lifecycle state",
        )

    payload = {
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "backend_name": backend_name,
        "generated_at": generated_at or _utc_now(),
        "passed": not normalized_blockers,
        "blockers": normalized_blockers,
        "deployment": dict(deployment),
        "artifacts": [dict(item) for item in artifacts],
        "gpu": dict(gpu),
        "cycles": [dict(item) for item in cycles],
        "contract_checks": dict(contract_checks),
        "final_residency": dict(final_residency) if final_residency else None,
        "runtime_ephemera_clean": runtime_ephemera_clean,
    }
    return _validate_managed_lifecycle_evidence(payload)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
