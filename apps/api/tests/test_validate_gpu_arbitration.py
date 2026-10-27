from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from types import SimpleNamespace
import uuid

import pytest
from pydantic import ValidationError

from aap_inference.gpu.contracts import GPUArbiterDispatchError, GPUArbiterErrorCode
from aap_inference.gpu.settings import (
    GPUArbiterMode,
    GPUAuthoritySettings,
    parse_gpu_resources,
)
from aap_inference.backend_transport import BackendTransport
from scripts.validate_gpu_arbitration import (
    ActionSpec,
    BackendEndpoint,
    EVIDENCE_SCHEMA,
    FaultController,
    ValidationManifest,
    _backend_physical_gpu_exact,
    _command_output,
    _database_window_check,
    _evidence_manifest,
    _preflight_allows_runtime_proof_refresh,
    _safe_endpoint,
    _safe_error,
    _sha256_json,
    _threshold_applicability,
    _thresholds,
    _timestamp_not_regressed,
    _run_action,
    _validate_run_safety,
    _write_report,
    action_overlap_ms,
    evaluate_memory_recovery,
    evaluate_preflight,
    evaluate_run,
    evaluate_stable_memory,
    parse_nvidia_smi,
    refresh_runtime_proofs,
    verify_evidence,
)


_RESOURCE_A = "node-a/GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_RESOURCE_B = "node-a/GPU-11111111-2222-3333-4444-555555555555"
_GPU_A = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_GPU_B = "GPU-11111111-2222-3333-4444-555555555555"
_BACKEND_A = "11111111-1111-4111-8111-111111111111"
_BACKEND_B = "22222222-2222-4222-8222-222222222222"
_BACKEND_C = "33333333-3333-4333-8333-333333333333"


def _manifest_payload(
    *,
    scenario: str = "single-card-co-residency",
    resources: list[dict] | None = None,
    actions: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "cohort_id": "p5c4-test",
        "node_id": "node-a",
        "scenario": scenario,
        "resources": resources or [{"resource_id": _RESOURCE_A, "gpu_uuid": _GPU_A}],
        "actions": actions
        or [
            {
                "id": "warm-a",
                "role": "requester",
                "backend_id": _BACKEND_A,
                "resource_id": _RESOURCE_A,
                "operation": "warmup",
                "body": {},
            },
            {
                "id": "warm-b",
                "role": "peer",
                "backend_id": _BACKEND_B,
                "resource_id": _RESOURCE_A,
                "operation": "warmup",
                "body": {},
            },
        ],
    }


def _manifest(**kwargs) -> ValidationManifest:
    return ValidationManifest.model_validate(_manifest_payload(**kwargs), strict=True)


def _gpu_samples(*, a: int = 1000, b: int = 2000) -> list[dict]:
    return [
        {
            "gpus": [
                {
                    "index": 0,
                    "uuid": _GPU_A,
                    "memory_total_mb": 24576,
                    "memory_used_mb": a,
                },
                {
                    "index": 1,
                    "uuid": _GPU_B,
                    "memory_total_mb": 24576,
                    "memory_used_mb": b,
                },
            ]
        }
        for _ in range(5)
    ]


def _ready_redis_snapshot(
    resource_id: str,
    allocations: list[dict],
) -> dict:
    normalized_allocations = [
        {
            **allocation,
            "generation": allocation.get("generation", "1"),
            "eviction_priority": allocation.get("eviction_priority", 0),
            "max_concurrency": allocation.get("max_concurrency", 4),
            "evictable": allocation.get(
                "evictable", allocation.get("state") == "resident"
            ),
        }
        for allocation in allocations
    ]
    return {
        "status": "ready",
        "snapshot": {
            "resource_id": resource_id,
            "allocatable_mb": 20_000,
            "committed_mb": sum(
                allocation["budget_mb"]
                for allocation in normalized_allocations
                if allocation["state"] != "unloaded"
            ),
            "backend_memberships": [
                {
                    "backend_id": allocation["backend_id"],
                    "membership_epoch": 1,
                    "state": "active",
                }
                for allocation in normalized_allocations
            ],
            "allocations": normalized_allocations,
            "leases": [],
            "card_queue": [],
            "backend_queues": [],
            "transition": None,
        },
    }


def _with_final_truth(
    after: dict,
    *,
    physical_identity_by_resource: dict[str, tuple[str, int]] | None = None,
) -> tuple[dict, dict]:
    after = deepcopy(after)
    registries = []
    memberships = []
    fences = []
    backends = {}
    physical_gpus = []
    for resource_id, resource in after["redis"]["resources"].items():
        gpu_uuid, gpu_index = (
            physical_identity_by_resource[resource_id]
            if physical_identity_by_resource is not None
            else ((_GPU_A, 0) if resource_id == _RESOURCE_A else (_GPU_B, 1))
        )
        physical_gpus.append(
            {"uuid": gpu_uuid, "index": gpu_index, "memory_total_mb": 24576}
        )
        for allocation in resource["snapshot"]["allocations"]:
            backend_id = allocation["backend_id"]
            state = allocation["state"]
            registries.append(
                {
                    "backend_id": backend_id,
                    "gpu_resource_id": resource_id,
                    "vram_budget_mb": allocation["budget_mb"],
                    "eviction_priority": allocation["eviction_priority"],
                    "max_concurrency": allocation["max_concurrency"],
                    "health": {
                        "gpu_arbiter_probe": {"managed_lifecycle_sha256": "capability"}
                    },
                }
            )
            memberships.append(
                {
                    "backend_id": backend_id,
                    "gpu_resource_id": resource_id,
                    "membership_epoch": 1,
                    "state": "active",
                    "vram_budget_mb": allocation["budget_mb"],
                    "eviction_priority": allocation["eviction_priority"],
                    "max_concurrency": allocation["max_concurrency"],
                }
            )
            fences.append(
                {
                    "backend_id": backend_id,
                    "generation_high_water": int(allocation["generation"]),
                    "control_epoch_high_water": 1,
                    "runtime_epoch_high_water": 1,
                    "token_expiry_high_water": "2026-07-16T01:00:00+00:00",
                }
            )
            resident = state == "resident"
            backends[backend_id] = {
                "healthy": True,
                "challenge_echoed": True,
                "managed_lifecycle_sha256": "capability",
                "gpu_info": {"device_index": gpu_index},
                "residency": {
                    "state": state,
                    "gpu_loaded": resident,
                    "active_requests": 0,
                    "builders": 0,
                    "borrowers": 0,
                    "draining": False,
                    "evictable": resident,
                    "generation": allocation["generation"],
                    "pools": {
                        "pool": {
                            "resident": resident,
                            "device": "cuda" if resident else None,
                            "provider": None,
                        }
                    },
                    "boot_id": "boot-1",
                    "lifecycle_gate": "enforce",
                    "control_epoch": "1",
                    "identity": {
                        "audience": "aap-gpu-lifecycle",
                        "backend_registry_id": backend_id,
                        "gpu_resource_id": resource_id,
                    },
                },
            }
    database = {
        "topology_fingerprint": "stable-topology",
        "registries": registries,
        "memberships": memberships,
        "fences": fences,
    }
    before = {"database": deepcopy(database)}
    after["database"] = database
    after["backends"] = backends
    after["nvidia_smi"] = {"gpus": physical_gpus}
    return before, after


def test_manifest_rejects_duplicate_actions_and_unknown_fields() -> None:
    payload = _manifest_payload()
    payload["actions"][1]["id"] = payload["actions"][0]["id"]
    with pytest.raises(ValidationError, match="action ids must be unique"):
        ValidationManifest.model_validate(payload, strict=True)

    payload = _manifest_payload()
    payload["resources"][0]["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ValidationManifest.model_validate(payload, strict=True)


def test_manifest_rejects_non_whitelisted_or_malformed_action_body() -> None:
    payload = _manifest_payload()
    payload["actions"][0]["operation"] = "unload"
    with pytest.raises(ValidationError):
        ValidationManifest.model_validate(payload, strict=True)

    payload = _manifest_payload()
    payload["actions"][0]["operation"] = "predict"
    payload["actions"][0]["body"] = {"task": {}}
    with pytest.raises(ValidationError, match="predict body must contain tasks"):
        ValidationManifest.model_validate(payload, strict=True)


def test_capacity_rejection_manifest_requires_one_expected_requester() -> None:
    action = {
        "id": "reject-a",
        "role": "requester",
        "backend_id": _BACKEND_A,
        "resource_id": _RESOURCE_A,
        "operation": "warmup",
        "body": {},
        "expected_error_code": "gpu_capacity_unavailable",
    }
    manifest = _manifest(
        scenario="single-card-capacity-rejection",
        actions=[action],
    )
    assert manifest.actions[0].expected_error_code == "gpu_capacity_unavailable"

    missing = deepcopy(action)
    missing.pop("expected_error_code")
    with pytest.raises(ValidationError, match="capacity-rejection requires"):
        _manifest(
            scenario="single-card-capacity-rejection",
            actions=[missing],
        )

    with pytest.raises(ValidationError, match="only valid for capacity-rejection"):
        _manifest(actions=[action])


def test_nvidia_smi_parser_keeps_exact_multi_gpu_and_process_identity() -> None:
    parsed = parse_nvidia_smi(
        f"0, {_GPU_A}, 24576, 1024\n1, {_GPU_B}, 24576, 2048\n",
        f"{_GPU_A}, 101, python, 900\n{_GPU_B}, 202, python, N/A\n",
    )

    assert [(gpu["index"], gpu["uuid"]) for gpu in parsed["gpus"]] == [
        (0, _GPU_A),
        (1, _GPU_B),
    ]
    assert parsed["compute_processes"][1]["used_memory_mb"] is None

    with pytest.raises(ValueError, match="process row identity"):
        parse_nvidia_smi(
            f"0, {_GPU_A}, 24576, 1024\n",
            f"{_GPU_B}, 202, python, 100\n",
        )


def test_frozen_memory_threshold_boundaries() -> None:
    assert evaluate_stable_memory([100, 164, 120, 130, 140])["passed"] is True
    assert evaluate_stable_memory([100, 165, 120, 130, 140])["passed"] is False

    exact = evaluate_memory_recovery(
        context_samples_mb=[100] * 5,
        loaded_mb=1100,
        unloaded_samples_mb=[200] * 5,
        gpu_total_mb=24576,
    )
    below = evaluate_memory_recovery(
        context_samples_mb=[100] * 5,
        loaded_mb=1100,
        unloaded_samples_mb=[201] * 5,
        gpu_total_mb=24576,
    )
    assert exact["passed"] is True
    assert exact["recovery_ratio"] == pytest.approx(0.90)
    assert below["passed"] is False


def test_run_safety_blocks_production_and_mismatched_confirmation() -> None:
    with pytest.raises(PermissionError, match="production"):
        _validate_run_safety(
            environment="production",
            run_id="run-1",
            confirm_run_id="run-1",
        )
    with pytest.raises(PermissionError, match="exactly match"):
        _validate_run_safety(
            environment="development",
            run_id="run-1",
            confirm_run_id="run-2",
        )
    _validate_run_safety(
        environment="staging",
        run_id="run-1",
        confirm_run_id="run-1",
    )


@pytest.mark.asyncio
async def test_run_refreshes_every_runtime_proof_and_fails_closed() -> None:
    calls: list[tuple[str, str]] = []

    async def refresher(backend_id, challenge):
        calls.append((str(backend_id), challenge))
        if str(backend_id) == _BACKEND_B:
            raise TimeoutError("health refresh timed out")
        return True

    checks = await refresh_runtime_proofs(
        [_BACKEND_B, _BACKEND_A, _BACKEND_A],
        refresher=refresher,
    )

    assert [backend_id for backend_id, _challenge in calls] == [
        _BACKEND_A,
        _BACKEND_B,
    ]
    assert all(len(challenge) == 64 for _backend_id, challenge in calls)
    assert [check["status"] for check in checks] == ["passed", "blocked"]
    assert checks[1]["details"]["backend_id"] == _BACKEND_B
    assert "timed out" in checks[1]["details"]["error"]


def test_run_only_refreshes_proofs_after_other_preflight_gates_pass() -> None:
    cached_proof_only = {
        "checks": [
            {"code": "redis_ready", "status": "passed"},
            {"code": "backend_live_proof", "status": "blocked"},
        ]
    }
    assert _preflight_allows_runtime_proof_refresh(cached_proof_only) is True

    unsafe = deepcopy(cached_proof_only)
    unsafe["checks"][0]["status"] = "blocked"
    assert _preflight_allows_runtime_proof_refresh(unsafe) is False
    assert _preflight_allows_runtime_proof_refresh({"checks": []}) is False


def test_endpoint_and_error_evidence_redacts_credentials() -> None:
    endpoint = _safe_endpoint("https://user:password@example.com:9443/backend?token=x")
    assert endpoint["origin"] == "https://example.com:9443/backend"
    error = _safe_error(
        RuntimeError(
            "connect redis://:password@cache/0?X-Amz-Signature=query-secret with "
            "Authorization: Bearer abc+/def== and X-Api-Key: low-entropy"
        )
    )
    assert "password" not in error
    assert "query-secret" not in error
    assert "abc+/def==" not in error
    assert "low-entropy" not in error


def test_physical_gpu_identity_requires_every_reported_field_to_match() -> None:
    assert _backend_physical_gpu_exact(
        {
            "gpu_info": {
                "device_uuid": _GPU_A,
                "device_index": 0,
                "physical_device_token": "index:0",
            }
        },
        gpu_uuid=_GPU_A,
        gpu_index=0,
    )
    assert _backend_physical_gpu_exact(
        {"gpu_info": {"physical_device_token": "index:0"}},
        gpu_uuid=_GPU_A,
        gpu_index=0,
    )
    assert not _backend_physical_gpu_exact(
        {"gpu_info": {"device_uuid": _GPU_A, "device_index": 1}},
        gpu_uuid=_GPU_A,
        gpu_index=0,
    )
    assert not _backend_physical_gpu_exact(
        {"gpu_info": {"physical_device_token": "index:1"}},
        gpu_uuid=_GPU_A,
        gpu_index=0,
    )


def test_token_horizon_allows_initial_null_but_never_regresses_to_null() -> None:
    horizon = "2026-07-16T01:00:00Z"
    assert _timestamp_not_regressed(None, None)
    assert _timestamp_not_regressed(None, horizon)
    assert not _timestamp_not_regressed(horizon, None)


def test_preflight_requires_exact_db_redis_health_and_gpu_identity() -> None:
    manifest = _manifest()
    resources, config_errors = parse_gpu_resources(
        json.dumps(
            {
                _RESOURCE_A: {
                    "node_id": "node-a",
                    "physical_device_token": _GPU_A,
                    "allocatable_mb": 20_000,
                    "mode": "enforce",
                }
            }
        )
    )
    config = GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.ENFORCE,
        gpu_arbiter_resources=resources,
        gpu_arbiter_config_errors=config_errors,
    )
    database = {
        "database_heads": ["0128"],
        "registries": [
            {
                "backend_id": backend_id,
                "gpu_resource_id": _RESOURCE_A,
                "vram_budget_mb": 4000,
                "eviction_priority": 0,
                "max_concurrency": 4,
                "health": {"gpu_arbiter_probe": {"managed_lifecycle_sha256": "cap"}},
            }
            for backend_id in (_BACKEND_A, _BACKEND_B, _BACKEND_C)
        ],
        "memberships": [
            {
                "backend_id": backend_id,
                "gpu_resource_id": _RESOURCE_A,
                "membership_epoch": 1,
                "state": "active",
                "vram_budget_mb": 4000,
                "eviction_priority": 0,
                "max_concurrency": 4,
            }
            for backend_id in (_BACKEND_A, _BACKEND_B, _BACKEND_C)
        ],
        "fences": [
            {
                "backend_id": backend_id,
                "generation_high_water": 1,
                "control_epoch_high_water": 2,
                "runtime_epoch_high_water": 1,
            }
            for backend_id in (_BACKEND_A, _BACKEND_B, _BACKEND_C)
        ],
    }
    redis_snapshot = {
        "resources": {
            _RESOURCE_A: _ready_redis_snapshot(
                _RESOURCE_A,
                [
                    {"backend_id": _BACKEND_A, "state": "unloaded", "budget_mb": 4000},
                    {"backend_id": _BACKEND_B, "state": "unloaded", "budget_mb": 4000},
                    {"backend_id": _BACKEND_C, "state": "unloaded", "budget_mb": 4000},
                ],
            )
        }
    }
    live_backends = {
        backend_id: {
            "healthy": True,
            "challenge_echoed": True,
            "managed_lifecycle_sha256": "cap",
            "residency": {
                "lifecycle_gate": "enforce",
                "identity": {
                    "backend_registry_id": backend_id,
                    "gpu_resource_id": _RESOURCE_A,
                },
            },
            "gpu_info": {"device_index": 0},
        }
        for backend_id in (_BACKEND_A, _BACKEND_B, _BACKEND_C)
    }
    nvidia = {"gpus": [{"index": 0, "uuid": _GPU_A}]}

    checks = evaluate_preflight(
        manifest=manifest,
        config=config,
        database=database,
        redis_snapshot=redis_snapshot,
        live_backends=live_backends,
        nvidia=nvidia,
        code_database_heads=["0128"],
    )

    assert checks
    assert not [check for check in checks if check["status"] != "passed"]

    cold_snapshot = deepcopy(redis_snapshot)
    cold_allocations = cold_snapshot["resources"][_RESOURCE_A]["snapshot"][
        "allocations"
    ]
    cold_allocations[:] = [
        allocation
        for allocation in cold_allocations
        if allocation["backend_id"] != _BACKEND_A
    ]
    cold_snapshot["resources"][_RESOURCE_A]["snapshot"]["committed_mb"] = 0
    cold_checks = evaluate_preflight(
        manifest=manifest,
        config=config,
        database=database,
        redis_snapshot=cold_snapshot,
        live_backends=live_backends,
        nvidia=nvidia,
        code_database_heads=["0128"],
    )
    assert not [check for check in cold_checks if check["status"] != "passed"]

    untrusted_live = deepcopy(live_backends)
    untrusted_live[_BACKEND_C]["challenge_echoed"] = False
    untrusted_checks = evaluate_preflight(
        manifest=manifest,
        config=config,
        database=database,
        redis_snapshot=redis_snapshot,
        live_backends=untrusted_live,
        nvidia=nvidia,
        code_database_heads=["0128"],
    )
    assert (
        next(
            check
            for check in untrusted_checks
            if check["code"] == "backend_live_proof"
            and check["details"]["backend_id"] == _BACKEND_C
        )["status"]
        == "blocked"
    )

    stale_fence_database = deepcopy(database)
    next(
        row for row in stale_fence_database["fences"] if row["backend_id"] == _BACKEND_C
    )["generation_high_water"] = 0
    stale_fence_checks = evaluate_preflight(
        manifest=manifest,
        config=config,
        database=stale_fence_database,
        redis_snapshot=redis_snapshot,
        live_backends=live_backends,
        nvidia=nvidia,
        code_database_heads=["0128"],
    )
    assert (
        next(
            check
            for check in stale_fence_checks
            if check["code"] == "backend_fence_active"
            and check["details"]["backend_id"] == _BACKEND_C
        )["status"]
        == "blocked"
    )

    drifted = deepcopy(redis_snapshot)
    drifted["resources"][_RESOURCE_A]["snapshot"]["backend_memberships"][0][
        "membership_epoch"
    ] = 2
    drift_checks = evaluate_preflight(
        manifest=manifest,
        config=config,
        database=database,
        redis_snapshot=drifted,
        live_backends=live_backends,
        nvidia=nvidia,
        code_database_heads=["0128"],
    )
    assert (
        next(
            check
            for check in drift_checks
            if check["code"] == "redis_membership_domain_exact"
        )["status"]
        == "blocked"
    )


def test_database_window_blocks_control_state_drift() -> None:
    before = {
        "database_clock": "2026-07-16T00:00:00Z",
        "control_fingerprint": "stable",
    }
    after = {
        "database_clock": "2026-07-16T00:00:01Z",
        "control_fingerprint": "stable",
    }

    assert _database_window_check(before, after)["status"] == "passed"
    after["control_fingerprint"] = "drifted"
    assert _database_window_check(before, after)["status"] == "blocked"


def test_run_evaluator_proves_single_card_co_residency() -> None:
    manifest = _manifest()
    allocations = [
        {"backend_id": _BACKEND_A, "state": "resident", "budget_mb": 4000},
        {"backend_id": _BACKEND_B, "state": "resident", "budget_mb": 4000},
    ]
    after = {
        "redis": {
            "resources": {_RESOURCE_A: _ready_redis_snapshot(_RESOURCE_A, allocations)}
        }
    }
    before, after = _with_final_truth(after)
    actions = [
        {
            "id": "warm-a",
            "role": "requester",
            "backend_id": _BACKEND_A,
            "operation": "warmup",
            "status": "passed",
            "resource_id": _RESOURCE_A,
            "started_monotonic_ms": 0,
            "finished_monotonic_ms": 1000,
            "http_started_monotonic_ms": 0,
            "http_finished_monotonic_ms": 1000,
        },
        {
            "id": "warm-b",
            "role": "peer",
            "backend_id": _BACKEND_B,
            "operation": "warmup",
            "status": "passed",
            "resource_id": _RESOURCE_A,
            "started_monotonic_ms": 100,
            "finished_monotonic_ms": 900,
        },
    ]

    checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=after,
        baseline_samples=_gpu_samples(a=1000),
        during_samples=_gpu_samples(a=9000),
        recovery_samples=_gpu_samples(a=9000),
        fault=None,
    )

    assert not [check for check in checks if check["status"] != "passed"]
    assert (
        next(check for check in checks if check["code"] == "single_card_co_residency")[
            "status"
        ]
        == "passed"
    )

    drifted_after = deepcopy(after)
    drifted_after["backends"][_BACKEND_A]["residency"]["gpu_loaded"] = False
    drifted_checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=drifted_after,
        baseline_samples=_gpu_samples(a=1000),
        during_samples=_gpu_samples(a=9000),
        recovery_samples=_gpu_samples(a=9000),
        fault=None,
    )
    assert (
        next(
            check
            for check in drifted_checks
            if check["code"] == "final_backend_truth_exact"
            and check["details"]["backend_id"] == _BACKEND_A
        )["status"]
        == "blocked"
    )


def test_run_evaluator_proves_capacity_rejection_before_http() -> None:
    manifest = _manifest(
        scenario="single-card-capacity-rejection",
        actions=[
            {
                "id": "reject-a",
                "role": "requester",
                "backend_id": _BACKEND_A,
                "resource_id": _RESOURCE_A,
                "operation": "warmup",
                "body": {},
                "expected_error_code": "gpu_capacity_unavailable",
            }
        ],
    )
    after = {
        "redis": {
            "resources": {
                _RESOURCE_A: _ready_redis_snapshot(
                    _RESOURCE_A,
                    [
                        {
                            "backend_id": _BACKEND_A,
                            "state": "unloaded",
                            "budget_mb": 9000,
                        },
                        {
                            "backend_id": _BACKEND_B,
                            "state": "resident",
                            "budget_mb": 14000,
                            "eviction_priority": 10,
                        },
                    ],
                )
            }
        }
    }
    before, after = _with_final_truth(after)
    before["redis"] = deepcopy(after["redis"])
    actions = [
        {
            "id": "reject-a",
            "role": "requester",
            "backend_id": _BACKEND_A,
            "resource_id": _RESOURCE_A,
            "operation": "warmup",
            "status": "passed",
            "expected_error_code": "gpu_capacity_unavailable",
            "error_code": "gpu_capacity_unavailable",
            "error_http_status": 503,
            "started_monotonic_ms": 0.0,
            "finished_monotonic_ms": 10.0,
        }
    ]

    checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=after,
        baseline_samples=_gpu_samples(),
        during_samples=[],
        recovery_samples=_gpu_samples(),
        fault=None,
    )

    assert not [check for check in checks if check["status"] != "passed"]
    drifted = deepcopy(after)
    drifted["redis"]["resources"][_RESOURCE_A]["snapshot"]["committed_mb"] += 1
    drifted_checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=drifted,
        baseline_samples=_gpu_samples(),
        during_samples=[],
        recovery_samples=_gpu_samples(),
        fault=None,
    )
    assert (
        next(
            check
            for check in drifted_checks
            if check["code"] == "single_card_capacity_rejected_before_http"
        )["status"]
        == "blocked"
    )


def test_run_evaluator_requires_dual_card_overlap() -> None:
    manifest = _manifest(
        scenario="dual-card",
        resources=[
            {"resource_id": _RESOURCE_A, "gpu_uuid": _GPU_A},
            {"resource_id": _RESOURCE_B, "gpu_uuid": _GPU_B},
        ],
        actions=[
            {
                "id": "warm-a",
                "role": "peer",
                "backend_id": _BACKEND_A,
                "resource_id": _RESOURCE_A,
                "operation": "warmup",
                "body": {},
            },
            {
                "id": "warm-b",
                "role": "peer",
                "backend_id": _BACKEND_B,
                "resource_id": _RESOURCE_B,
                "operation": "warmup",
                "body": {},
            },
        ],
    )
    after = {
        "redis": {
            "resources": {
                _RESOURCE_A: _ready_redis_snapshot(
                    _RESOURCE_A,
                    [
                        {
                            "backend_id": _BACKEND_A,
                            "state": "resident",
                            "budget_mb": 4000,
                        }
                    ],
                ),
                _RESOURCE_B: _ready_redis_snapshot(
                    _RESOURCE_B,
                    [
                        {
                            "backend_id": _BACKEND_B,
                            "state": "resident",
                            "budget_mb": 4000,
                        },
                        {
                            "backend_id": _BACKEND_C,
                            "state": "resident",
                            "budget_mb": 1000,
                        },
                    ],
                ),
            }
        }
    }
    before, after = _with_final_truth(after)
    before["redis"] = deepcopy(after["redis"])
    actions = [
        {
            "id": "warm-a",
            "role": "peer",
            "backend_id": _BACKEND_A,
            "operation": "warmup",
            "status": "passed",
            "resource_id": _RESOURCE_A,
            "started_monotonic_ms": 0,
            "finished_monotonic_ms": 1000,
            "http_started_monotonic_ms": 0,
            "http_finished_monotonic_ms": 1000,
        },
        {
            "id": "warm-b",
            "role": "peer",
            "backend_id": _BACKEND_B,
            "operation": "warmup",
            "status": "passed",
            "resource_id": _RESOURCE_B,
            "started_monotonic_ms": 400,
            "finished_monotonic_ms": 1100,
            "http_started_monotonic_ms": 400,
            "http_finished_monotonic_ms": 1100,
        },
    ]
    assert action_overlap_ms(actions[0], actions[1]) == 600

    checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=after,
        baseline_samples=_gpu_samples(),
        during_samples=[{"redis": after["redis"], "backends": after["backends"]}],
        recovery_samples=_gpu_samples(a=5000, b=6000),
        fault=None,
    )

    assert not [check for check in checks if check["status"] != "passed"]

    loading_sample = deepcopy(after)
    for backend in loading_sample["backends"].values():
        backend["residency"]["state"] = "loading"
        backend["residency"]["gpu_loaded"] = None
    boundary_checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=after,
        baseline_samples=_gpu_samples(),
        during_samples=[loading_sample],
        recovery_samples=_gpu_samples(a=5000, b=6000),
        fault=None,
    )
    assert (
        next(
            check
            for check in boundary_checks
            if check["code"] == "multi_resource_gpu_execution"
        )["status"]
        == "passed"
    )

    removed_peer = deepcopy(after)
    removed_peer["redis"]["resources"][_RESOURCE_B]["snapshot"]["allocations"] = [
        allocation
        for allocation in removed_peer["redis"]["resources"][_RESOURCE_B]["snapshot"][
            "allocations"
        ]
        if allocation["backend_id"] != _BACKEND_C
    ]
    removed_peer["redis"]["resources"][_RESOURCE_B]["snapshot"]["committed_mb"] = 4000
    removed_checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=removed_peer,
        baseline_samples=_gpu_samples(),
        during_samples=[{"redis": after["redis"], "backends": after["backends"]}],
        recovery_samples=_gpu_samples(a=5000, b=6000),
        fault=None,
    )
    assert (
        next(
            check
            for check in removed_checks
            if check["code"] == "final_backend_truth_exact"
            and check["details"]["backend_id"] == _BACKEND_C
        )["status"]
        == "blocked"
    )


def test_run_evaluator_requires_observed_single_card_victim_transition() -> None:
    manifest = _manifest(
        scenario="single-card-eviction",
        actions=[
            {
                "id": "victim-predict",
                "role": "victim",
                "backend_id": _BACKEND_A,
                "resource_id": _RESOURCE_A,
                "operation": "predict",
                "body": {"tasks": []},
            },
            {
                "id": "requester-warmup",
                "role": "requester",
                "backend_id": _BACKEND_B,
                "resource_id": _RESOURCE_A,
                "operation": "warmup",
                "body": {},
            },
        ],
    )
    after = {
        "redis": {
            "resources": {
                _RESOURCE_A: _ready_redis_snapshot(
                    _RESOURCE_A,
                    [
                        {
                            "backend_id": _BACKEND_A,
                            "state": "unloaded",
                            "budget_mb": 8000,
                        },
                        {
                            "backend_id": _BACKEND_B,
                            "state": "resident",
                            "budget_mb": 1000,
                        },
                    ],
                )
            }
        }
    }
    before, after = _with_final_truth(after)
    resident = _ready_redis_snapshot(
        _RESOURCE_A,
        [
            {"backend_id": _BACKEND_A, "state": "resident", "budget_mb": 8000},
            {"backend_id": _BACKEND_B, "state": "unloaded", "budget_mb": 1000},
        ],
    )
    draining = deepcopy(resident)
    draining["snapshot"]["allocations"][0]["state"] = "draining"
    draining["snapshot"]["transition"] = {
        "operation": "evict",
        "backend_id": _BACKEND_A,
        "requester_backend_id": _BACKEND_B,
        "eviction_branch": "unload",
    }
    during = [
        {"redis": {"resources": {_RESOURCE_A: resident}}},
        {"redis": {"resources": {_RESOURCE_A: draining}}},
    ]

    checks = evaluate_run(
        manifest=manifest,
        actions=[
            {
                "id": "victim-predict",
                "role": "victim",
                "backend_id": _BACKEND_A,
                "operation": "predict",
                "status": "passed",
                "resource_id": _RESOURCE_A,
                "started_monotonic_ms": 0,
                "finished_monotonic_ms": 1000,
            },
            {
                "id": "requester-warmup",
                "role": "requester",
                "backend_id": _BACKEND_B,
                "operation": "warmup",
                "status": "passed",
                "resource_id": _RESOURCE_A,
                "started_monotonic_ms": 500,
                "finished_monotonic_ms": 1500,
            },
        ],
        before=before,
        after=after,
        baseline_samples=_gpu_samples(a=100),
        during_samples=during,
        recovery_samples=_gpu_samples(a=200),
        fault=None,
    )

    assert not [check for check in checks if check["status"] != "passed"]
    failed = evaluate_run(
        manifest=manifest,
        actions=[
            {
                "id": "victim-predict",
                "role": "victim",
                "backend_id": _BACKEND_A,
                "operation": "predict",
                "status": "passed",
                "resource_id": _RESOURCE_A,
                "started_monotonic_ms": 0,
                "finished_monotonic_ms": 1000,
            },
            {
                "id": "requester-warmup",
                "role": "requester",
                "backend_id": _BACKEND_B,
                "operation": "warmup",
                "status": "passed",
                "resource_id": _RESOURCE_A,
                "started_monotonic_ms": 500,
                "finished_monotonic_ms": 1500,
            },
        ],
        before=before,
        after=after,
        baseline_samples=_gpu_samples(a=100),
        during_samples=during[:1],
        recovery_samples=_gpu_samples(a=200),
        fault=None,
    )
    assert (
        next(
            check
            for check in failed
            if check["code"] == "single_card_victim_transition_observed"
        )["status"]
        == "blocked"
    )


def test_fault_controller_hits_only_once() -> None:
    action = _manifest().actions[0]
    fault = FaultController(kind="response-lost-after-http", target=action.id)
    assert fault.hit_action(action) is True
    assert fault.hit_action(action) is False
    assert fault.hits == 1
    assert fault.hit_action_id == action.id


@pytest.mark.asyncio
async def test_run_action_cancels_and_reaps_http_child_on_injected_cancel(
    monkeypatch,
) -> None:
    request_cancelled = asyncio.Event()

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                request_cancelled.set()

    monkeypatch.setattr(
        BackendTransport, "_http_client", lambda _self, **_kwargs: FakeClient()
    )

    @asynccontextmanager
    async def dispatch_factory(_request):
        yield SimpleNamespace(
            generation="1",
            admission_token="signed-token",
            report_response=lambda _status: None,
        )

    async def database_clock() -> str:
        return "2026-07-16T00:00:00Z"

    action = ActionSpec.model_validate(
        {
            "id": "cancel-action",
            "role": "peer",
            "backend_id": _BACKEND_A,
            "resource_id": _RESOURCE_A,
            "operation": "warmup",
            "body": {},
        },
        strict=True,
    )
    fault = FaultController(kind="cancel-after-grant", target=action.id)
    row = await _run_action(
        action,
        endpoint=BackendEndpoint(
            backend_id=_BACKEND_A,
            resource_id=_RESOURCE_A,
            url="http://backend.invalid",
            auth_method="none",
            auth_token=None,
        ),
        dispatch_factory=dispatch_factory,
        database_clock=database_clock,
        fault=fault,
    )

    assert row["status"] == "fault_injected"
    assert row["grant_generation"] == "1"
    assert request_cancelled.is_set()
    assert fault.hit_action_id == action.id


@pytest.mark.asyncio
async def test_run_action_accepts_exact_capacity_error_before_http() -> None:
    @asynccontextmanager
    async def dispatch_factory(_request):
        if False:
            yield None
        raise GPUArbiterDispatchError(GPUArbiterErrorCode.CAPACITY_UNAVAILABLE)

    async def database_clock() -> str:
        raise AssertionError("capacity rejection must not start HTTP timing")

    action = ActionSpec.model_validate(
        {
            "id": "capacity-rejected",
            "role": "requester",
            "backend_id": _BACKEND_A,
            "resource_id": _RESOURCE_A,
            "operation": "warmup",
            "body": {},
            "expected_error_code": "gpu_capacity_unavailable",
        },
        strict=True,
    )
    row = await _run_action(
        action,
        endpoint=BackendEndpoint(
            _BACKEND_A, _RESOURCE_A, "http://backend.invalid", "none", None
        ),
        dispatch_factory=dispatch_factory,
        database_clock=database_clock,
        fault=None,
    )

    assert row["status"] == "passed"
    assert row["error_code"] == "gpu_capacity_unavailable"
    assert row["error_http_status"] == 503
    assert "grant_generation" not in row
    assert "http_started_monotonic_ms" not in row


@pytest.mark.asyncio
async def test_command_timeout_terminates_kills_and_reaps_subprocess(
    monkeypatch,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.communicate_calls = 0
            self.terminated = False
            self.killed = False

        async def communicate(self):
            self.communicate_calls += 1
            if self.killed:
                return b"", b""
            await asyncio.Event().wait()

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    process = FakeProcess()

    async def create_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        "scripts.validate_gpu_arbitration.asyncio.create_subprocess_exec",
        create_subprocess,
    )
    monkeypatch.setattr(
        "scripts.validate_gpu_arbitration.NVIDIA_SMI_TIMEOUT_SECONDS", 0.001
    )
    monkeypatch.setattr(
        "scripts.validate_gpu_arbitration.SUBPROCESS_TERMINATE_TIMEOUT_SECONDS",
        0.001,
    )

    with pytest.raises(TimeoutError):
        await _command_output("nvidia-smi")

    assert process.terminated is True
    assert process.killed is True
    assert process.communicate_calls == 3


@pytest.mark.asyncio
async def test_concurrent_health_fault_is_attributed_to_exact_action(
    monkeypatch,
) -> None:
    class FakeResponse:
        status_code = 200
        content = b"{}"

        @staticmethod
        def raise_for_status() -> None:
            return None

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs):
            await asyncio.sleep(0)
            return FakeResponse()

    monkeypatch.setattr(
        BackendTransport, "_http_client", lambda _self, **_kwargs: FakeClient()
    )
    fault = FaultController(kind="health-timeout", target=_BACKEND_A)

    @asynccontextmanager
    async def dispatch_factory(request):
        await asyncio.sleep(0)
        if str(request.backend_id) == _BACKEND_A:
            assert fault.hit_health(uuid.UUID(_BACKEND_A))
            raise TimeoutError("injected health-timeout")
        yield SimpleNamespace(
            generation="1",
            admission_token="signed-token",
            report_response=lambda _status: None,
        )

    async def database_clock() -> str:
        return "2026-07-16T00:00:00Z"

    def action(action_id: str, backend_id: str, resource_id: str) -> ActionSpec:
        return ActionSpec.model_validate(
            {
                "id": action_id,
                "role": "peer",
                "backend_id": backend_id,
                "resource_id": resource_id,
                "operation": "warmup",
                "body": {},
            },
            strict=True,
        )

    rows = await asyncio.gather(
        _run_action(
            action("health-a", _BACKEND_A, _RESOURCE_A),
            endpoint=BackendEndpoint(
                _BACKEND_A, _RESOURCE_A, "http://a.invalid", "none", None
            ),
            dispatch_factory=dispatch_factory,
            database_clock=database_clock,
            fault=fault,
        ),
        _run_action(
            action("peer-b", _BACKEND_B, _RESOURCE_B),
            endpoint=BackendEndpoint(
                _BACKEND_B, _RESOURCE_B, "http://b.invalid", "none", None
            ),
            dispatch_factory=dispatch_factory,
            database_clock=database_clock,
            fault=fault,
        ),
    )

    assert {row["id"]: row["status"] for row in rows} == {
        "health-a": "fault_injected",
        "peer-b": "passed",
    }
    assert fault.hit_action_id == "health-a"


@pytest.mark.asyncio
async def test_wrapped_victim_health_fault_is_attributed_to_requester_action() -> None:
    fault = FaultController(kind="health-timeout", target=_BACKEND_A)

    @asynccontextmanager
    async def dispatch_factory(_request):
        assert fault.hit_health(uuid.UUID(_BACKEND_A))
        raise GPUArbiterDispatchError(GPUArbiterErrorCode.CAPACITY_UNAVAILABLE)
        yield  # pragma: no cover - async context manager shape only

    async def database_clock() -> str:
        return "2026-07-16T00:00:00Z"

    action = ActionSpec.model_validate(
        {
            "id": "requester-b",
            "role": "requester",
            "backend_id": _BACKEND_B,
            "resource_id": _RESOURCE_A,
            "operation": "warmup",
            "body": {},
        },
        strict=True,
    )
    row = await _run_action(
        action,
        endpoint=BackendEndpoint(
            _BACKEND_B, _RESOURCE_A, "http://b.invalid", "none", None
        ),
        dispatch_factory=dispatch_factory,
        database_clock=database_clock,
        fault=fault,
    )

    assert row["status"] == "fault_injected"
    assert row["fault"] == "health-timeout"
    assert row["error_code"] == "gpu_capacity_unavailable"
    assert fault.hit_action_id == "requester-b"


def _dual_card_transport_fault_evidence() -> tuple[
    ValidationManifest,
    list[dict],
    dict,
    dict,
    FaultController,
]:
    manifest = _manifest(
        scenario="dual-card",
        resources=[
            {"resource_id": _RESOURCE_A, "gpu_uuid": _GPU_A},
            {"resource_id": _RESOURCE_B, "gpu_uuid": _GPU_B},
        ],
        actions=[
            {
                "id": "fault-a",
                "role": "peer",
                "backend_id": _BACKEND_A,
                "resource_id": _RESOURCE_A,
                "operation": "warmup",
                "body": {},
            },
            {
                "id": "peer-b",
                "role": "peer",
                "backend_id": _BACKEND_B,
                "resource_id": _RESOURCE_B,
                "operation": "warmup",
                "body": {},
            },
        ],
    )
    after = {
        "redis": {
            "resources": {
                _RESOURCE_A: _ready_redis_snapshot(
                    _RESOURCE_A,
                    [
                        {
                            "backend_id": _BACKEND_A,
                            "state": "unknown",
                            "budget_mb": 4000,
                            "evictable": False,
                        }
                    ],
                ),
                _RESOURCE_B: _ready_redis_snapshot(
                    _RESOURCE_B,
                    [
                        {
                            "backend_id": _BACKEND_B,
                            "state": "resident",
                            "budget_mb": 4000,
                        }
                    ],
                ),
            }
        }
    }
    after["redis"]["resources"][_RESOURCE_A]["snapshot"]["leases"] = [
        {
            "backend_id": _BACKEND_A,
            "generation": "1",
            "state": "uncertain",
        }
    ]
    before, after = _with_final_truth(after)
    actions = [
        {
            "id": "fault-a",
            "role": "peer",
            "backend_id": _BACKEND_A,
            "resource_id": _RESOURCE_A,
            "operation": "warmup",
            "status": "fault_injected",
            "fault": "response-lost-after-http",
            "grant_generation": "1",
            "http_started_monotonic_ms": 0.0,
            "http_finished_monotonic_ms": 1000.0,
            "http_started_database_clock": "2026-07-16T00:00:00Z",
            "http_finished_database_clock": "2026-07-16T00:00:01Z",
        },
        {
            "id": "peer-b",
            "role": "peer",
            "backend_id": _BACKEND_B,
            "resource_id": _RESOURCE_B,
            "operation": "warmup",
            "status": "passed",
            "http_status": 200,
            "http_started_monotonic_ms": 100.0,
            "http_finished_monotonic_ms": 900.0,
            "http_started_database_clock": "2026-07-16T00:00:00.100000Z",
            "http_finished_database_clock": "2026-07-16T00:00:00.900000Z",
        },
    ]
    fault = FaultController(
        kind="response-lost-after-http",
        target="fault-a",
        hits=1,
        hit_action_id="fault-a",
    )
    return manifest, actions, before, after, fault


def test_fault_scope_proves_exact_target_and_peer_card_isolation() -> None:
    manifest, actions, before, after, fault = _dual_card_transport_fault_evidence()
    checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=after,
        baseline_samples=_gpu_samples(),
        during_samples=[],
        recovery_samples=_gpu_samples(a=5000, b=6000),
        fault=fault,
    )

    assert not [check for check in checks if check["status"] != "passed"]
    assert (
        next(
            check
            for check in checks
            if check["code"] == "fault_peer_resource_isolation"
        )["status"]
        == "passed"
    )

    contaminated = deepcopy(after)
    contaminated["redis"]["resources"][_RESOURCE_B]["snapshot"]["allocations"][0][
        "state"
    ] = "unknown"
    contaminated["redis"]["resources"][_RESOURCE_B]["snapshot"]["committed_mb"] = 4000
    contaminated_checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=contaminated,
        baseline_samples=_gpu_samples(),
        during_samples=[],
        recovery_samples=_gpu_samples(),
        fault=fault,
    )
    assert (
        next(
            check
            for check in contaminated_checks
            if check["code"] == "fault_scope_isolated"
        )["status"]
        == "blocked"
    )

    unloaded_peer = deepcopy(after)
    unloaded_peer["redis"]["resources"][_RESOURCE_B]["snapshot"]["allocations"][0][
        "state"
    ] = "unloaded"
    unloaded_peer["redis"]["resources"][_RESOURCE_B]["snapshot"]["committed_mb"] = 0
    unloaded_peer["backends"][_BACKEND_B]["residency"].update(
        {
            "state": "unloaded",
            "gpu_loaded": False,
            "evictable": False,
        }
    )
    for pool in unloaded_peer["backends"][_BACKEND_B]["residency"]["pools"].values():
        pool["resident"] = False
    unloaded_checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=unloaded_peer,
        baseline_samples=_gpu_samples(),
        during_samples=[],
        recovery_samples=_gpu_samples(),
        fault=fault,
    )
    assert (
        next(
            check
            for check in unloaded_checks
            if check["code"] == "fault_peer_resource_isolation"
        )["status"]
        == "blocked"
    )

    sibling_changed = deepcopy(after)
    sibling_changed["redis"]["resources"][_RESOURCE_B]["snapshot"][
        "allocations"
    ].append(
        {
            "backend_id": _BACKEND_C,
            "state": "unloaded",
            "generation": "1",
            "budget_mb": 1000,
            "eviction_priority": 0,
            "max_concurrency": 4,
            "evictable": False,
        }
    )
    before_with_sibling = deepcopy(before)
    before_with_sibling["redis"] = {
        "resources": {
            _RESOURCE_B: deepcopy(sibling_changed["redis"]["resources"][_RESOURCE_B])
        }
    }
    before_with_sibling["redis"]["resources"][_RESOURCE_B]["snapshot"]["allocations"][
        1
    ].update({"state": "resident", "evictable": True})
    before_with_sibling["redis"]["resources"][_RESOURCE_B]["snapshot"][
        "committed_mb"
    ] = 5000
    sibling_checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before_with_sibling,
        after=sibling_changed,
        baseline_samples=_gpu_samples(),
        during_samples=[],
        recovery_samples=_gpu_samples(),
        fault=fault,
    )
    assert (
        next(
            check
            for check in sibling_checks
            if check["code"] == "fault_peer_resource_isolation"
        )["status"]
        == "blocked"
    )

    stale_peer_truth = {
        "redis": {
            "resources": {
                _RESOURCE_A: deepcopy(after["redis"]["resources"][_RESOURCE_A]),
                _RESOURCE_B: _ready_redis_snapshot(
                    _RESOURCE_B,
                    [
                        {
                            "backend_id": _BACKEND_B,
                            "state": "resident",
                            "budget_mb": 4000,
                        },
                        {
                            "backend_id": _BACKEND_C,
                            "state": "resident",
                            "budget_mb": 1000,
                        },
                    ],
                ),
            }
        }
    }
    stale_before, stale_after = _with_final_truth(stale_peer_truth)
    stale_before["redis"] = {
        "resources": {
            _RESOURCE_B: deepcopy(stale_after["redis"]["resources"][_RESOURCE_B])
        }
    }
    stale_residency = stale_after["backends"][_BACKEND_C]["residency"]
    stale_residency.update(
        {
            "state": "unloaded",
            "gpu_loaded": False,
            "evictable": False,
        }
    )
    for pool in stale_residency["pools"].values():
        pool.update({"resident": False, "device": None})
    stale_checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=stale_before,
        after=stale_after,
        baseline_samples=_gpu_samples(),
        during_samples=[],
        recovery_samples=_gpu_samples(),
        fault=fault,
    )
    assert (
        next(
            check
            for check in stale_checks
            if check["code"] == "final_backend_truth_exact"
            and check["details"]["backend_id"] == _BACKEND_C
        )["status"]
        == "blocked"
    )
    assert (
        next(
            check
            for check in stale_checks
            if check["code"] == "fault_peer_resource_isolation"
        )["status"]
        == "blocked"
    )

    regressed_before, regressed_after = _with_final_truth(stale_peer_truth)
    regressed_before["redis"] = {
        "resources": {
            _RESOURCE_B: deepcopy(regressed_after["redis"]["resources"][_RESOURCE_B])
        }
    }
    next(
        row
        for row in regressed_before["database"]["fences"]
        if row["backend_id"] == _BACKEND_C
    )["generation_high_water"] = 2
    regressed_checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=regressed_before,
        after=regressed_after,
        baseline_samples=_gpu_samples(),
        during_samples=[],
        recovery_samples=_gpu_samples(),
        fault=fault,
    )
    assert (
        next(
            check
            for check in regressed_checks
            if check["code"] == "run_fence_monotonic"
        )["status"]
        == "blocked"
    )
    assert (
        next(
            check
            for check in regressed_checks
            if check["code"] == "final_backend_truth_exact"
            and check["details"]["backend_id"] == _BACKEND_C
        )["status"]
        == "blocked"
    )
    assert (
        next(
            check
            for check in regressed_checks
            if check["code"] == "fault_peer_resource_isolation"
        )["status"]
        == "blocked"
    )


def test_fault_conservative_state_requires_target_allocation() -> None:
    manifest, actions, before, after, fault = _dual_card_transport_fault_evidence()
    after["redis"]["resources"][_RESOURCE_A]["snapshot"]["allocations"] = []
    after["redis"]["resources"][_RESOURCE_A]["snapshot"]["committed_mb"] = 0

    checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=after,
        baseline_samples=_gpu_samples(),
        during_samples=[],
        recovery_samples=_gpu_samples(),
        fault=fault,
    )

    assert (
        next(check for check in checks if check["code"] == "fault_conservative_state")[
            "status"
        ]
        == "blocked"
    )


def test_health_timeout_preserves_complete_victim_allocation() -> None:
    manifest = _manifest()
    after = {
        "redis": {
            "resources": {
                _RESOURCE_A: _ready_redis_snapshot(
                    _RESOURCE_A,
                    [
                        {
                            "backend_id": _BACKEND_A,
                            "state": "resident",
                            "budget_mb": 4000,
                        },
                        {
                            "backend_id": _BACKEND_B,
                            "state": "resident",
                            "budget_mb": 4000,
                        },
                    ],
                )
            }
        }
    }
    before, after = _with_final_truth(after)
    before["redis"] = deepcopy(after["redis"])
    actions = [
        {
            "id": "warm-a",
            "role": "requester",
            "backend_id": _BACKEND_A,
            "resource_id": _RESOURCE_A,
            "operation": "warmup",
            "status": "fault_injected",
            "fault": "health-timeout",
        },
        {
            "id": "warm-b",
            "role": "peer",
            "backend_id": _BACKEND_B,
            "resource_id": _RESOURCE_A,
            "operation": "warmup",
            "status": "passed",
            "http_status": 200,
            "http_started_monotonic_ms": 0.0,
            "http_finished_monotonic_ms": 1000.0,
        },
    ]
    fault = FaultController(
        kind="health-timeout",
        target=_BACKEND_A,
        hits=1,
        hit_action_id="warm-a",
    )
    checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=after,
        baseline_samples=_gpu_samples(),
        during_samples=[],
        recovery_samples=_gpu_samples(),
        fault=fault,
    )
    assert not [check for check in checks if check["status"] != "passed"]

    absent_requester = deepcopy(after)
    snapshot = absent_requester["redis"]["resources"][_RESOURCE_A]["snapshot"]
    snapshot["allocations"] = [
        allocation
        for allocation in snapshot["allocations"]
        if allocation["backend_id"] != _BACKEND_B
    ]
    snapshot["committed_mb"] = 4000
    requester_residency = absent_requester["backends"][_BACKEND_B]["residency"]
    requester_residency.update(
        {
            "state": "unloaded",
            "gpu_loaded": False,
            "evictable": False,
            "generation": None,
        }
    )
    requester_residency["pools"]["pool"].update({"resident": False, "device": None})
    absent_before = {
        "database": deepcopy(absent_requester["database"]),
        "redis": deepcopy(absent_requester["redis"]),
    }
    requester_fault = FaultController(
        kind="health-timeout",
        target=_BACKEND_A,
        hits=1,
        hit_action_id="warm-b",
    )
    requester_actions = [
        {
            **actions[0],
            "status": "passed",
            "fault": None,
            "http_status": 200,
            "http_started_monotonic_ms": 0.0,
            "http_finished_monotonic_ms": 1000.0,
        },
        {
            **actions[1],
            "status": "fault_injected",
            "fault": "health-timeout",
        },
    ]
    for field in (
        "grant_generation",
        "http_status",
        "http_started_database_clock",
        "http_finished_database_clock",
        "http_started_monotonic_ms",
        "http_finished_monotonic_ms",
    ):
        requester_actions[1].pop(field, None)
    requester_checks = evaluate_run(
        manifest=manifest,
        actions=requester_actions,
        before=absent_before,
        after=absent_requester,
        baseline_samples=_gpu_samples(),
        during_samples=[],
        recovery_samples=_gpu_samples(),
        fault=requester_fault,
    )
    assert not [check for check in requester_checks if check["status"] != "passed"]

    changed = deepcopy(after)
    changed["redis"]["resources"][_RESOURCE_A]["snapshot"]["allocations"][0][
        "not_evict_before_ms"
    ] = 999
    changed_checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=changed,
        baseline_samples=_gpu_samples(),
        during_samples=[],
        recovery_samples=_gpu_samples(),
        fault=fault,
    )
    assert (
        next(
            check
            for check in changed_checks
            if check["code"] == "health_timeout_preserves_victim"
        )["status"]
        == "blocked"
    )


def test_eviction_requires_exact_unload_transition_for_every_victim() -> None:
    manifest = _manifest(
        scenario="single-card-eviction",
        actions=[
            {
                "id": "victim-a",
                "role": "victim",
                "backend_id": _BACKEND_A,
                "resource_id": _RESOURCE_A,
                "operation": "predict",
                "body": {"tasks": []},
            },
            {
                "id": "victim-c",
                "role": "victim",
                "backend_id": _BACKEND_C,
                "resource_id": _RESOURCE_A,
                "operation": "predict",
                "body": {"tasks": []},
            },
            {
                "id": "requester-b",
                "role": "requester",
                "backend_id": _BACKEND_B,
                "resource_id": _RESOURCE_A,
                "operation": "warmup",
                "body": {},
            },
        ],
    )
    after = {
        "redis": {
            "resources": {
                _RESOURCE_A: _ready_redis_snapshot(
                    _RESOURCE_A,
                    [
                        {
                            "backend_id": _BACKEND_A,
                            "state": "unloaded",
                            "budget_mb": 4000,
                        },
                        {
                            "backend_id": _BACKEND_C,
                            "state": "unloaded",
                            "budget_mb": 4000,
                        },
                        {
                            "backend_id": _BACKEND_B,
                            "state": "resident",
                            "budget_mb": 4000,
                        },
                    ],
                )
            }
        }
    }
    before, after = _with_final_truth(after)
    before["redis"] = {
        "resources": {
            _RESOURCE_A: _ready_redis_snapshot(
                _RESOURCE_A,
                [
                    {
                        "backend_id": _BACKEND_A,
                        "state": "resident",
                        "budget_mb": 4000,
                    },
                    {
                        "backend_id": _BACKEND_C,
                        "state": "resident",
                        "budget_mb": 4000,
                    },
                    {
                        "backend_id": _BACKEND_B,
                        "state": "unloaded",
                        "budget_mb": 4000,
                    },
                ],
            )
        }
    }
    draining = deepcopy(before["redis"]["resources"][_RESOURCE_A])
    for allocation in draining["snapshot"]["allocations"]:
        if allocation["backend_id"] in {_BACKEND_A, _BACKEND_C}:
            allocation["state"] = "draining"
    draining["snapshot"]["transition"] = {
        "operation": "evict",
        "backend_id": _BACKEND_A,
        "requester_backend_id": _BACKEND_B,
        "eviction_branch": "unload",
    }
    actions = [
        {
            "id": action.id,
            "role": action.role,
            "backend_id": action.backend_id,
            "resource_id": action.resource_id,
            "operation": action.operation,
            "status": "passed",
        }
        for action in manifest.actions
    ]

    checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=after,
        baseline_samples=_gpu_samples(),
        during_samples=[{"redis": {"resources": {_RESOURCE_A: draining}}}],
        recovery_samples=_gpu_samples(),
        fault=None,
    )

    assert (
        next(
            check
            for check in checks
            if check["code"] == "single_card_victim_transition_observed"
        )["status"]
        == "blocked"
    )


def _cross_host_report(
    *,
    node_id: str,
    resource_id: str,
    gpu_uuid: str,
    gpu_index: int,
    started_at: str,
    finished_at: str,
) -> dict:
    backend_id = _BACKEND_A if node_id == "node-a" else _BACKEND_B
    manifest_payload = {
        "schema_version": 1,
        "cohort_id": "cohort-1",
        "node_id": node_id,
        "scenario": "cross-host",
        "resources": [{"resource_id": resource_id, "gpu_uuid": gpu_uuid}],
        "actions": [
            {
                "id": f"warm-{node_id}",
                "role": "peer",
                "backend_id": backend_id,
                "resource_id": resource_id,
                "operation": "warmup",
                "body": {},
            }
        ],
    }
    manifest = ValidationManifest.model_validate(manifest_payload, strict=True)
    after = {
        "redis": {
            "resources": {
                resource_id: _ready_redis_snapshot(
                    resource_id,
                    [
                        {
                            "backend_id": backend_id,
                            "state": "resident",
                            "budget_mb": 4000,
                        }
                    ],
                )
            }
        }
    }
    before, after = _with_final_truth(
        after,
        physical_identity_by_resource={resource_id: (gpu_uuid, gpu_index)},
    )
    actions = [
        {
            "id": f"warm-{node_id}",
            "role": "peer",
            "backend_id": backend_id,
            "resource_id": resource_id,
            "operation": "warmup",
            "status": "passed",
            "http_status": 200,
            "http_started_monotonic_ms": 0.0,
            "http_finished_monotonic_ms": 2000.0,
            "http_started_database_clock": started_at,
            "http_finished_database_clock": finished_at,
            "http_started_database_probe_rtt_ms": 0.0,
            "http_finished_database_probe_rtt_ms": 0.0,
        }
    ]
    baseline = _gpu_samples()
    during = [{"redis": after["redis"], "backends": after["backends"]}]
    recovery = _gpu_samples(a=5000, b=6000)
    checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=after,
        baseline_samples=baseline,
        during_samples=during,
        recovery_samples=recovery,
        fault=None,
    )
    checks.insert(
        0,
        {
            "code": "run_runtime_proof_refreshed",
            "status": "passed",
            "message": "run must persist fresh challenge-bound health before dispatch",
            "details": {"backend_id": backend_id},
        },
    )
    evidence_manifest = _evidence_manifest(manifest)
    return {
        "schema": EVIDENCE_SCHEMA,
        "command": "run",
        "status": "passed",
        "run_id": f"run-{node_id}",
        "cohort_id": "cohort-1",
        "node_id": node_id,
        "scenario": "cross-host",
        "resources": [{"resource_id": resource_id, "gpu_uuid": gpu_uuid}],
        "started_at": started_at,
        "finished_at": finished_at,
        "manifest_sha256": _sha256_json(manifest_payload),
        "evidence_manifest": evidence_manifest,
        "evidence_manifest_sha256": _sha256_json(evidence_manifest),
        "thresholds": _thresholds(),
        "threshold_applicability": _threshold_applicability(),
        "checks": checks,
        "actions": actions,
        "snapshots": {
            "baseline_gpu": baseline,
            "action_window": {
                "started_database_clock": started_at,
                "finished_database_clock": finished_at,
            },
            "before": before,
            "during": during,
            "after": after,
            "recovery_gpu": recovery,
        },
        "faults": [],
        "cleanup": {"performed": True},
    }


def test_verify_cross_host_requires_distinct_resources_and_overlap() -> None:
    first = _cross_host_report(
        node_id="node-a",
        resource_id="node-a/index:0",
        gpu_uuid=_GPU_A,
        gpu_index=0,
        started_at="2026-07-16T00:00:00Z",
        finished_at="2026-07-16T00:00:02Z",
    )
    second = _cross_host_report(
        node_id="node-b",
        resource_id="node-b/index:0",
        gpu_uuid=_GPU_B,
        gpu_index=0,
        started_at="2026-07-16T00:00:01Z",
        finished_at="2026-07-16T00:00:03Z",
    )

    assert verify_evidence([first, second], scenario="cross-host")["status"] == "passed"
    assert (
        verify_evidence([first], scenario="single-card-co-residency")["status"]
        == "failed"
    )

    slow_probe_first = deepcopy(first)
    slow_probe_second = deepcopy(second)
    for report in (slow_probe_first, slow_probe_second):
        report["actions"][0]["http_started_database_probe_rtt_ms"] = 800.0
        report["actions"][0]["http_finished_database_probe_rtt_ms"] = 800.0
    assert (
        verify_evidence([slow_probe_first, slow_probe_second], scenario="cross-host")[
            "status"
        ]
        == "failed"
    )

    second["node_id"] = "node-a"
    assert verify_evidence([first, second], scenario="cross-host")["status"] == "failed"

    second["node_id"] = "node-b"
    second["snapshots"]["after"]["nvidia_smi"]["gpus"][0]["index"] = 1
    assert verify_evidence([first, second], scenario="cross-host")["status"] == "failed"


def test_verify_rejects_forged_summary_thresholds_and_malformed_input() -> None:
    first = _cross_host_report(
        node_id="node-a",
        resource_id="node-a/index:0",
        gpu_uuid=_GPU_A,
        gpu_index=0,
        started_at="2026-07-16T00:00:00Z",
        finished_at="2026-07-16T00:00:02Z",
    )
    second = _cross_host_report(
        node_id="node-b",
        resource_id="node-b/index:0",
        gpu_uuid=_GPU_B,
        gpu_index=0,
        started_at="2026-07-16T00:00:01Z",
        finished_at="2026-07-16T00:00:03Z",
    )

    forged = deepcopy(first)
    forged["snapshots"]["after"]["backends"][_BACKEND_A]["residency"]["gpu_loaded"] = (
        False
    )
    assert (
        verify_evidence([forged, second], scenario="cross-host")["status"] == "failed"
    )

    drifted_threshold = deepcopy(first)
    drifted_threshold["thresholds"]["stable_memory_spread_mb"] = 65
    assert (
        verify_evidence([drifted_threshold, second], scenario="cross-host")["status"]
        == "failed"
    )

    drifted_applicability = deepcopy(first)
    drifted_applicability["threshold_applicability"]["min_memory_recovery_ratio"] = (
        "all-scenarios"
    )
    assert (
        verify_evidence([drifted_applicability, second], scenario="cross-host")[
            "status"
        ]
        == "failed"
    )

    wrong_command = deepcopy(first)
    wrong_command["command"] = "preflight"
    assert (
        verify_evidence([wrong_command, second], scenario="cross-host")["status"]
        == "failed"
    )

    missing_runtime_proof = deepcopy(first)
    missing_runtime_proof["checks"] = [
        check
        for check in missing_runtime_proof["checks"]
        if check["code"] != "run_runtime_proof_refreshed"
    ]
    assert (
        verify_evidence([missing_runtime_proof, second], scenario="cross-host")[
            "status"
        ]
        == "failed"
    )
    assert (
        verify_evidence([{}], scenario="single-card-co-residency")["status"] == "failed"
    )


def _capacity_rejection_primary_report() -> dict:
    manifest = _manifest(
        scenario="single-card-capacity-rejection",
        actions=[
            {
                "id": "reject-a",
                "role": "requester",
                "backend_id": _BACKEND_A,
                "resource_id": _RESOURCE_A,
                "operation": "warmup",
                "body": {},
                "expected_error_code": "gpu_capacity_unavailable",
            }
        ],
    )
    after = {
        "redis": {
            "resources": {
                _RESOURCE_A: _ready_redis_snapshot(
                    _RESOURCE_A,
                    [
                        {
                            "backend_id": _BACKEND_A,
                            "state": "unloaded",
                            "budget_mb": 9000,
                        },
                        {
                            "backend_id": _BACKEND_B,
                            "state": "resident",
                            "budget_mb": 14000,
                            "eviction_priority": 10,
                        },
                    ],
                )
            }
        }
    }
    before, after = _with_final_truth(after)
    before["redis"] = deepcopy(after["redis"])
    actions = [
        {
            "id": "reject-a",
            "role": "requester",
            "backend_id": _BACKEND_A,
            "resource_id": _RESOURCE_A,
            "operation": "warmup",
            "status": "passed",
            "expected_error_code": "gpu_capacity_unavailable",
            "error_code": "gpu_capacity_unavailable",
            "error_http_status": 503,
            "retry_after_seconds": None,
            "error": None,
            "started_at": "2026-07-16T00:00:00Z",
            "finished_at": "2026-07-16T00:00:00.010000Z",
            "started_monotonic_ms": 0.0,
            "finished_monotonic_ms": 10.0,
        }
    ]
    baseline = _gpu_samples()
    recovery = _gpu_samples()
    checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=after,
        baseline_samples=baseline,
        during_samples=[],
        recovery_samples=recovery,
        fault=None,
    )
    checks[:0] = [
        {
            "code": "run_runtime_proof_refreshed",
            "status": "passed",
            "message": "run must persist fresh challenge-bound health before dispatch",
            "details": {"backend_id": backend_id},
        }
        for backend_id in (_BACKEND_A, _BACKEND_B)
    ]
    evidence_manifest = _evidence_manifest(manifest)
    return {
        "schema": EVIDENCE_SCHEMA,
        "command": "run",
        "status": "passed",
        "run_id": "capacity-rejection-run",
        "cohort_id": manifest.cohort_id,
        "node_id": manifest.node_id,
        "scenario": manifest.scenario,
        "resources": [resource.model_dump() for resource in manifest.resources],
        "started_at": "2026-07-16T00:00:00Z",
        "finished_at": "2026-07-16T00:00:01Z",
        "manifest_sha256": _sha256_json(manifest.model_dump(mode="json")),
        "evidence_manifest": evidence_manifest,
        "evidence_manifest_sha256": _sha256_json(evidence_manifest),
        "thresholds": _thresholds(),
        "threshold_applicability": _threshold_applicability(),
        "checks": checks,
        "actions": actions,
        "snapshots": {
            "baseline_gpu": baseline,
            "during": [],
            "before": before,
            "after": after,
            "recovery_gpu": recovery,
        },
        "faults": [],
        "cleanup": {"performed": True},
    }


def test_verify_capacity_rejection_requires_no_backend_http() -> None:
    report = _capacity_rejection_primary_report()
    assert (
        verify_evidence([report], scenario="single-card-capacity-rejection")["status"]
        == "passed"
    )

    forged = deepcopy(report)
    forged["actions"][0]["http_started_monotonic_ms"] = 0.0
    assert (
        verify_evidence([forged], scenario="single-card-capacity-rejection")["status"]
        == "failed"
    )


def _dual_card_primary_report() -> dict:
    manifest = _manifest(
        scenario="dual-card",
        resources=[
            {"resource_id": _RESOURCE_A, "gpu_uuid": _GPU_A},
            {"resource_id": _RESOURCE_B, "gpu_uuid": _GPU_B},
        ],
        actions=[
            {
                "id": "warm-a",
                "role": "peer",
                "backend_id": _BACKEND_A,
                "resource_id": _RESOURCE_A,
                "operation": "warmup",
                "body": {},
            },
            {
                "id": "warm-b",
                "role": "peer",
                "backend_id": _BACKEND_B,
                "resource_id": _RESOURCE_B,
                "operation": "warmup",
                "body": {},
            },
        ],
    )
    after = {
        "redis": {
            "resources": {
                _RESOURCE_A: _ready_redis_snapshot(
                    _RESOURCE_A,
                    [
                        {
                            "backend_id": _BACKEND_A,
                            "state": "resident",
                            "budget_mb": 4000,
                        }
                    ],
                ),
                _RESOURCE_B: _ready_redis_snapshot(
                    _RESOURCE_B,
                    [
                        {
                            "backend_id": _BACKEND_B,
                            "state": "resident",
                            "budget_mb": 4000,
                        }
                    ],
                ),
            }
        }
    }
    before, after = _with_final_truth(after)
    actions = [
        {
            "id": "warm-a",
            "role": "peer",
            "backend_id": _BACKEND_A,
            "resource_id": _RESOURCE_A,
            "operation": "warmup",
            "status": "passed",
            "http_status": 200,
            "http_started_monotonic_ms": 0.0,
            "http_finished_monotonic_ms": 1000.0,
            "http_started_database_clock": "2026-07-16T00:00:00Z",
            "http_finished_database_clock": "2026-07-16T00:00:01Z",
            "http_started_database_probe_rtt_ms": 0.0,
            "http_finished_database_probe_rtt_ms": 0.0,
        },
        {
            "id": "warm-b",
            "role": "peer",
            "backend_id": _BACKEND_B,
            "resource_id": _RESOURCE_B,
            "operation": "warmup",
            "status": "passed",
            "http_status": 200,
            "http_started_monotonic_ms": 400.0,
            "http_finished_monotonic_ms": 1100.0,
            "http_started_database_clock": "2026-07-16T00:00:00.400000Z",
            "http_finished_database_clock": "2026-07-16T00:00:01.100000Z",
            "http_started_database_probe_rtt_ms": 0.0,
            "http_finished_database_probe_rtt_ms": 0.0,
        },
    ]
    baseline = _gpu_samples()
    during = [{"redis": after["redis"], "backends": after["backends"]}]
    recovery = _gpu_samples(a=5000, b=6000)
    checks = evaluate_run(
        manifest=manifest,
        actions=actions,
        before=before,
        after=after,
        baseline_samples=baseline,
        during_samples=during,
        recovery_samples=recovery,
        fault=None,
    )
    checks[:0] = [
        {
            "code": "run_runtime_proof_refreshed",
            "status": "passed",
            "message": "run must persist fresh challenge-bound health before dispatch",
            "details": {"backend_id": backend_id},
        }
        for backend_id in (_BACKEND_A, _BACKEND_B)
    ]
    evidence_manifest = _evidence_manifest(manifest)
    return {
        "schema": EVIDENCE_SCHEMA,
        "command": "run",
        "status": "passed",
        "run_id": "dual-card-run",
        "cohort_id": manifest.cohort_id,
        "node_id": manifest.node_id,
        "scenario": manifest.scenario,
        "resources": [resource.model_dump() for resource in manifest.resources],
        "started_at": "2026-07-16T00:00:00Z",
        "finished_at": "2026-07-16T00:00:02Z",
        "manifest_sha256": _sha256_json(manifest.model_dump(mode="json")),
        "evidence_manifest": evidence_manifest,
        "evidence_manifest_sha256": _sha256_json(evidence_manifest),
        "thresholds": _thresholds(),
        "threshold_applicability": _threshold_applicability(),
        "checks": checks,
        "actions": actions,
        "snapshots": {
            "baseline_gpu": baseline,
            "during": during,
            "before": before,
            "after": after,
            "recovery_gpu": recovery,
        },
        "faults": [],
        "cleanup": {"performed": True},
    }


def test_verify_dual_card_recomputes_http_overlap() -> None:
    report = _dual_card_primary_report()
    assert verify_evidence([report], scenario="dual-card")["status"] == "passed"

    fast_local = deepcopy(report)
    fast_local["actions"][0]["http_started_database_probe_rtt_ms"] = 800.0
    fast_local["actions"][0]["http_finished_database_probe_rtt_ms"] = 800.0
    assert verify_evidence([fast_local], scenario="dual-card")["status"] == "passed"

    forged = deepcopy(report)
    forged["actions"][1]["http_started_monotonic_ms"] = 1001.0
    forged["actions"][1]["http_finished_monotonic_ms"] = 1500.0
    assert verify_evidence([forged], scenario="dual-card")["status"] == "failed"


def test_report_write_is_atomic_and_cleans_temporary_file(tmp_path, capsys) -> None:
    output = tmp_path / "evidence.json"
    report = {"schema": EVIDENCE_SCHEMA, "status": "passed"}

    _write_report(report, output)

    assert json.loads(output.read_text()) == report
    assert json.loads(capsys.readouterr().out) == report
    assert list(tmp_path.glob(".*.tmp")) == []
