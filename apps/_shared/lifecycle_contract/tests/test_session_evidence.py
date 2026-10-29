from copy import deepcopy

import pytest

from aap_lifecycle_contract import (
    REQUIRED_CONTRACT_CHECKS,
    memory_cycle_evidence,
    validate_managed_lifecycle_evidence,
)


def evidence():
    pools = {"pipeline": {"cap": 1}, "detector": {"cap": 1}}
    observations = {
        "pipeline": [
            ["CUDAExecutionProvider", "CPUExecutionProvider"],
            ["CUDAExecutionProvider"],
        ],
        "detector": [["CUDAExecutionProvider"]],
    }
    cycles = []
    for number in (1, 2):
        cycle = memory_cycle_evidence(
            cycle=number,
            generation=str(number),
            context_samples_mb=[100] * 5,
            loaded_samples_mb=[1000] * 5,
            unloaded_samples_mb=[100] * 5,
        )
        cycle["session_observations"] = deepcopy(observations)
        cycles.append(cycle)
    return {
        "schema_version": "1",
        "backend_name": "onnx",
        "generated_at": "2026-01-01T00:00:00Z",
        "passed": True,
        "blockers": [],
        "deployment": {
            "git_commit": "abc",
            "image_id": "sha256:" + "a" * 64,
            "runtime_versions": {"onnxruntime": "1.22"},
            "pool_topology": {
                "pools": pools,
                "session_counts": {"pipeline": 2, "detector": 1},
            },
        },
        "artifacts": [
            {
                "kind": kind,
                "name": kind,
                "size_bytes": 1,
                "sha256": "a" * 64,
                "approval_ref": "approved",
            }
            for kind in ("weight", "fixture")
        ],
        "gpu": {
            "uuid": "GPU-123",
            "total_memory_mb": 1024,
            "driver_version": "1",
            "runtime_version": "1",
            "visible_device_count": 1,
        },
        "cycles": cycles,
        "contract_checks": {key: True for key in REQUIRED_CONTRACT_CHECKS},
        "final_residency": {
            "state": "unloaded",
            "gpu_loaded": False,
            "evictable": False,
            "active_requests": 0,
            "builders": 0,
            "borrowers": 0,
            "pools": {name: {"resident": False} for name in pools},
        },
        "runtime_ephemera_clean": True,
    }


def test_ort_qualification_accepts_measured_sessions_for_every_cycle():
    value = evidence()
    assert validate_managed_lifecycle_evidence(value) == value


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value["cycles"][0].pop("session_observations"),
        lambda value: value["deployment"]["pool_topology"].pop("session_counts"),
        lambda value: value["deployment"]["pool_topology"]["session_counts"].update(
            pipeline=True
        ),
        lambda value: value["cycles"][0]["session_observations"].pop("detector"),
        lambda value: value["cycles"][0]["session_observations"]["pipeline"].pop(),
        lambda value: value["cycles"][1]["session_observations"]["pipeline"][0].clear(),
        lambda value: value["cycles"][1]["session_observations"]["pipeline"][
            0
        ].__setitem__(0, "CPUExecutionProvider"),
        lambda value: value["cycles"][1].update(session_observations=None),
    ],
)
def test_ort_qualification_rejects_missing_or_unqualified_session_evidence(change):
    value = evidence()
    change(value)
    with pytest.raises(ValueError):
        validate_managed_lifecycle_evidence(value)


def test_ort_rejects_same_missing_session_in_every_cycle():
    value = evidence()
    for cycle in value["cycles"]:
        cycle["session_observations"]["pipeline"].pop()
    with pytest.raises(ValueError):
        validate_managed_lifecycle_evidence(value)


def test_non_ort_report_remains_unchanged_without_session_observations():
    value = evidence()
    value["deployment"]["runtime_versions"] = {"torch": "qualified"}
    del value["deployment"]["pool_topology"]["session_counts"]
    for cycle in value["cycles"]:
        del cycle["session_observations"]
    assert validate_managed_lifecycle_evidence(value) == value
