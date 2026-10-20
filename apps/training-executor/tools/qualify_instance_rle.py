"""Bounded native consumer qualification; defaults to CPU and cleans fixtures.

Use the pinned backend image with both native adapters and this checkout mounted
read-only. Training and managed-return installation share a temporary directory.
GPU execution requires the platform resource owner
and is deliberately not acquired or bypassed by this probe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import resource
import tempfile

import numpy as np
from PIL import Image
import torch
import ultralytics
from ultralytics import YOLO, settings
from ultralytics.cfg import get_cfg

from training_contract.catalog import digest
from training_contract.dataset import INSTANCE_RLE_CONTRACT, instance_rle_evidence
from training_executor.adapters import validate_dataset
from training_executor.instance_rle import (
    InstanceRLEDataset,
    InstanceRLETrainer,
    consumer_evidence,
    decode_rle,
    transform_mask,
)
from training_executor.train import UltralyticsCheckpoints
from training_executor.native_identity import validate_architecture


def encode(mask):
    counts, previous, count = [], 0, 0
    for value in mask.flatten(order="F"):
        value = int(value)
        if value == previous:
            count += 1
        else:
            counts.append(count)
            previous, count = value, 1
    counts.append(count)
    return {"encoding": "coco_rle", "size": list(mask.shape), "counts": counts}


def fixture(root):
    samples = []
    hole = np.zeros((96, 160), dtype=np.uint8)
    hole[15:80, 20:140] = 1
    hole[35:60, 50:100] = 0
    disconnected = np.zeros_like(hole)
    disconnected[20:35, 40:65] = 1
    disconnected[65:82, 125:150] = 1
    originals = [hole, disconnected]
    for i, split in enumerate(("train", "train", "val", "val")):
        task_id = f"00000000-0000-0000-0000-{i + 1:012d}"
        image_path, label_path = (
            f"images/{split}/{task_id}.png",
            f"labels/{split}/{task_id}.json",
        )
        (root / image_path).parent.mkdir(parents=True, exist_ok=True)
        (root / label_path).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (160, 96), (30 + i, 50, 100)).save(root / image_path)
        instances = []
        for j, mask in enumerate(originals if i % 2 == 0 else []):
            rle = encode(mask)
            assert np.array_equal(decode_rle(rle), mask)
            instances.append(
                {
                    "annotation_id": f"10000000-0000-0000-0000-{i * 2 + j:012d}",
                    "annotation_version": 3,
                    "class_index": 0,
                    "source_geometry": "raster_mask",
                    "source_object_sha256": hashlib.sha256(
                        json.dumps(rle).encode()
                    ).hexdigest(),
                    **instance_rle_evidence(rle),
                    "rle": rle,
                }
            )
        payload = {"schema": 1, "size": [96, 160], "instances": instances}
        text = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        (root / label_path).write_text(text)
        samples.append(
            {
                "task_id": task_id,
                "image_path": image_path,
                "label_path": label_path,
                "width": 160,
                "height": 96,
                "sha256": hashlib.sha256((root / image_path).read_bytes()).hexdigest(),
                "label_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "split": split,
                "semantic_label_sha256": digest(
                    sorted(
                        (item["class_index"], item["pixel_sha256"])
                        for item in instances
                    )
                ),
                "instances": [
                    {k: v for k, v in item.items() if k != "rle"} for item in instances
                ],
            }
        )
    (root / "manifest.json").write_text(
        json.dumps({**INSTANCE_RLE_CONTRACT, "classes": ["shape"], "samples": samples})
    )
    (root / "classes.json").write_text('["shape"]')
    (root / "data.yaml").write_text(
        "train: images/train\nval: images/val\nnames: {0: shape}\n"
    )
    return originals


class Interrupted(Exception):
    pass


def qualify(base_path, temporary, base_model=None):
    torch.set_num_threads(4)
    settings.update(
        {"sync": False, "wandb": False, "mlflow": False, "tensorboard": False}
    )
    from ultralytics.utils import USER_CONFIG_DIR
    import shutil

    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for font in ("Arial.ttf", "Arial.Unicode.ttf"):
        shutil.copyfile(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", USER_CONFIG_DIR / font
        )
    if base_model is not None and not base_model.endswith("-seg"):
        from native_family_qualification import qualify_detection, qualify_libre

        if base_model.lower().startswith("libreyolo"):
            return qualify_libre(base_path, temporary, base_model)
        return qualify_detection(base_path, temporary, base_model)
    root = Path(temporary) / "data"
    root.mkdir()
    originals = fixture(root)
    native = YOLO(base_path, task="segment")
    base_model = base_model or Path(native.model.yaml["yaml_file"]).stem
    identity = validate_architecture(native.model, base_model)
    nms = False if identity.end2end else None
    args = get_cfg(
        overrides={
            "task": "segment",
            "imgsz": 128,
            "batch": 1,
            "workers": 0,
            "overlap_mask": False,
            "mask_ratio": 1,
            "nms": nms,
        }
    )
    dataset = InstanceRLEDataset(root, args, "train", 1)
    positive, negative = dataset.item(0), dataset.item(1)
    assert positive["masks"].shape == (2, 128, 128)
    assert negative["masks"].shape == (0, 128, 128)
    import cv2

    for index, original in enumerate(originals):
        expected = np.zeros((128, 128), dtype=np.uint8)
        expected[25:102] = cv2.resize(
            original, (128, 77), interpolation=cv2.INTER_NEAREST
        )
        assert np.array_equal(positive["masks"][index].numpy(), expected)
        assert np.array_equal(
            transform_mask(encode(original), 128, flip=True), expected[:, ::-1]
        )
    batch = dataset.collate_fn([positive, negative])
    assert batch["batch_idx"].tolist() == [0, 0]
    assert batch["masks"].shape == (2, 128, 128)
    tiny = np.zeros((128, 128), dtype=np.uint8)
    tiny[0, 0] = 1
    quarter = torch.nn.functional.interpolate(
        torch.from_numpy(tiny)[None, None].float(),
        (32, 32),
        mode="bilinear",
        align_corners=False,
    ).gt_(0.5)
    assert not quarter.any()
    frozen = {
        "task": "segment",
        "framework": "ultralytics",
        "framework_version": ultralytics.__version__,
        "base_model": {"id": base_model},
        "adapter_revision": 2,
        "classes": ["shape"],
        "data_contract": INSTANCE_RLE_CONTRACT,
        "parameters": {"epochs": 3, "imgsz": 128},
        "validation_digest": "qualification",
    }
    spec = {
        "frozen_config": frozen,
        "parameters": frozen["parameters"],
        "data_yaml": str(root / "data.yaml"),
    }
    validate_dataset(spec)
    first = Path(temporary) / "first"
    first.mkdir()
    checkpoints = UltralyticsCheckpoints(spec, first)
    native.add_callback("on_train_start", checkpoints.started)
    native.add_callback("on_model_save", checkpoints.saved)

    def interrupt(trainer):
        if trainer.epoch == 0:
            raise Interrupted(
                "qualification cancellation after a durable full checkpoint"
            )

    native.add_callback("on_fit_epoch_end", interrupt)
    options = dict(
        data=str(root / "data.yaml"),
        device="cpu",
        epochs=3,
        imgsz=128,
        batch=1,
        workers=0,
        overlap_mask=False,
        mask_ratio=1,
        nms=nms,
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
        project=str(first),
        name="framework",
    )
    try:
        native.train(trainer=InstanceRLETrainer, **options)
    except Interrupted:
        pass
    assert checkpoints.checkpoint and checkpoints.checkpoint["epoch"] == 1
    checkpoint_path = first / checkpoints.checkpoint["name"]
    blob = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    assert blob["optimizer"] and blob["aap_resume"]["config_digest"] == digest(frozen)
    second = Path(temporary) / "second"
    second.mkdir()
    resumed_spec = {**spec, "resume_path": str(checkpoint_path)}
    resumed_checkpoints = UltralyticsCheckpoints(resumed_spec, second)
    resumed = YOLO(str(checkpoint_path), task="segment")
    resumed.add_callback("on_train_start", resumed_checkpoints.started)
    resumed.add_callback("on_model_save", resumed_checkpoints.saved)

    # Match the production relocation override without changing resume state.
    class RelocatedTrainer(InstanceRLETrainer):
        def check_resume(self, overrides):
            super().check_resume(overrides)
            for field in ("project", "name", "data", "exist_ok"):
                setattr(self.args, field, overrides[field])
            self.args.save_dir = str(second / "framework")

    result = resumed.train(
        trainer=RelocatedTrainer,
        **{**options, "project": str(second), "resume": str(checkpoint_path)},
    )
    best = second / "framework/weights/best.pt"
    reloaded = YOLO(str(best), task="segment")
    assert reloaded.task == "segment" and reloaded.model.names == {0: "shape"}
    assert resumed.trainer.start_epoch == 1
    assert resumed_checkpoints.checkpoint["epoch"] == 3
    assert resumed.trainer.validator.dataloader.num_workers == 0
    assert type(resumed.trainer.validator).__name__ == "InstanceRLEValidator"
    from training_executor.evaluation import ultralytics_descriptor

    evaluator = ultralytics_descriptor(
        spec,
        {"sha256": hashlib.sha256(best.read_bytes()).hexdigest()},
        resumed.trainer.validator,
        result.results_dict,
    )
    assert evaluator["revision"] == 3
    from native_family_qualification import qualify_managed_return

    return {
        "native_version": ultralytics.__version__,
        "base_model": base_model,
        "head": identity.head,
        "initialization": "pretrained"
        if Path(base_path).suffix == ".pt"
        else "scratch",
        "managed_return": qualify_managed_return(best, base_model, Path(temporary)),
        "end2end": bool(resumed.trainer.validator.end2end),
        "nms": resumed.trainer.validator.args.nms,
        "device": "cpu",
        "input_shape": [96, 160],
        "train_shape": [128, 128],
        "instances": 2,
        "includes_explicit_negative": True,
        "exact_original_and_transformed_masks": True,
        "cancel_checkpoint_epoch": 1,
        "resume_start_epoch": resumed.trainer.start_epoch,
        "resume_completed_epoch": 3,
        "best_reloaded": True,
        "evaluator_revision": evaluator["revision"],
        "native_metrics": result.results_dict,
        "rss_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "gpu_peak_bytes": None,
        "gpu_qualification": "requires platform-owned GPU execution",
        "consumer": consumer_evidence(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--base")
    group.add_argument("--manifest")
    parser.add_argument("--model")
    parser.add_argument("--output")
    parser.add_argument("--timeout", type=int, default=1200)
    parsed = parser.parse_args()
    if parsed.manifest:
        from qualification_matrix import run_matrix

        report = run_matrix(
            Path(__file__),
            parsed.manifest,
            timeout=parsed.timeout,
            output=parsed.output,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="aap-rle-native-") as temporary:
            report = qualify(parsed.base, temporary, parsed.model)
        if parsed.output:
            Path(parsed.output).write_text(json.dumps(report, indent=2) + "\n")
    print("AAP_RLE_QUALIFICATION=" + json.dumps(report, sort_keys=True))
