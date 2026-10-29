from __future__ import annotations

import copy
import hashlib

import pytest

from aap_backend_runtime.lifecycle_evidence import (
    artifact_evidence,
    build_managed_lifecycle_evidence,
)
from aap_lifecycle_contract import (
    REQUIRED_CONTRACT_CHECKS,
    memory_cycle_evidence,
    validate_managed_lifecycle_evidence,
)


def _residency() -> dict:
    return {
        "state": "unloaded",
        "gpu_loaded": False,
        "evictable": False,
        "active_requests": 0,
        "builders": 0,
        "borrowers": 0,
        "pools": {"models": {"resident": False}},
    }


def _cycle(index: int) -> dict:
    return memory_cycle_evidence(
        cycle=index,
        generation=str(index * 2),
        context_samples_mb=[100, 100, 100, 100, 100],
        loaded_samples_mb=[1100, 1100, 1100],
        unloaded_samples_mb=[120, 121, 119, 120, 120],
    )


def _evidence(**overrides) -> dict:
    values = {
        "backend_name": "yolo-backend",
        "deployment": {
            "git_commit": "a" * 40,
            "image_id": "sha256:" + "b" * 64,
            "runtime_versions": {"python": "3.12.11", "cuda": "13.0"},
            "pool_topology": {"models": {"cap": 2}},
        },
        "artifacts": [
            {
                "kind": "weight",
                "name": "model.pt",
                "size_bytes": 7,
                "sha256": hashlib.sha256(b"weights").hexdigest(),
                "approval_ref": "models:2026-07-30",
            },
            {
                "kind": "fixture",
                "name": "fixture.png",
                "size_bytes": 7,
                "sha256": hashlib.sha256(b"fixture").hexdigest(),
                "approval_ref": "fixtures:2026-07-30",
            },
        ],
        "gpu": {
            "uuid": "GPU-1234",
            "total_memory_mb": 24576,
            "driver_version": "580.65",
            "runtime_version": "13.0",
            "visible_device_count": 1,
        },
        "cycles": [_cycle(1), _cycle(2)],
        "contract_checks": {name: True for name in REQUIRED_CONTRACT_CHECKS},
        "final_residency": _residency(),
        "runtime_ephemera_clean": True,
        "generated_at": "2026-07-30T12:00:00Z",
    }
    values.update(overrides)
    return build_managed_lifecycle_evidence(**values)


def test_artifact_evidence_hashes_without_persisting_path(tmp_path) -> None:
    artifact = tmp_path / "model.pt"
    artifact.write_bytes(b"weights")

    evidence = artifact_evidence(
        artifact,
        kind="weight",
        approval_ref="models:approved",
    )

    assert evidence == {
        "kind": "weight",
        "name": "model.pt",
        "size_bytes": 7,
        "sha256": hashlib.sha256(b"weights").hexdigest(),
        "approval_ref": "models:approved",
    }
    assert str(tmp_path) not in str(evidence)


def test_memory_cycle_calculates_stability_and_recovery_ratio() -> None:
    cycle = _cycle(1)

    assert cycle["unloaded_spread_mb"] == 2
    assert cycle["working_set_recovery_ratio"] == 0.98


def test_build_evidence_derives_passed_from_full_qualification() -> None:
    evidence = _evidence()

    assert evidence["schema_version"] == "1"
    assert evidence["passed"] is True
    assert evidence["blockers"] == []


def test_build_evidence_emits_structured_blockers_for_failed_gates() -> None:
    failed_checks = {name: True for name in REQUIRED_CONTRACT_CHECKS}
    failed_checks["token_replay_rejected"] = False
    weak_cycle = memory_cycle_evidence(
        cycle=1,
        generation="2",
        context_samples_mb=[100, 100, 100, 100, 100],
        loaded_samples_mb=[1100, 1100, 1100],
        unloaded_samples_mb=[350, 350, 350, 350, 350],
    )

    evidence = _evidence(
        cycles=[weak_cycle, _cycle(2)],
        contract_checks=failed_checks,
        runtime_ephemera_clean=False,
    )

    assert evidence["passed"] is False
    assert {item["code"] for item in evidence["blockers"]} == {
        "contract_checks_failed",
        "memory_recovery_insufficient",
        "runtime_ephemera_dirty",
    }


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: payload.update({"extra": True}),
        lambda payload: payload["deployment"].pop("image_id"),
        lambda payload: payload["gpu"].update({"visible_device_count": "1"}),
        lambda payload: payload["artifacts"][0].update({"name": "/tmp/model.pt"}),
        lambda payload: payload["deployment"]["pool_topology"].update(
            {"private_key": "secret"}
        ),
        lambda payload: payload["contract_checks"].update(
            {"token_replay_rejected": False}
        ),
        lambda payload: payload["cycles"][0].update(
            {"working_set_recovery_ratio": 1.0}
        ),
    ),
)
def test_validate_evidence_rejects_shape_type_path_and_secret_drift(mutate) -> None:
    evidence = copy.deepcopy(_evidence())
    mutate(evidence)

    with pytest.raises(ValueError):
        validate_managed_lifecycle_evidence(evidence)
