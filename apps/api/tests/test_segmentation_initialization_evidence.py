"""Normalized registration never replaces frozen raw training-report identity."""

import asyncio
import copy
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from training_contract.catalog import digest
from training_contract.dataset import SEGMENTATION_CONTRACT
from training_contract.initialization import SEGMENTATION_POLICY_DIGEST
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingRun
from app.db.models.trained_model import TrainedModelVersion
from app.services import training_dataset_storage as owners
from app.services import training_initialization as service
from app.services import training_run_storage as run_owners
from app.services import training_storage_io
from app.services.storage import storage_service
from app.services.training_evaluations import parse_evaluation, SEMANTICS
from tests.test_training_storage_targets import target


def source_fixture(
    monkeypatch, *, base_model="yolo11n-seg", framework_version="8.4.177"
):
    project_id, run_id, attempt_id, dataset_id = (uuid.uuid4() for _ in range(4))
    classes = ["shape"]
    config = {
        "task": "segment",
        "framework": "ultralytics",
        "framework_version": framework_version,
        "base_model": {"id": base_model},
        "data_contract": dict(SEGMENTATION_CONTRACT),
        "classes": classes,
        "dataset_id": str(dataset_id),
        "dataset_digest": "a" * 64,
        "validation_digest": "b" * 64,
        "parameters": {"epochs": 3, "imgsz": 128},
    }
    run = SimpleNamespace(
        id=run_id,
        project_id=project_id,
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
    weights = {
        "name": "best.pt",
        "key": prefix + "best.pt",
        "role": "weights",
        "sha256": "d" * 64,
        "size_bytes": 100,
    }
    report = {
        "validation_digest": config["validation_digest"],
        "evaluation": {
            "framework": "ultralytics",
            "version": framework_version,
            "task": "instance-segmentation",
        },
        "summary": {
            "metrics/mAP50-95(B)": 0.7,
            "metrics/mAP50-95(M)": 0.6,
            "fitness": 1.3,
        },
        "per_class": [
            {"index": 0, "name": "shape", "mAP50-95(B)": 0.7, "mAP50-95(M)": 0.6}
        ],
        "comparison": {
            "revision": 2,
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
                "task": "instance-segmentation",
                "implementation": "ultralytics.models.yolo.segment.val.SegmentationValidator",
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
                    "overlap_mask": False,
                    "mask_ratio": 1,
                    "mask_processing": "ultralytics.utils.ops.process_mask",
                },
            },
            "semantics": {
                key: SEMANTICS for key in ("metrics/mAP50-95(B)", "metrics/mAP50-95(M)")
            },
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
        status="succeeded",
        runtime=runtime,
        artifacts={"best.pt": weights, "metrics.json": metrics},
    )
    checked = parse_evaluation(report, run, attempt, metrics, weights, 0, 1)
    normalized = service.normalized_registration_evaluation(report, checked)
    version = SimpleNamespace(
        id=uuid.uuid4(),
        name="Segment source",
        project_id=project_id,
        run_id=run_id,
        attempt_id=attempt_id,
        framework="ultralytics",
        framework_version=framework_version,
        artifact=weights,
        classes=classes,
        bindings=classes,
        binding_digest=digest({"classes": classes, "bindings": classes}),
        evaluation=normalized,
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
    dataset = SimpleNamespace(
        id=dataset_id, manifest={**SEGMENTATION_CONTRACT, "classes": classes}
    )

    class DB:
        def __init__(self):
            self.rows = iter([version, run, dataset])

        async def scalar(self, statement):
            entity = statement.column_descriptions[0].get("entity")
            found = {
                TrainedModelVersion: version,
                TrainingRun: run,
                TrainingDatasetVersion: dataset,
            }.get(entity)
            return found if found is not None else next(self.rows)

        async def get(self, model, identifier):
            return version if model is TrainedModelVersion else attempt

    async def routed(db, owner, purpose, callback, *args, **kwargs):
        # Saved-owner resolution is unit-tested elsewhere; this fixture keeps
        # the same pinned owner while the fake provider serves the objects.
        return callback(storage_service, *args, **kwargs)

    monkeypatch.setattr(training_storage_io, "run", routed)
    monkeypatch.setattr(
        training_storage_io.artifact_reads, "resolve", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "app.services.training_artifacts.storage.client.get_object",
        lambda **kwargs: {
            "Body": io.BytesIO(raw),
            "Metadata": {"attempt-id": str(attempt_id)},
        },
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
    monkeypatch.setattr(
        "app.services.training_runs.verify_artifact", lambda *args: None
    )
    service.verify_storage(pin, metrics, owner=owner, full=True)
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
        "app.services.training_runs.verify_artifact", lambda *args: None
    )
    monkeypatch.setattr(
        "app.services.training_artifacts.read_json_artifact", lambda *args: changed
    )
    with pytest.raises(ValueError, match="报告不一致"):
        service.verify_storage(pin, metrics, owner=owner, full=True)
