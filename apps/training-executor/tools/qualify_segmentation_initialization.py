"""CPU native weights-only qualification with automatically removed fixtures."""

import hashlib
import json
from pathlib import Path
import runpy
import shutil
import tempfile
import uuid

from ultralytics import YOLO, settings
from ultralytics.utils import USER_CONFIG_DIR

from training_contract.catalog import digest
from training_contract.dataset import INSTANCE_RLE_CONTRACT
from training_contract.initialization import SEGMENTATION_POLICY_DIGEST, validate_pin
from training_executor.initialization import prepare, observation, validate_input
from training_executor.instance_rle import InstanceRLETrainer


class ObservedFreshState(Exception):
    pass


def qualification(base, root):
    import torch

    torch.set_num_threads(4)
    settings.update(
        {"sync": False, "wandb": False, "mlflow": False, "tensorboard": False}
    )
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for font in ("Arial.ttf", "Arial.Unicode.ttf"):
        shutil.copyfile(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", USER_CONFIG_DIR / font
        )
    data = root / "data"
    data.mkdir()
    helper = runpy.run_path(str(Path(__file__).with_name("qualify_instance_rle.py")))
    helper["fixture"](data)
    options = dict(
        data=str(data / "data.yaml"),
        device="cpu",
        imgsz=128,
        batch=1,
        workers=0,
        overlap_mask=False,
        mask_ratio=1,
        amp=False,
        cache=False,
        deterministic=True,
        optimizer="SGD",
        mosaic=0,
        mixup=0,
        cutmix=0,
        copy_paste=0,
        close_mosaic=0,
        translate=0,
        scale=0,
        degrees=0,
        shear=0,
        perspective=0,
        flipud=0,
        rect=False,
        multi_scale=0,
        plots=False,
        save=True,
        exist_ok=False,
        name="framework",
    )
    source = YOLO(base, task="segment")
    result = source.train(
        trainer=InstanceRLETrainer,
        **{**options, "epochs": 1, "project": str(root / "source")},
    )
    weights = root / "source/framework/weights/best.pt"
    raw = weights.read_bytes()
    identifiers = {
        key: str(uuid.uuid4())
        for key in (
            "project_id",
            "run_id",
            "attempt_id",
            "dataset_id",
            "model_version_id",
        )
    }
    classes = ["shape"]
    pin = {
        "kind": "trained_model",
        "policy_digest": SEGMENTATION_POLICY_DIGEST,
        **identifiers,
        "model_name": "Native CPU qualification",
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "task": "segment",
        "model_family": "yolo11",
        "base_model": "yolo11n-seg",
        "data_contract": INSTANCE_RLE_CONTRACT,
        "artifact": {
            "role": "weights",
            "key": f"projects/{identifiers['project_id']}/runs/{identifiers['run_id']}/attempts/{identifiers['attempt_id']}/best.pt",
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
            "report_digest": digest(result.results_dict),
            "normalized_model_digest": "f" * 64,
            "descriptor_digest": "1" * 64,
            "weights_sha256": hashlib.sha256(raw).hexdigest(),
            "validation_digest": "c" * 64,
        },
    }
    pin["source_digest"] = digest(pin)
    validate_pin(pin)
    spec = {
        "frozen_config": {
            "initialization": pin,
            "classes": classes,
            "task": "segment",
            "data_contract": INSTANCE_RLE_CONTRACT,
        },
        "initialization_path": str(weights),
    }
    validate_input(spec)
    candidate = YOLO(str(weights), task="segment")
    baseline = prepare(spec, candidate)
    captured = {}

    def inspect(trainer):
        captured.update(observation(spec, baseline, trainer))
        raise ObservedFreshState()

    candidate.add_callback("on_train_start", inspect)
    try:
        candidate.train(
            trainer=InstanceRLETrainer,
            **{
                **options,
                "epochs": 3,
                "resume": False,
                "project": str(root / "candidate"),
            },
        )
    except ObservedFreshState:
        pass
    assert captured["start_epoch"] == 0 and captured["optimizer_state_count"] == 0
    assert captured["source_tensor_digest"] == captured["loaded_tensor_digest"]
    assert (
        captured["mask_head_tensor_digest"]
        == captured["loaded_mask_head_tensor_digest"]
    )
    return {
        "device": "cpu",
        "source_epochs": 1,
        "target_epochs": 3,
        "fresh_state": captured,
        "gpu_qualification": "requires platform-owned execution; not claimed by CPU probe",
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="aap-segment-initialization-") as temporary:
        report = qualification(args.base, Path(temporary))
    print("AAP_SEGMENT_INITIALIZATION=" + json.dumps(report, sort_keys=True))
