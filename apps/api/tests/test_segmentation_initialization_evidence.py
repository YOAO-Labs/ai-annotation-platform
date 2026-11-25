"""Normalized registration never replaces frozen raw training-report identity."""

import asyncio
import copy
from datetime import UTC, datetime
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest
from sqlalchemy import Select

from training_contract.catalog import digest
from training_contract.dataset import SEGMENTATION_CONTRACT
from training_contract.initialization import SEGMENTATION_POLICY_DIGEST
from training_contract.models import model_spec
from app.services import training_dataset_storage as owners
from app.services import training_initialization as service
from app.services import training_run_storage as run_owners
from app.services import training_storage_io
from app.services.training_evaluations import parse_evaluation, SEMANTICS
from tests.test_training_storage_targets import target


class LearningFactsSession:
    autoflush = False

    def __init__(self, facts):
        self.facts = facts

    async def execute(self, statement, params=None):
        if isinstance(statement, Select):
            (source,) = statement.get_final_froms()
            function = source.element
            assert function.name == "training_dataset_facts"
            version_id, project_id, _ = (arg.value for arg in function.clauses)
            key = (function.name, project_id, version_id)
        else:
            name = statement.text.split("yoao_learning_v1.", 1)[1].split("(", 1)[0]
            fields = {
                "model_version_detail": ("project", "version"),
                "model_deployment_detail": ("project", "version", "deployment"),
                "training_get_run": ("project", "id"),
                "training_get_attempt": ("id",),
            }[name]
            key = (name, *(params[field] for field in fields))
        row = copy.deepcopy(self.facts.get(key))
        return SimpleNamespace(
            mappings=lambda: SimpleNamespace(one_or_none=lambda: row)
        )


def source_fixture(
    monkeypatch, *, base_model="yolo11n-seg", framework_version="8.4.177"
):
    project_id, run_id, attempt_id, dataset_id = (uuid.uuid4() for _ in range(4))
    classes = ["shape"]
    now = datetime.now(UTC)
    task = model_spec("ultralytics", base_model).task
    segmentation = task == "segment"
    report_task = "instance-segmentation" if segmentation else "detection"
    manifest = dict(SEGMENTATION_CONTRACT) if segmentation else {}
    manifest["classes"] = classes
    config = {
        "task": task,
        "framework": "ultralytics",
        "framework_version": framework_version,
        "base_model": {"id": base_model},
        **({"data_contract": dict(SEGMENTATION_CONTRACT)} if segmentation else {}),
        "classes": classes,
        "dataset_id": str(dataset_id),
        "dataset_digest": "a" * 64,
        "validation_digest": "b" * 64,
        "parameters": {"epochs": 3, "imgsz": 128},
    }
    run = SimpleNamespace(
        id=run_id,
        project_id=project_id,
        creator_id=None,
        study_id=None,
        initialization_model_id=None,
        auto_trigger_id=None,
        async_job_id=None,
        request_digest="1" * 64,
        name="Source run",
        status="succeeded",
        origin=None,
        authorization={},
        current_attempt_id=attempt_id,
        metrics={},
        error_message=None,
        created_at=now,
        completed_at=now,
        config=config,
        config_digest=digest(config),
        dataset_id=dataset_id,
    )
    run.storage_ref = owners.make_ref(
        target(),
        2,
        run_owners.prefix_for(project_id, run_id),
        purpose="training-run",
    )
    owner = run_owners.resolve(run)
    runtime = {
        "python": "3.12.3",
        "torch": "2.14.1+cu132",
        "torchvision": "0.29.1+cu132",
        "cuda": "13.2",
        "image_digest": "sha256:" + "c" * 64,
    }
    prefix = f"{owner.prefix}attempts/{attempt_id}/"
    weight_bytes = b"retained training weights"
    weights = {
        "name": "best.pt",
        "key": prefix + "best.pt",
        "role": "weights",
        "sha256": hashlib.sha256(weight_bytes).hexdigest(),
        "size_bytes": len(weight_bytes),
    }
    scores = (
        {"mAP50-95(B)": 0.7, "mAP50-95(M)": 0.6} if segmentation else {"mAP50-95": 0.7}
    )
    report = {
        "validation_digest": config["validation_digest"],
        "evaluation": {
            "framework": "ultralytics",
            "version": framework_version,
            "task": report_task,
        },
        "summary": {
            **{f"metrics/{key}": value for key, value in scores.items()},
            "fitness": 1.3 if segmentation else 0.7,
        },
        "per_class": [{"index": 0, "name": "shape", **scores}],
        "comparison": {
            "revision": 2 if segmentation else 1,
            "config_digest": run.config_digest,
            "candidate": {
                "weights_sha256": weights["sha256"],
                "selection": "native_best",
                "best_epoch": None,
            },
            "benchmark": {
                "validation_digest": config["validation_digest"],
                "classes": classes,
                "class_space": "frozen_native",
            },
            "evaluator": {
                "framework": "ultralytics",
                "version": framework_version,
                "task": report_task,
                "implementation": (
                    "ultralytics.models.yolo.segment.val.SegmentationValidator"
                    if segmentation
                    else "ultralytics.models.yolo.detect.val.DetectionValidator"
                ),
                "source_sha256": "e" * 64,
                "options": {
                    "split": "val",
                    "imgsz": 128,
                    "batch": 1,
                    "conf": 0.001,
                    "iou": 0.7,
                    "max_det": 300,
                    "half": False,
                    "augment": False,
                    "device": "cuda:0",
                    "workers": 2,
                    "rect": False,
                    "single_cls": False,
                    "classes": None,
                    "agnostic_nms": False,
                    **(
                        {
                            "overlap_mask": False,
                            "mask_ratio": 1,
                            "mask_processing": "ultralytics.utils.ops.process_mask",
                        }
                        if segmentation
                        else {}
                    ),
                },
            },
            "semantics": {f"metrics/{key}": SEMANTICS for key in scores},
        },
    }
    raw = json.dumps(report).encode()
    metrics = {
        "name": "metrics.json",
        "key": prefix + "metrics.json",
        "role": "metrics",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    attempt = SimpleNamespace(
        id=attempt_id,
        run_id=run_id,
        ordinal=1,
        resume_from_id=None,
        executor_id=None,
        boot_id=None,
        claim_id=None,
        finish_request_digest=None,
        resource_id=None,
        upload_inventory={},
        observation_state={},
        failure=None,
        native_budget=None,
        native_accounting=None,
        error_message=None,
        heartbeat_at=now,
        started_at=now,
        completed_at=now,
        created_at=now,
        status="succeeded",
        runtime=runtime,
        artifacts={"best.pt": weights, "metrics.json": metrics},
    )
    evaluation = report
    if segmentation:
        checked = parse_evaluation(report, run, attempt, metrics, weights, 0, 1)
        evaluation = service.normalized_registration_evaluation(report, checked)
    version = SimpleNamespace(
        id=uuid.uuid4(),
        name="Source model",
        project_id=project_id,
        run_id=run_id,
        attempt_id=attempt_id,
        request_id=uuid.uuid4(),
        request_digest="2" * 64,
        created_at=now,
        framework="ultralytics",
        framework_version=framework_version,
        artifact=weights,
        classes=classes,
        bindings=classes,
        binding_digest=digest({"classes": classes, "bindings": classes}),
        evaluation=evaluation,
        provenance={
            "run_id": str(run_id),
            "attempt_id": str(attempt_id),
            "dataset_id": str(dataset_id),
            "dataset_digest": config["dataset_digest"],
            "validation_digest": config["validation_digest"],
            "config_digest": run.config_digest,
            "base_model": config["base_model"],
            "runtime": runtime,
        },
    )
    dataset = dict(
        id=dataset_id,
        project_id=project_id,
        creator_id=None,
        parent_id=None,
        async_job_id=None,
        attempt_id=uuid.uuid4(),
        name="Source dataset",
        status="ready",
        snapshot_digest=config["dataset_digest"],
        snapshot={},
        manifest=manifest,
    )

    objects = {
        (owner.bucket, weights["key"]): weight_bytes,
        (owner.bucket, metrics["key"]): raw,
    }

    def head_object(*, Bucket, Key):
        return {
            "ContentLength": len(objects[Bucket, Key]),
            "Metadata": {"attempt-id": str(attempt_id)},
        }

    def get_object(*, Bucket, Key):
        return {
            **head_object(Bucket=Bucket, Key=Key),
            "Body": io.BytesIO(objects[Bucket, Key]),
        }

    transport = SimpleNamespace(
        client=SimpleNamespace(
            head_object=Mock(side_effect=head_object),
            get_object=Mock(side_effect=get_object),
        )
    )

    class DB(LearningFactsSession):
        storage_transport = transport

        def __init__(self):
            super().__init__(
                {
                    ("model_version_detail", project_id, version.id): vars(version),
                    ("training_get_run", project_id, run_id): vars(run),
                    ("training_get_attempt", attempt_id): vars(attempt),
                    ("training_dataset_facts", project_id, dataset_id): dataset,
                }
            )

    async def routed(db, selected_owner, purpose, callback, *args, **kwargs):
        assert selected_owner == owner
        return callback(transport, *args, **kwargs)

    monkeypatch.setattr(training_storage_io, "run", routed)
    monkeypatch.setattr(
        training_storage_io.artifact_reads, "resolve", AsyncMock(return_value=None)
    )
    return project_id, version, DB, report, metrics, owner


def test_segment_pin_freezes_raw_and_registered_evidence_independently(monkeypatch):
    project, version, DB, report, metrics, owner = source_fixture(monkeypatch)
    pin, frozen_metrics = asyncio.run(service.source_pin(DB(), project, version.id))
    assert pin["policy_digest"] == SEGMENTATION_POLICY_DIGEST
    assert pin["evaluation"]["report_digest"] == digest(report)
    assert pin["evaluation"]["normalized_model_digest"] == digest(version.evaluation)
    assert (
        pin["evaluation"]["report_digest"]
        != pin["evaluation"]["normalized_model_digest"]
    )
    assert frozen_metrics == metrics
    service.verify_storage(
        pin, metrics, owner=owner, full=True, transport=DB.storage_transport
    )
    assert not service.compatible_pin(pin, "ultralytics", "yolo11n-seg", ["shape"])
    assert service.compatible_pin(pin, "ultralytics", "yolo11n", ["shape"])


def test_normalized_metric_drift_and_report_tampering_are_rejected(monkeypatch):
    project, version, DB, report, metrics, owner = source_fixture(monkeypatch)
    original = copy.deepcopy(version.evaluation)
    version.evaluation["summary"]["metrics/mAP50-95(M)"] = 0.9
    with pytest.raises(ValueError, match="语义不一致"):
        asyncio.run(service.source_pin(DB(), project, version.id))
    version.evaluation = original
    pin, _ = asyncio.run(service.source_pin(DB(), project, version.id))
    changed = copy.deepcopy(report)
    changed["comparison"]["evaluator"]["options"]["iou"] = 0.8
    monkeypatch.setattr(
        "app.services.training_artifacts.read_json_artifact",
        lambda *args, **kwargs: changed,
    )
    with pytest.raises(ValueError, match="报告不一致"):
        service.verify_storage(
            pin, metrics, owner=owner, full=True, transport=DB.storage_transport
        )
