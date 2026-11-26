"""Platform-side yoao-trainer/v1 contract model and framework-neutrality tests."""

from __future__ import annotations

import ast
import inspect
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from training_contract import trainer_plugin as trainer_plugin_mod
from training_contract.trainer_plugin import (
    PREVIEW_MAX_POOL_BYTES,
    PREVIEW_MAX_SOURCE_BYTES,
    TRAINER_PROTOCOL_VERSION,
    PreviewOutputV1,
    PreviewReportV1,
    PreviewSourceV1,
    PreviewSpecV1,
    ServingCompatibilityV1,
    TrainArtifactV1,
    TrainEventV1,
    TrainReceiptV1,
    TrainSpecV1,
    TrainerArchitectureV1,
    TrainerCapabilitiesV1,
    ValidateConfigResultV1,
    ValidationIssueV1,
    atomic_write_json,
    canonical_json_bytes,
    canonical_json_digest,
    write_event_jsonl,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 64
DIGEST_E = "e" * 64


def _collect_schema_keys(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            found.update(_collect_schema_keys(value))
    elif isinstance(node, list):
        for item in node:
            found.update(_collect_schema_keys(item))
    return found


def test_trainer_plugin_module_is_framework_agnostic() -> None:
    source_file = inspect.getsourcefile(trainer_plugin_mod)
    assert source_file is not None
    source = Path(source_file).read_text(encoding="utf-8")
    lowered = source.lower()
    for forbidden in ("ultralytics", "libreyolo", "torch"):
        assert forbidden not in lowered
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    assert imported_roots <= {
        "__future__",
        "hashlib",
        "json",
        "math",
        "os",
        "pathlib",
        "pydantic",
        "re",
        "tempfile",
        "typing",
    }


def test_canonical_digest_matches_stable_sha256_encoding() -> None:
    import hashlib

    payload = {
        "z": [0.25, True, None],
        "a": {"nested": "中文", "count": 3},
        "format": None,
    }
    expected = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    assert canonical_json_digest(payload) == expected
    assert b"-0.0" not in canonical_json_bytes({"zero": -0.0})
    with pytest.raises(ValueError):
        canonical_json_digest({"loss": float("nan")})


def test_trainer_models_emit_valid_json_schema_with_required_protocol_version() -> None:
    for model_cls in (
        TrainerCapabilitiesV1,
        ValidateConfigResultV1,
        TrainSpecV1,
        TrainReceiptV1,
        PreviewSpecV1,
        PreviewReportV1,
    ):
        schema = model_cls.model_json_schema()
        assert "protocol_version" in schema.get("required", [])
        keys = _collect_schema_keys(schema)
        assert "ge" not in keys
        assert "le" not in keys

    event_schema = TrainEventV1.model_json_schema()
    event_keys = _collect_schema_keys(event_schema)
    assert "ge" not in event_keys
    assert "le" not in event_keys
    progress_any_of = event_schema["properties"]["progress"]["anyOf"]
    number_branch = next(
        item for item in progress_any_of if item.get("type") == "number"
    )
    assert number_branch["minimum"] == 0.0
    assert number_branch["maximum"] == 1.0


def test_trainer_plugin_round_trip_and_atomic_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compat = ServingCompatibilityV1(
        framework="custom-framework",
        format=None,
        training_runtime={"python": "3.10", "framework": "2.0.0"},
        compatible_serving_runtimes=[{"device": "cpu"}],
    )
    caps = TrainerCapabilitiesV1(
        protocol_version=TRAINER_PROTOCOL_VERSION,
        framework_id="custom-framework",
        framework_version="2.0.0",
        runtime_versions={"python": "3.10"},
        supported_tasks=["detect", "segment"],
        supported_dataset_contracts=["bbox-v1"],
        architectures=[
            TrainerArchitectureV1(
                id="arch-s",
                family="arch",
                tasks=["detect"],
                base_asset_filename="arch-s.bin",
                base_asset_sha256=DIGEST_A,
            )
        ],
        policy_digests={"budget": DIGEST_B},
        serving_compatibility=[compat],
    )
    assert caps.protocol_version == TRAINER_PROTOCOL_VERSION

    cfg_ok = ValidateConfigResultV1(
        protocol_version=TRAINER_PROTOCOL_VERSION,
        valid=True,
        schema_id="custom-2.0.0-detect-1",
        schema_digest=DIGEST_A,
        resolved_config={"epochs": 5},
        effective_values={"epochs": 5},
        origins={"epochs": "user"},
    )
    assert cfg_ok.valid is True
    cfg_bad = ValidateConfigResultV1(
        protocol_version=TRAINER_PROTOCOL_VERSION,
        valid=False,
        issues=[
            ValidationIssueV1(
                code="invalid_value", field="lr0", message="out of bounds"
            )
        ],
    )
    assert cfg_bad.issues[0].code == "invalid_value"

    spec = TrainSpecV1(
        protocol_version=TRAINER_PROTOCOL_VERSION,
        run_id="run-100",
        attempt_id="attempt-100",
        framework_id="custom-framework",
        task="detect",
        architecture_id="arch-s",
        dataset_contract="bbox-v1",
        dataset_dir="/work/input/dataset",
        dataset_snapshot_digest=DIGEST_A,
        config_digest=DIGEST_B,
        classes_digest=DIGEST_C,
        schema_id="custom-2.0.0-detect-1",
        schema_digest=DIGEST_D,
        parameters={"epochs": 5},
    )
    spec_file = tmp_path / "spec.json"
    atomic_write_json(spec_file, spec)
    assert stat.S_IMODE(spec_file.stat().st_mode) == 0o644
    raw_spec = json.loads(spec_file.read_text(encoding="utf-8"))
    assert TrainSpecV1.model_validate(raw_spec) == spec

    events_file = tmp_path / "events.jsonl"
    write_event_jsonl(
        events_file,
        TrainEventV1(
            seq=1,
            kind="progress",
            phase="train",
            epoch=1,
            total_epochs=5,
            progress=0,
            metrics={"loss": 0.42},
        ),
    )
    write_event_jsonl(
        events_file,
        TrainEventV1(seq=2, kind="progress", progress=-0.0, metrics={"loss": -0.0}),
    )
    lines = events_file.read_text(encoding="utf-8").splitlines()
    assert "-0.0" not in lines[1]
    assert TrainEventV1.model_validate(json.loads(lines[0])).progress == 0.0
    assert TrainEventV1.model_validate(json.loads(lines[1])).progress == 0.0

    receipt = TrainReceiptV1(
        protocol_version=TRAINER_PROTOCOL_VERSION,
        status="succeeded",
        artifacts=[
            TrainArtifactV1(
                role="weights",
                filename="best.bin",
                sha256=DIGEST_A,
                size_bytes=512,
            )
        ],
        serving_compatibility=compat,
    )
    receipt_file = tmp_path / "result.json"
    atomic_write_json(receipt_file, receipt, mode=0o600)
    assert stat.S_IMODE(receipt_file.stat().st_mode) == 0o600
    raw_receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    assert raw_receipt["serving_compatibility"]["format"] is None
    assert TrainReceiptV1.model_validate(raw_receipt) == receipt

    preview_spec = PreviewSpecV1(
        protocol_version=TRAINER_PROTOCOL_VERSION,
        preview_id="prev-1",
        framework_id="custom-framework",
        task="detect",
        architecture_id="arch-s",
        input_digest=DIGEST_E,
        config_digest=DIGEST_A,
        dataset_snapshot_digest=DIGEST_B,
        policy_digest=DIGEST_C,
        seed=7,
        epoch=1,
        count=1,
        sources=[
            PreviewSourceV1(
                task_id="task-1",
                filename="images/001.jpg",
                sha256=DIGEST_D,
                width=320,
                height=240,
                size_bytes=4096,
                box_count=1,
                label_path="labels/001.txt",
                label_sha256=DIGEST_A,
            )
        ],
    )
    report = PreviewReportV1(
        protocol_version=TRAINER_PROTOCOL_VERSION,
        status="succeeded",
        input_digest=preview_spec.input_digest,
        policy_digest=DIGEST_C,
        cuda_initialized=False,
        outputs=[
            PreviewOutputV1(
                filename="preview-0.png",
                sha256=DIGEST_A,
                size_bytes=2048,
                width=320,
                height=320,
                box_count=1,
                metadata={"clipped": 0},
            )
        ],
        runtime={"cpu_only": True},
        details={"mosaic_closed": False},
    )
    assert report.outputs[0].filename == "preview-0.png"

    def _boom(_src: str, _dst: Path) -> None:
        raise RuntimeError("replace failed")

    monkeypatch.setattr(os, "replace", _boom)
    fail_target = tmp_path / "cleanup" / "atomic.json"
    with pytest.raises(RuntimeError, match="replace failed"):
        atomic_write_json(fail_target, {"ok": True})
    assert not fail_target.exists()
    assert not list(fail_target.parent.glob(".tmp-*"))


@pytest.mark.parametrize(
    "bad_format",
    ["", "   ", " onnx", "onnx\n"],
)
def test_serving_compatibility_rejects_invalid_format(bad_format: str) -> None:
    with pytest.raises(ValidationError):
        ServingCompatibilityV1(framework="custom", format=bad_format)


def test_trainer_plugin_negative_guards(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ServingCompatibilityV1.model_validate({"framework": "custom"})
    with pytest.raises(ValidationError):
        ServingCompatibilityV1.model_validate(
            {"framework": "custom", "format": None, "extra": 1}
        )
    with pytest.raises(ValidationError):
        TrainReceiptV1.model_validate({"status": "cancelled"})
    with pytest.raises(ValidationError):
        TrainerArchitectureV1(
            id="arch-s",
            family="arch",
            tasks=["detect"],
            base_asset_sha256=DIGEST_A,
        )
    with pytest.raises(ValidationError):
        TrainerCapabilitiesV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            framework_id="custom",
            framework_version="1.0.0",
            supported_tasks=["detect"],
            supported_dataset_contracts=["bbox-v1"],
            architectures=[
                TrainerArchitectureV1(id="arch-s", family="arch", tasks=["segment"])
            ],
        )
    with pytest.raises(ValidationError):
        TrainerCapabilitiesV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            framework_id="custom",
            framework_version="1.0.0",
            runtime_versions={" bad": "1.0"},
            supported_tasks=["detect"],
            supported_dataset_contracts=["bbox-v1"],
            architectures=[
                TrainerArchitectureV1(id="arch-s", family="arch", tasks=["detect"])
            ],
        )
    with pytest.raises(ValidationError):
        ValidateConfigResultV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            valid=True,
            schema_id="only-id",
        )
    with pytest.raises(ValidationError):
        ValidateConfigResultV1(protocol_version=TRAINER_PROTOCOL_VERSION, valid=False)
    with pytest.raises(ValidationError):
        TrainSpecV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            run_id="run-1",
            attempt_id="attempt-1",
            framework_id="custom",
            task="detect",
            architecture_id="arch-s",
            dataset_contract="bbox-v1",
            dataset_dir="/work/input/dataset",
            dataset_snapshot_digest=DIGEST_A,
            config_digest=DIGEST_B,
            classes_digest=DIGEST_C,
            resume_checkpoint_path="/work/input/resume.bin",
            init_weights_path="/work/input/init.bin",
        )
    with pytest.raises(ValidationError):
        TrainEventV1(seq=1, kind="epoch", epoch=4, total_epochs=2)
    with pytest.raises(ValidationError):
        TrainEventV1.model_validate({"seq": 1, "kind": "progress", "progress": False})
    with pytest.raises(ValueError):
        write_event_jsonl(
            tmp_path / "never-events" / "events.jsonl",
            TrainEventV1(seq=1, kind="epoch", metrics={"loss": float("inf")}),
        )
    assert not (tmp_path / "never-events").exists()
    with pytest.raises(ValidationError):
        TrainArtifactV1(
            role="weights",
            filename="nested/best.bin",
            sha256=DIGEST_A,
            size_bytes=1,
        )
    with pytest.raises(ValidationError):
        TrainReceiptV1(protocol_version=TRAINER_PROTOCOL_VERSION, status="failed")
    with pytest.raises(ValidationError):
        TrainReceiptV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            status="succeeded",
            failure_code="boom",
        )
    with pytest.raises(ValidationError):
        PreviewSourceV1(
            task_id="t-1",
            filename="/abs/image.jpg",
            sha256=DIGEST_A,
            width=100,
            height=100,
            size_bytes=100,
            box_count=0,
        )
    with pytest.raises(ValidationError):
        PreviewSourceV1(
            task_id="t-1",
            filename="images/huge.jpg",
            sha256=DIGEST_A,
            width=5000,
            height=4000,
            size_bytes=100,
            box_count=0,
        )
    with pytest.raises(ValidationError):
        PreviewSpecV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            preview_id="p-oversize",
            framework_id="custom",
            task="detect",
            architecture_id="arch-s",
            input_digest=DIGEST_E,
            config_digest=DIGEST_A,
            dataset_snapshot_digest=DIGEST_B,
            policy_digest=DIGEST_C,
            seed=1,
            epoch=1,
            count=1,
            sources=[
                PreviewSourceV1(
                    task_id=f"t-{idx}",
                    filename=f"images/{idx}.jpg",
                    sha256=DIGEST_D,
                    width=100,
                    height=100,
                    size_bytes=PREVIEW_MAX_SOURCE_BYTES,
                    box_count=0,
                )
                for idx in range(
                    (PREVIEW_MAX_POOL_BYTES // PREVIEW_MAX_SOURCE_BYTES) + 1
                )
            ],
        )
    with pytest.raises(ValidationError):
        PreviewReportV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            status="succeeded",
            input_digest=DIGEST_A,
            policy_digest=DIGEST_B,
            cuda_initialized=True,
            outputs=[
                PreviewOutputV1(
                    filename="p0.png",
                    sha256=DIGEST_C,
                    size_bytes=10,
                    width=32,
                    height=32,
                    box_count=0,
                )
            ],
        )
    with pytest.raises(ValidationError):
        PreviewReportV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            status="succeeded",
            input_digest=DIGEST_A,
            policy_digest=DIGEST_B,
            outputs=[],
        )
    with pytest.raises(ValidationError):
        PreviewReportV1(
            protocol_version=TRAINER_PROTOCOL_VERSION,
            status="failed",
            input_digest=DIGEST_A,
            policy_digest=DIGEST_B,
        )
    nan_dir = tmp_path / "never-mkdir"
    nan_path = nan_dir / "bad.json"
    with pytest.raises(ValueError):
        atomic_write_json(nan_path, {"metric": float("inf")})
    assert not nan_dir.exists()
