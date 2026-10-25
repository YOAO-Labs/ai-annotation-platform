"""Rooted initialization changes address capability, not frozen model policy."""

from copy import deepcopy
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
from training_contract.catalog import digest
from training_contract.initialization import (
    POLICY_DIGEST,
    PIN_KEYS,
    artifact_root,
    compatible_runtime,
    validate_pin,
)

from app.db.models.training_run import TrainingRun
from app.services import training_runs as runs


def pin_for(root=""):
    ids = {
        key: str(uuid.uuid4())
        for key in (
            "project_id",
            "model_version_id",
            "run_id",
            "attempt_id",
            "dataset_id",
        )
    }
    classes = ["car"]
    pin = {
        "kind": "trained_model",
        "policy_digest": POLICY_DIGEST,
        **ids,
        "model_name": "Retained source",
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "task": "image-detection",
        "model_family": "yolo11",
        "base_model": "yolo11n",
        "artifact": {
            "key": root
            + f"projects/{ids['project_id']}/runs/{ids['run_id']}/attempts/{ids['attempt_id']}/best.pt",
            "role": "weights",
            "sha256": "a" * 64,
            "size_bytes": 8,
        },
        "classes": classes,
        "classes_digest": digest(classes),
        "bindings": classes,
        "binding_digest": digest({"classes": classes, "bindings": classes}),
        "config_digest": "b" * 64,
        "dataset_digest": "c" * 64,
        "validation_digest": "d" * 64,
        "runtime": {
            "python": "3.12.3",
            "torch": "2.14.1+cu132",
            "torchvision": "0.29.1+cu132",
            "cuda": "13.2",
            "image_digest": "sha256:" + "e" * 64,
        },
        "evaluation": {
            "report_sha256": "f" * 64,
            "report_digest": "1" * 64,
            "validation_digest": "d" * 64,
        },
    }
    pin["source_digest"] = digest(pin)
    return pin


def config_for(pin):
    return {
        "initialization": pin,
        "adapter_revision": 2,
        "framework": pin["framework"],
        "framework_version": pin["framework_version"],
        "base_model": {"id": pin["base_model"]},
        "classes": pin["classes"],
    }


@pytest.mark.parametrize(
    "root",
    [
        "",
        "_aap_managed/training/",
        "中文/[a](b)/_aap_managed/training/",
        "_aap_managed/training/inner/_aap_managed/training/",
    ],
)
def test_valid_addresses_preserve_exact_fields_policy_and_digest(root):
    pin = pin_for(root)
    frozen = deepcopy(pin)
    assert validate_pin(pin) is pin
    assert set(pin) == PIN_KEYS
    assert artifact_root(pin) == root
    assert pin == frozen
    assert pin["policy_digest"] == POLICY_DIGEST


@pytest.mark.parametrize(
    "root",
    [
        "outside/",
        "__aap_reference__/_aap_managed/training/",
        "_aap_managed/datasets/",
        "/_aap_managed/training/",
        "bad//_aap_managed/training/",
        "../_aap_managed/training/",
        "bad\\root/_aap_managed/training/",
        "bad\x00/_aap_managed/training/",
        "x" * 247 + "/_aap_managed/training/",
        "界" * 230 + "/_aap_managed/training/",
    ],
)
def test_invalid_managed_root_is_rejected_with_a_valid_frozen_digest(root):
    pin = pin_for(root)
    with pytest.raises(ValueError):
        validate_pin(pin)


@pytest.mark.parametrize("rooted", [False, True])
@pytest.mark.parametrize("capability", [None, 1, True, "2", 2])
def test_only_rooted_source_requires_explicit_integer_address_capability(
    rooted, capability
):
    pin = pin_for("中文/_aap_managed/training/" if rooted else "")
    runtime = {**pin["runtime"], "initialization_policy_digest": POLICY_DIGEST}
    if capability is not None:
        runtime["training_saved_prefix_schema"] = capability
    expected = not rooted or type(capability) is int and capability >= 2
    assert compatible_runtime(config_for(pin), runtime) is expected
    assert runs.input_runtime_compatible(config_for(pin), runtime) is expected
    assert runs.input_runtime_compatible(
        {**config_for(pin), "initialization": None}, runtime
    )


@pytest.mark.parametrize("capability", [None, 2])
async def test_exact_claim_recovery_retains_lease_until_root_capability_matches(
    monkeypatch, capability
):
    pin = pin_for("中文/_aap_managed/training/")
    body = SimpleNamespace(
        resource_id="gpu",
        executor_id="executor",
        boot_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        runtime={"gpu_uuid": "gpu"},
        recover_only=True,
    )
    if capability:
        body.runtime["training_saved_prefix_schema"] = capability
    attempt = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=uuid.UUID(pin["run_id"]),
        executor_id=body.executor_id,
        boot_id=body.boot_id,
        claim_id=body.claim_id,
        lease_digest=hashlib.sha256(b"token").hexdigest(),
    )
    run = SimpleNamespace(config=config_for(pin))
    lease = SimpleNamespace(attempt_id=attempt.id)
    db = SimpleNamespace(
        get=AsyncMock(
            side_effect=lambda model, identifier: (
                run if model is TrainingRun else attempt
            )
        )
    )
    monkeypatch.setattr(runs, "catalog", lambda: {"enabled": True})
    monkeypatch.setattr(runs, "resource_id", lambda: "gpu")
    monkeypatch.setattr(runs.settings, "training_gpu_uuid", "gpu")
    monkeypatch.setattr(runs, "lock_resource", AsyncMock(return_value=lease))
    monkeypatch.setattr(runs, "observe_lost", AsyncMock())
    monkeypatch.setattr(runs, "claim_token", lambda *args: "token")
    assignment = AsyncMock(return_value={"recovered": True})
    monkeypatch.setattr(runs, "assignment", assignment)
    if capability:
        assert await runs.claim(db, body) == {"recovered": True}
        assignment.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as error:
            await runs.claim(db, body)
        assert error.value.status_code == 409
        assignment.assert_not_awaited()
    assert lease.attempt_id == attempt.id


async def test_new_claim_skips_rooted_source_for_old_executor_without_owning_attempt(
    monkeypatch,
):
    pin = pin_for("中文/_aap_managed/training/")
    body = SimpleNamespace(
        resource_id="gpu",
        executor_id="executor",
        boot_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        runtime={"gpu_uuid": "gpu"},
        recover_only=False,
    )
    run = SimpleNamespace(config={**config_for(pin), "resource_id": "gpu"})
    lease = SimpleNamespace(attempt_id=None)
    db = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [run])),
        get=AsyncMock(),
    )
    monkeypatch.setattr(runs, "catalog", lambda: {"enabled": True})
    monkeypatch.setattr(runs, "resource_id", lambda: "gpu")
    monkeypatch.setattr(runs.settings, "training_gpu_uuid", "gpu")
    monkeypatch.setattr(runs, "lock_resource", AsyncMock(return_value=lease))
    monkeypatch.setattr(runs, "observe_lost", AsyncMock())
    assert await runs.claim(db, body) is None
    assert lease.attempt_id is None
    db.get.assert_not_awaited()


async def test_resume_eligibility_keeps_rooted_address_capability_gate(monkeypatch):
    from app.services import training_observability as observations

    pin = pin_for("中文/_aap_managed/training/")
    attempt = SimpleNamespace(
        id=uuid.uuid4(),
        status="failed",
        artifacts={"last.pt": {"role": "checkpoint", "epoch": 2}},
    )
    run = SimpleNamespace(
        current_attempt_id=attempt.id,
        status="failed",
        study_id=None,
        config={**config_for(pin), "resource_id": "gpu", "parameters": {"epochs": 10}},
    )
    lease = SimpleNamespace(
        attempt_id=None,
        last_seen_at=runs.now(),
        runtime={**pin["runtime"], "initialization_policy_digest": POLICY_DIGEST},
    )
    monkeypatch.setattr(runs, "authority", AsyncMock())
    result = await observations.recovery(
        SimpleNamespace(get=AsyncMock(return_value=attempt)),
        run,
        check_storage=False,
        lease=lease,
    )
    assert not result["eligible"]
    assert "runtime_incompatible" in result["reasons"]


def test_executor_advertises_saved_prefix_protocol_without_model_or_gpu_execution(
    monkeypatch, tmp_path
):
    from pathlib import Path
    import sys

    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[2] / "training-executor")
    )
    # Process supervision is unused in this protocol-only runtime handshake.
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            __version__="controlled",
            version=SimpleNamespace(cuda="controlled"),
            cuda=SimpleNamespace(
                is_available=lambda: True,
                device_count=lambda: 1,
                get_device_name=lambda index: "controlled GPU",
            ),
        ),
    )
    from training_executor import worker

    monkeypatch.setenv("TRAINING_GPU_UUID", "GPU-controlled")
    monkeypatch.setenv(
        "TRAINING_IMAGE_REFERENCE", "registry.invalid/executor@sha256:" + "a" * 64
    )
    monkeypatch.setenv("TRAINING_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(
        worker.subprocess, "check_output", lambda *args, **kwargs: "GPU-controlled\n"
    )
    monkeypatch.setattr(
        worker.importlib.metadata,
        "version",
        lambda name: "8.4.177" if name == "ultralytics" else "controlled",
    )
    runtime = worker.runtime_identity()
    assert runtime["training_saved_prefix_schema"] == 2
    assert runtime["executor_protocol"] == 2
    assert runtime["initialization_policy_digest"] == POLICY_DIGEST
