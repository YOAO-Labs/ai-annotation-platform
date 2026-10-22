"""Pinned native identity, full tensor transfer and input checks before loading."""

import copy
import hashlib
import threading
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path

import pytest
import torch
from training_contract.catalog import digest
from training_contract.initialization import POLICY_DIGEST
from training_contract.observability import InputMismatchError
from training_executor.initialization import observation, prepare, validate_input
from training_executor.worker import Supervisor


def source_pin(raw):
    identifiers = {
        key: str(uuid.uuid4())
        for key in (
            "project_id",
            "model_version_id",
            "run_id",
            "attempt_id",
            "dataset_id",
        )
    }
    classes = ["car", "person"]
    pin = {
        "kind": "trained_model",
        "policy_digest": POLICY_DIGEST,
        **identifiers,
        "model_name": "Native source",
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "task": "image-detection",
        "model_family": "yolo11",
        "base_model": "yolo11n",
        "artifact": {
            "key": f"projects/{identifiers['project_id']}/runs/{identifiers['run_id']}/attempts/{identifiers['attempt_id']}/best.pt",
            "role": "weights",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        },
        "classes": classes,
        "classes_digest": digest(classes),
        "bindings": classes,
        "binding_digest": digest({"classes": classes, "bindings": classes}),
        "config_digest": "a" * 64,
        "dataset_digest": "b" * 64,
        "validation_digest": "c" * 64,
        "runtime": {
            "python": "3.12.3",
            "torch": "2.14.1+cu132",
            "torchvision": "0.29.1+cu132",
            "cuda": "13.2",
            "image_digest": "sha256:" + "d" * 64,
        },
        "evaluation": {
            "report_sha256": "e" * 64,
            "report_digest": "f" * 64,
            "validation_digest": "c" * 64,
        },
    }
    pin["source_digest"] = digest(pin)
    return pin


def specification(tmp_path):
    raw = b"downloaded and verified source bytes"
    pin = source_pin(raw)
    path = tmp_path / "initialization.pt"
    path.write_bytes(raw)
    return {
        "frozen_config": {"initialization": pin, "classes": pin["classes"]},
        "initialization_path": str(path),
    }


@pytest.mark.parametrize("problem", ["missing", "size", "digest"])
def test_input_is_checked_before_native_deserialization(tmp_path, problem):
    spec = specification(tmp_path)
    validate_input(spec)
    path = tmp_path / "initialization.pt"
    if problem == "missing":
        path.unlink()
    elif problem == "size":
        path.write_bytes(b"short")
    else:
        path.write_bytes(b"x" * path.stat().st_size)
    with pytest.raises(InputMismatchError):
        validate_input(spec)
    spec["resume_path"] = "/state/own-checkpoint.pt"
    validate_input(spec)
    baseline = prepare(spec, None)
    assert baseline["mode"] == "checkpoint_resume"
    assert (
        observation(spec, baseline, SimpleNamespace(start_epoch=2))["start_epoch"] == 2
    )


def test_pinned_detection_model_transfers_every_tensor_and_requires_fresh_state(
    tmp_path,
):
    from ultralytics.nn.tasks import DetectionModel

    spec = specification(tmp_path)
    native = DetectionModel("yolo11n.yaml", nc=2, verbose=False).half()
    native.names = {0: "car", 1: "person"}
    wrapper = SimpleNamespace(model=native, task="detect")
    baseline = prepare(spec, wrapper)
    loaded = copy.deepcopy(native).float()
    optimizer = torch.optim.SGD(loaded.parameters(), lr=0.01)
    trainer = SimpleNamespace(
        model=loaded,
        optimizer=optimizer,
        scheduler=torch.optim.lr_scheduler.LambdaLR(optimizer, lambda _: 1),
        scaler=torch.amp.GradScaler("cpu", enabled=False),
        ema=SimpleNamespace(updates=0),
        best_fitness=0.0,
        best=tmp_path / "best.pt",
        start_epoch=0,
    )
    receipt = observation(spec, baseline, trainer)
    assert receipt["source_tensor_digest"] == receipt["loaded_tensor_digest"]
    assert receipt["tensor_count"] == len(native.state_dict()) > 400
    with torch.no_grad():
        next(loaded.parameters()).add_(0.01)
    with pytest.raises(InputMismatchError):
        observation(spec, baseline, trainer)
    native.names = {0: "person", 1: "car"}
    with pytest.raises(InputMismatchError, match="class identity"):
        prepare(spec, wrapper)


@pytest.mark.parametrize("exact_size,maximum", [(5, 100), (4, 3)])
def test_download_enforces_exact_and_maximum_sizes_without_exposing_api_auth(
    tmp_path, monkeypatch, exact_size, maximum
):
    supervisor = object.__new__(Supervisor)
    supervisor.shutdown = threading.Event()
    supervisor.stop_training = threading.Event()
    raw = b"data"
    calls = []

    @contextmanager
    def stream(*args, **kwargs):
        calls.append((args, kwargs))
        yield SimpleNamespace(
            raise_for_status=lambda: None, iter_bytes=lambda _: iter([raw])
        )

    monkeypatch.setattr("training_executor.worker.httpx.stream", stream)
    with pytest.raises(InputMismatchError):
        supervisor.download(
            "https://owned-object.invalid/download",
            tmp_path / "input.pt",
            hashlib.sha256(raw).hexdigest(),
            maximum,
            exact_size=exact_size,
        )
    assert not (tmp_path / "input.pt").exists()
    assert calls[0][1] == {"timeout": 60, "trust_env": False}


def segment_specification(tmp_path):
    from training_contract.initialization import SEGMENTATION_POLICY_DIGEST
    from training_contract.dataset import INSTANCE_RLE_CONTRACT

    spec = specification(tmp_path)
    pin = spec["frozen_config"]["initialization"]
    pin.update(
        policy_digest=SEGMENTATION_POLICY_DIGEST,
        task="segment",
        base_model="yolo11n-seg",
        data_contract=dict(INSTANCE_RLE_CONTRACT),
    )
    pin["evaluation"].update(
        normalized_model_digest="1" * 64,
        descriptor_digest="2" * 64,
        weights_sha256=pin["artifact"]["sha256"],
    )
    pin["source_digest"] = digest(
        {key: value for key, value in pin.items() if key != "source_digest"}
    )
    spec["frozen_config"].update(
        task="segment", data_contract=dict(INSTANCE_RLE_CONTRACT)
    )
    return spec


def test_native_segmentation_warm_start_transfers_mask_head_and_resets_state(tmp_path):
    from ultralytics.nn.tasks import SegmentationModel

    spec = segment_specification(tmp_path)
    native = SegmentationModel("yolo11n-seg.yaml", nc=2, verbose=False).half()
    native.names = {0: "car", 1: "person"}
    baseline = prepare(spec, SimpleNamespace(model=native, task="segment"))
    loaded = copy.deepcopy(native).float()
    optimizer = torch.optim.SGD(loaded.parameters(), lr=0.01)
    trainer = SimpleNamespace(
        model=loaded,
        optimizer=optimizer,
        scheduler=torch.optim.lr_scheduler.LambdaLR(optimizer, lambda _: 1),
        scaler=torch.amp.GradScaler("cpu", enabled=False),
        ema=SimpleNamespace(updates=0),
        best_fitness=0.0,
        best=tmp_path / "best.pt",
        start_epoch=0,
    )
    receipt = observation(spec, baseline, trainer)
    assert receipt["source_tensor_digest"] == receipt["loaded_tensor_digest"]
    assert (
        receipt["mask_head_tensor_digest"] == receipt["loaded_mask_head_tensor_digest"]
    )
    assert receipt["mask_head_tensor_count"] == len(native.model[-1].state_dict()) > 50
    with torch.no_grad():
        next(loaded.model[-1].parameters()).add_(0.01)
    with pytest.raises(InputMismatchError):
        observation(spec, baseline, trainer)
    # Resume uses the original experiment checkpoint and never rereads the
    # optional source initialization file, even when that source is gone.
    Path(spec["initialization_path"]).unlink()
    spec["resume_path"] = "/state/own-checkpoint.pt"
    validate_input(spec)
    assert prepare(spec, None)["mode"] == "checkpoint_resume"
