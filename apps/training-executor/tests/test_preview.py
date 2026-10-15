import copy
import hashlib
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from training_contract import definition, resolve
from training_contract.catalog import digest
from training_contract.preview import POLICY_DIGEST, input_digest, native_values

from training_executor.files import digest_file
from training_executor.preview import native_preview, verify_pool


def fixture_spec(root, *, values=None, epoch=1, count=4):
    for name in ("images/train", "labels/train"):
        (root / name).mkdir(parents=True)
    sources = []
    for index, color in enumerate(("orange", "cyan", "magenta", "lime")):
        image = Image.new("RGB", (320, 240), color)
        ImageDraw.Draw(image).rectangle((60, 45, 190, 170), outline="black", width=7)
        image_path = f"images/train/{index}.png"
        label_path = f"labels/train/{index}.txt"
        image.save(root / image_path)
        labels = (
            ""
            if index == 3
            else f"{index % 2} 0.4 0.4 0.4 0.5\n0 0.99 0.99 0.01 0.01\n"
        )
        (root / label_path).write_text(labels)
        sources.append(
            dict(
                task_id=str(index),
                image_path=image_path,
                label_path=label_path,
                labels=labels,
                sha256=digest_file(root / image_path),
                label_sha256=hashlib.sha256(labels.encode()).hexdigest(),
                size_bytes=(root / image_path).stat().st_size,
                width=320,
                height=240,
            )
        )
    schema = definition("ultralytics", "yolo11n")
    configuration = resolve(
        "ultralytics",
        "yolo11n",
        dict(
            schema_id=schema["schema_id"],
            schema_digest=schema["schema_digest"],
            values=values
            or dict(
                epochs=10,
                batch=2,
                imgsz=128,
                mosaic=1.0,
                mixup=1.0,
                degrees=15.0,
                translate=0.2,
                close_mosaic=2,
            ),
        ),
    )
    base = Path(os.environ.get("PREVIEW_TEST_BASE", "/models/yolo11n.pt"))
    if not base.is_file():
        pytest.skip(
            "Pinned native preview tests require the controlled YOLO11 fixture weights"
        )
    config = dict(
        adapter_revision=2,
        framework="ultralytics",
        framework_version=schema["framework_version"],
        image_digest="sha256:" + "a" * 64,
        configuration=configuration,
        base_model=dict(id="yolo11n", sha256=digest_file(base)),
        classes=["a", "b"],
        parameters={
            key: configuration["effective_values"][key]
            for key in ("epochs", "batch", "imgsz", "seed", "lr0", "patience")
        },
    )
    spec = dict(
        preview_id="fixture",
        input_digest="a" * 64,
        config=config,
        config_digest=digest(config),
        policy_digest=POLICY_DIGEST,
        seed=123,
        epoch=epoch,
        count=count,
        sources=sources,
        base_path=str(base),
        native_values=native_values(config),
        dataset_snapshot_digest="a" * 64,
        pool_summary={"selected": len(sources)},
    )
    spec["input_digest"] = input_digest(spec)
    return spec


def test_native_trace_matches_uninstrumented_pixels_and_boxes(tmp_path):
    spec = fixture_spec(tmp_path)
    before = copy.deepcopy(spec)
    native = native_preview(spec, tmp_path, traced=False)
    observed = native_preview(spec, tmp_path)
    again = native_preview(spec, tmp_path)
    assert spec == before
    assert observed["outputs"] == again["outputs"]
    assert [(o["sha256"], o["boxes"]) for o in native["outputs"]] == [
        (o["sha256"], o["boxes"]) for o in observed["outputs"]
    ]
    assert all(len(o["source_draws"]) == 8 for o in observed["outputs"])
    assert any(len(set(o["source_draws"])) > 1 for o in observed["outputs"])
    assert any(o["filtered"] > 0 and o["clipped"] > 0 for o in observed["outputs"])
    assert not observed["runtime"]["cuda_initialized"]


def test_epoch_close_and_rect_use_native_transforms(tmp_path):
    spec = fixture_spec(tmp_path, epoch=9)
    report = native_preview(spec, tmp_path)
    assert report["mosaic_closed"]
    assert all(
        report["transform_parameters"][k] == 0
        for k in ("mosaic", "mixup", "cutmix", "copy_paste")
    )
    assert all(len(output["source_draws"]) == 1 for output in report["outputs"])
    assert report["outputs"][3]["boxes"] == []


def test_native_cutmix_and_multiscale_keep_provenance_and_exact_geometry(tmp_path):
    spec = fixture_spec(
        tmp_path,
        values=dict(
            epochs=10,
            batch=2,
            imgsz=128,
            mosaic=1.0,
            mixup=1.0,
            cutmix=1.0,
            multi_scale=0.5,
            close_mosaic=2,
        ),
    )
    report = native_preview(spec, tmp_path)
    baseline = native_preview(spec, tmp_path, traced=False)
    assert [(o["sha256"], o["boxes"]) for o in report["outputs"]] == [
        (o["sha256"], o["boxes"]) for o in baseline["outputs"]
    ]
    assert any(
        any(item["step"].startswith("cutmix") for item in o["operations"])
        for o in report["outputs"]
    )
    assert all(
        set(o["source_draws"]) <= set(o["sources_read"]) for o in report["outputs"]
    )


def test_corrupt_and_missing_frozen_inputs_fail_before_native_use(tmp_path):
    spec = fixture_spec(tmp_path)
    (tmp_path / spec["sources"][0]["label_path"]).write_text("")
    with pytest.raises(ValueError, match="frozen"):
        verify_pool(spec, tmp_path)
    (tmp_path / spec["sources"][0]["image_path"]).unlink()
    with pytest.raises(ValueError, match="Missing"):
        verify_pool(spec, tmp_path)


def test_applied_native_cutmix_preserves_exact_source_and_box_evidence(tmp_path):
    spec = fixture_spec(
        tmp_path,
        values=dict(
            epochs=10,
            batch=2,
            imgsz=128,
            mosaic=0.0,
            mixup=0.0,
            cutmix=1.0,
            translate=0.0,
            scale=0.0,
            fliplr=0.0,
        ),
    )
    for index, source in enumerate(spec["sources"]):
        x, y = ((0.15, 0.15), (0.85, 0.85), (0.85, 0.15), (0.15, 0.85))[index]
        labels = f"0 {x} {y} 0.15 0.15\n"
        source["labels"] = labels
        source["label_sha256"] = hashlib.sha256(labels.encode()).hexdigest()
        (tmp_path / source["label_path"]).write_text(labels)
    spec["seed"] = 1
    spec["input_digest"] = input_digest(spec)
    observed = native_preview(spec, tmp_path)
    baseline = native_preview(spec, tmp_path, traced=False)
    assert [(item["sha256"], item["boxes"]) for item in observed["outputs"]] == [
        (item["sha256"], item["boxes"]) for item in baseline["outputs"]
    ]
    applied = [
        item
        for item in observed["outputs"]
        if any(operation["step"] == "cutmix" for operation in item["operations"])
    ]
    assert applied and all(len(item["source_draws"]) == 2 for item in applied)
    assert any(len(set(item["source_draws"])) == 2 for item in applied)


def test_rect_disables_native_mixing_without_changing_frozen_values(tmp_path):
    spec = fixture_spec(
        tmp_path,
        values=dict(epochs=10, batch=2, imgsz=128, rect=True),
    )
    before = copy.deepcopy(spec)
    report = native_preview(spec, tmp_path)
    assert spec == before
    assert all(
        report["transform_parameters"][key] == 0
        for key in ("mosaic", "mixup", "cutmix")
    )
    assert all(len(item["source_draws"]) == 1 for item in report["outputs"])
