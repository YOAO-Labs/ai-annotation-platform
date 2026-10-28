"""Native checkpoint loading and geometry qualification in the pinned CPU image."""

from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
import torch

from libreyolo_native import (
    LibreDetectionModel,
    ordered_names,
    validate_metadata,
    validate_pretrained_metadata,
    validate_result,
    verify_loaded_tensors,
)
from training_contract.model_return import libre_identity


def expected(size="t", names=None):
    return {
        "framework": "libreyolo",
        "framework_version": "1.6.0",
        "classes": names or ["car", "person"],
        **libre_identity(f"LibreYOLO9{size}"),
    }


def metadata(size="t", names=None):
    return {
        "model": {"main.weight": torch.ones(2)},
        "schema_version": "1.0",
        "libreyolo_version": "1.6.0",
        "model_family": "yolo9",
        "size": size,
        "task": "detect",
        "nc": 2,
        "names": names or {0: "car", 1: "person"},
        "imgsz": 128,
        "letterbox_pad": "topleft",
    }


@pytest.mark.parametrize("field", list(metadata()))
def test_every_required_raw_metadata_field_is_checked_before_native_coercion(field):
    blob = metadata()
    del blob[field]
    if field == "letterbox_pad":
        validate_metadata(blob, expected())  # Native legacy default is top-left.
    else:
        with pytest.raises(ValueError):
            validate_metadata(blob, expected())


@pytest.mark.parametrize(
    "names",
    [
        {0: "car"},
        {"0": "car", "1": "person"},
        {False: "car", True: "person"},
        {0: "car", 2: "person"},
        ["car", 1],
        ["person", "car"],
    ],
)
def test_sparse_coerced_foreign_and_reordered_native_names_are_rejected(names):
    with pytest.raises(ValueError):
        validate_metadata({**metadata(), "names": names}, expected())


@pytest.mark.parametrize(
    "change",
    [
        {"size": "e"},
        {"model_family": "yolo26"},
        {"task": "segment"},
        {"schema_version": "0.9"},
        {"libreyolo_version": "1.5.0"},
        {"nc": True},
        {"imgsz": 129},
        {"letterbox_pad": "other"},
        {"quant": None},
        {"input_profile": {}},
        {"aap_resume": {}},
    ],
)
def test_foreign_unqualified_formats_fail_before_loading(change):
    with pytest.raises(ValueError):
        validate_metadata({**metadata(), **change}, expected())


def test_all_main_tensors_must_load_exactly_and_auxiliary_heads_remain_allowed():
    source = {"main.weight": torch.tensor([1.0, 2.0], dtype=torch.float16)}
    loaded = {"main.weight": source["main.weight"].float()}
    verify_loaded_tensors({**source, "aux.weight": torch.ones(3)}, loaded)
    for corrupted in (
        {},
        {**source, "unknown.weight": torch.ones(1)},
        {"main.weight": torch.ones(3)},
        {"main.weight": torch.tensor([1.0, 3.0])},
        {"main.weight": torch.tensor([float("nan"), 2.0])},
        {**source, "aux.weight": "foreign"},
    ):
        with pytest.raises(ValueError):
            verify_loaded_tensors(corrupted, loaded)
    with pytest.raises(ValueError):
        verify_loaded_tensors(
            {"count": torch.ones(1, dtype=torch.int64)}, {"count": torch.ones(1)}
        )


def result(xyxy=None, scores=None, indexes=None):
    class Boxes(SimpleNamespace):
        def __len__(self):
            return len(self.xyxy)

    return SimpleNamespace(
        orig_shape=(120, 200),
        names={0: "car", 1: "person"},
        boxes=Boxes(
            xyxy=np.asarray(
                xyxy if xyxy is not None else [[20, 12, 60, 36], [80, 48, 180, 108]],
                dtype=float,
            ),
            conf=np.asarray(scores if scores is not None else [0.7, 0.8]),
            cls=np.asarray(indexes if indexes is not None else [0, 1]),
        ),
    )


def test_all_detections_are_normalized_once_with_original_pixel_dimensions():
    from predictor import _emit_detection

    raw = result()
    validate_result(raw, (200, 120), raw.names, None, 10)
    items = _emit_detection(raw, raw.names, 200, 120)
    assert len(items) == 2
    assert items[0]["value"] == {
        "x": 10.0,
        "y": 10.0,
        "width": 20.0,
        "height": 20.0,
        "rectanglelabels": ["car"],
    }
    assert items[1]["native_class_index"] == 1
    empty = result([], [], [])
    empty.boxes.xyxy = np.empty((0, 4))
    validate_result(empty, (200, 120), raw.names, None, 10)


@pytest.mark.parametrize(
    "fault",
    [
        "dimensions",
        "names",
        "fractional_class",
        "class_range",
        "score",
        "nan",
        "geometry",
        "length",
        "max_det",
        "filter",
    ],
)
def test_native_result_contract_rejects_invalid_output(fault):
    raw = result()
    classes, max_det = None, 10
    if fault == "dimensions":
        raw.orig_shape = (200, 120)
    elif fault == "names":
        raw.names = {0: "person", 1: "car"}
    elif fault == "fractional_class":
        raw.boxes.cls = np.array([0.5, 1.0])
    elif fault == "class_range":
        raw.boxes.cls = np.array([0, 2])
    elif fault == "score":
        raw.boxes.conf = np.array([1.1, 0.8])
    elif fault == "nan":
        raw.boxes.xyxy[0, 0] = np.nan
    elif fault == "geometry":
        raw.boxes.xyxy[0, 2] = 201
    elif fault == "length":
        raw.boxes.conf = np.array([0.8])
    elif fault == "max_det":
        max_det = 1
    else:
        classes = [0]
    with pytest.raises(ValueError):
        validate_result(raw, (200, 120), {0: "car", 1: "person"}, classes, max_det)


@pytest.mark.parametrize("size", list("tsmc"))
def test_native_each_size_reload_non_square_output_and_class_order(tmp_path, size):
    from libreyolo.models.yolo9.model import LibreYOLO9

    torch.set_num_threads(2)
    names = ["person", "car"] if size in {"s", "c"} else ["car", "person"]
    native = LibreYOLO9(None, size, nb_classes=2, device="cpu", task="detect")
    blob = metadata(size, names)
    blob["names"] = dict(enumerate(names))
    blob["model"] = native.model.state_dict()
    path = tmp_path / "native.pt"
    torch.save(blob, path)
    del native, blob
    model = LibreDetectionModel(path, expected(size, names))
    assert ordered_names(model.names, 2) == names
    output = model.predict(
        Image.new("RGB", (200, 120)),
        conf=0.0,
        iou=0.7,
        max_det=7,
        classes=None,
        device="cpu",
    )
    assert len(output) == 1 and len(output[0].boxes) == 7
    with pytest.raises(ValueError, match="CPU only"):
        model.to("cuda:0")
    assert str(model.device) == "cpu"
    # A fresh native load must retain the same exact classes and weights.
    second = LibreDetectionModel(path, expected(size, names))
    assert second.names == model.names
    if size == "t":
        partial = torch.load(path, weights_only=True, map_location="cpu")
        del partial["model"][next(iter(partial["model"]))]
        torch.save(partial, path)
        with pytest.raises(ValueError, match="main tensor keys"):
            LibreDetectionModel(path, expected(size, names))


def test_native_runtime_and_partial_checkpoint_are_rejected(tmp_path, monkeypatch):
    identity = deepcopy(expected())
    identity["runtime_qualification"]["revision"] = 99
    with pytest.raises(ValueError, match="runtime"):
        LibreDetectionModel(tmp_path / "missing.pt", identity)
    monkeypatch.setattr("libreyolo_native.version", lambda _: "foreign")
    with pytest.raises(ValueError, match="runtime"):
        LibreDetectionModel(tmp_path / "missing.pt", expected())


def test_device_guard_rejects_wrong_dtype_and_device():
    model = LibreDetectionModel.__new__(LibreDetectionModel)
    model._device = torch.device("cpu")
    model._native = SimpleNamespace(model=torch.nn.Linear(2, 2))
    model._verify_device()
    model._native.model.half()
    with pytest.raises(ValueError, match="FP32"):
        model._verify_device()
    model._native.model.float()
    model._device = torch.device("cuda:0")
    with pytest.raises(ValueError, match="wrong device"):
        model._verify_device()


def test_qualified_model_cannot_switch_devices():
    model = LibreDetectionModel.__new__(LibreDetectionModel)
    model._device = torch.device("cpu")
    model._native = SimpleNamespace(model=torch.nn.Linear(2, 2))
    assert model.to("cpu") is model
    with pytest.raises(ValueError, match="cannot change"):
        model.to("cuda:0")


def test_pretrained_identity_is_separate_from_trained_checkpoint_contract():
    names = [f"class-{index}" for index in range(80)]
    blob = {"model": {"main.weight": torch.ones(2)}, "schema_version": "1.0",
            "libreyolo_version": "1.1.1", "model_family": "yolo9", "size": "t",
            "task": "detect", "nc": 80, "imgsz": 640, "names": dict(enumerate(names))}
    assert validate_pretrained_metadata(blob, "t") == names
    with pytest.raises(ValueError):
        validate_metadata(blob, expected("t", names))
    with pytest.raises(ValueError):
        validate_pretrained_metadata(blob, "s")
    with pytest.raises(ValueError):
        validate_pretrained_metadata({**blob, "libreyolo_version": "1.6.0"}, "t")


def test_forward_guard_checks_actual_image_tensor_and_autocast():
    model = LibreDetectionModel.__new__(LibreDetectionModel)
    model._device = torch.device("cpu")
    model._native = SimpleNamespace(model=torch.nn.Linear(2, 2))
    model._forward_seen = False
    model._check_forward_input(None, (torch.ones(1, 2),))
    assert model._forward_seen
    with pytest.raises(ValueError, match="image tensor"):
        model._check_forward_input(None, (torch.ones(1, 2, dtype=torch.float16),))
    with torch.autocast("cpu"):
        with pytest.raises(ValueError, match="mixed precision"):
            model._check_forward_input(None, (torch.ones(1, 2),))
