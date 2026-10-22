"""Exercise native detection and bitmap segmentation evaluation without downloads."""

import json
import random
import shutil

import pytest


@pytest.mark.parametrize(
    "base_model",
    ["yolov8n", "yolo11n", "yolo26n", "yolov8n-seg", "yolo11n-seg", "yolo26n-seg"],
)
def test_native_training_metrics_are_separate_unaugmented_and_rng_neutral(
    tmp_path, base_model
):
    torch = pytest.importorskip("torch")
    np = pytest.importorskip("numpy")
    pytest.importorskip("ultralytics")
    from PIL import Image, ImageDraw
    from ultralytics import YOLO
    from ultralytics.utils import USER_CONFIG_DIR
    from training_contract.models import model_spec

    spec = model_spec("ultralytics", base_model)
    task = spec.task
    torch.set_num_threads(2)

    from training_executor.instance_rle import InstanceRLETrainer
    from training_executor.train import (
        UltralyticsTrainingEvaluation,
        ultralytics_epoch_metrics,
        ultralytics_model_path,
    )

    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    for name in ("Arial.ttf", "Arial.Unicode.ttf"):
        if not (USER_CONFIG_DIR / name).exists():
            shutil.copyfile(font, USER_CONFIG_DIR / name)

    samples = []
    # The train and val sets have different membership counts and object positions.
    for split, count in (("train", 2), ("val", 1)):
        (tmp_path / "images" / split).mkdir(parents=True)
        (tmp_path / "labels" / split).mkdir(parents=True)
        for index in range(count):
            left = 16 + index * 16
            image = Image.new("RGB", (128, 128), "black")
            ImageDraw.Draw(image).rectangle((left, 24, left + 31, 71), fill="white")
            image_path = f"images/{split}/{index}.png"
            image.save(tmp_path / image_path)
            label_path = f"labels/{split}/{index}.json"
            mask = np.zeros((128, 128), dtype=np.uint8)
            mask[24:72, left : left + 32] = 1
            counts, previous, length = [], 0, 0
            for value in mask.flatten(order="F"):
                if value != previous:
                    counts.append(length)
                    previous, length = int(value), 0
                length += 1
            counts.append(length)
            instance = {
                "annotation_id": f"{split}-{index}",
                "annotation_version": 1,
                "class_index": 0,
                "bbox_xyxy": [left, 24, left + 32, 72],
                "rle": {"size": [128, 128], "counts": counts},
            }
            (tmp_path / label_path).write_text(json.dumps({"instances": [instance]}))
            (tmp_path / "labels" / split / f"{index}.txt").write_text(
                f"0 {(left + 16) / 128} {48 / 128} {32 / 128} {48 / 128}\n"
            )
            samples.append(
                dict(
                    split=split,
                    image_path=image_path,
                    label_path=label_path,
                    task_id=f"{split}-{index}",
                    width=128,
                    height=128,
                    instances=[
                        {key: value for key, value in instance.items() if key != "rle"}
                    ],
                )
            )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"samples": samples, "classes": ["shape"]})
    )
    data = tmp_path / "data.yaml"
    data.write_text(
        f"path: {tmp_path}\ntrain: images/train\nval: images/val\nnames: ['shape']\n"
    )
    evaluator = UltralyticsTrainingEvaluation(tmp_path / "monitor")
    records = []

    def on_epoch(trainer):
        if not 0 <= trainer.epoch < trainer.epochs:
            return
        validation = dict(trainer.metrics)
        fitness, best = trainer.fitness, trainer.best_fitness
        rng = (random.getstate(), np.random.get_state(), torch.get_rng_state().clone())
        metrics = {**ultralytics_epoch_metrics(trainer), **evaluator(trainer)}
        assert random.getstate() == rng[0]
        assert np.array_equal(np.random.get_state()[1], rng[1][1])
        assert torch.equal(torch.get_rng_state(), rng[2])
        assert trainer.metrics == validation
        assert (trainer.fitness, trainer.best_fitness) == (fitness, best)
        assert len(evaluator.validator.dataloader.dataset) == 2
        if task == "segment":
            dataset = evaluator.validator.dataloader.dataset
            assert dataset.split == "train" and not dataset.augment
            assert trainer.train_loader.dataset.augment
        records.append(metrics)

    model = YOLO(
        ultralytics_model_path(
            {
                "base_path": "/unused/pretrained.pt",
                "frozen_config": {
                    "base_model": {"id": base_model},
                    "configuration": {"effective_values": {"pretrained": False}},
                },
            }
        )
    )
    assert not model.ckpt
    model.add_callback("on_fit_epoch_end", on_epoch)
    model.train(
        **({"trainer": InstanceRLETrainer} if task == "segment" else {}),
        data=str(data),
        epochs=2,
        batch=2,
        imgsz=128,
        device="cpu",
        workers=0,
        amp=False,
        plots=False,
        save=True,
        val=True,
        close_mosaic=0,
        pretrained=False,
        project=str(tmp_path),
        name="native",
        overlap_mask=False,
        nms=False if spec.end2end else None,
    )
    assert len(records) == 2
    for metrics in records:
        for suffix in ("(B)", "(M)") if task == "segment" else ("(B)",):
            for name in ("precision", "recall", "mAP50", "mAP50-95"):
                key = f"metrics/{name}{suffix}"
                assert key in metrics and f"train/{key}" in metrics
        for name in (
            "box",
            "cls",
            "l1" if spec.end2end else "dfl",
            *(["seg", "sem"] if task == "segment" else []),
        ):
            assert f"train/{name}_loss" in metrics
            assert f"val/{name}_loss" in metrics
        if spec.end2end:
            assert "train/dfl_loss" not in metrics and "val/dfl_loss" not in metrics
