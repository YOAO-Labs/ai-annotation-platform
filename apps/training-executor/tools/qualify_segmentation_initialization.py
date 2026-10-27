"""CPU native weights-only qualification with automatically removed fixtures."""

import hashlib
import json
from pathlib import Path
import runpy
import shutil
import tempfile
import uuid

from ultralytics import YOLO, settings
import ultralytics
from ultralytics.utils import USER_CONFIG_DIR

from training_contract.catalog import digest
from training_contract.dataset import INSTANCE_RLE_CONTRACT
from training_contract.initialization import policy_for_model, validate_pin
from training_executor.initialization import prepare, observation, validate_input
from training_executor.instance_rle import InstanceRLETrainer
from training_executor.native_identity import validate_architecture
from ultralytics.models.yolo.detect import DetectionTrainer


class ObservedFreshState(Exception):
    pass


def qualification(base, root, base_model=None):
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
    source = YOLO(base)
    base_model = base_model or Path(source.model.yaml["yaml_file"]).stem
    identity = validate_architecture(source.model, base_model)
    segmentation = identity.task == "segment"
    if segmentation:
        helper = runpy.run_path(
            str(Path(__file__).with_name("qualify_instance_rle.py"))
        )
        helper["fixture"](data)
    else:
        from native_family_qualification import detection_fixture

        detection_fixture(data)
    trainer_class = InstanceRLETrainer if segmentation else DetectionTrainer
    policy = policy_for_model(base_model, ultralytics.__version__)
    options = dict(
        data=str(data / "data.yaml"),
        device="cpu",
        imgsz=128,
        batch=1,
        workers=0,
        overlap_mask=False,
        mask_ratio=1,
        nms=False if identity.end2end else None,
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
    result = source.train(
        trainer=trainer_class,
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
        "policy_digest": digest(policy),
        **identifiers,
        "model_name": "Native CPU qualification",
        "framework": "ultralytics",
        "framework_version": ultralytics.__version__,
        "task": policy["task"],
        "model_family": identity.family,
        "base_model": identity.id,
        **({"data_contract": INSTANCE_RLE_CONTRACT} if segmentation else {}),
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
            "validation_digest": "c" * 64,
            **(
                {
                    "normalized_model_digest": "f" * 64,
                    "descriptor_digest": "1" * 64,
                    "weights_sha256": hashlib.sha256(raw).hexdigest(),
                }
                if segmentation
                else {}
            ),
        },
    }
    pin["source_digest"] = digest(pin)
    validate_pin(pin)
    spec = {
        "frozen_config": {
            "initialization": pin,
            "classes": classes,
            "task": policy["task"],
            **({"data_contract": INSTANCE_RLE_CONTRACT} if segmentation else {}),
        },
        "initialization_path": str(weights),
    }
    validate_input(spec)
    candidate = YOLO(str(weights), task=identity.task)
    baseline = prepare(spec, candidate)
    captured = {}

    def inspect(trainer):
        captured.update(observation(spec, baseline, trainer))
        raise ObservedFreshState()

    candidate.add_callback("on_train_start", inspect)
    try:
        candidate.train(
            trainer=trainer_class,
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
    if segmentation:
        assert (
            captured["mask_head_tensor_digest"]
            == captured["loaded_mask_head_tensor_digest"]
        )
    return {
        "base_model": base_model,
        "native_version": ultralytics.__version__,
        "device": "cpu",
        "source_epochs": 1,
        "target_epochs": 3,
        "fresh_state": captured,
        "gpu_qualification": "requires platform-owned execution; not claimed by CPU probe",
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--base")
    group.add_argument("--manifest")
    parser.add_argument("--model")
    parser.add_argument("--output")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    if args.manifest:
        from qualification_matrix import run_matrix

        report = run_matrix(
            Path(__file__), args.manifest, timeout=args.timeout, output=args.output
        )
    else:
        with tempfile.TemporaryDirectory(
            prefix="aap-segment-initialization-"
        ) as temporary:
            report = qualification(args.base, Path(temporary), args.model)
        if args.output:
            Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print("AAP_SEGMENT_INITIALIZATION=" + json.dumps(report, sort_keys=True))
