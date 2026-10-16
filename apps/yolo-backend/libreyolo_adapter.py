"""Strict native LibreYOLO CPU detection behind the existing managed pool."""

from __future__ import annotations

from importlib.metadata import version

import numpy as np
from training_contract.model_return import LIBRE_FORMAT, LIBRE_RUNTIME


def ordered_names(names, count):
    if isinstance(names, dict):
        if any(type(key) is not int for key in names) or set(names) != set(
            range(count)
        ):
            raise ValueError(
                "Native class indexes must be complete consecutive integers"
            )
        names = [names[index] for index in range(count)]
    if (
        not isinstance(names, list)
        or len(names) != count
        or any(not isinstance(name, str) or not name for name in names)
    ):
        raise ValueError("Native class names must be complete strings")
    return names


def validate_metadata(blob, expected):
    from libreyolo.utils.serialization import validate_checkpoint_metadata

    # Native validation normalizes sparse names, so validate the original first.
    if not isinstance(blob, dict) or any(
        key not in blob
        for key in (
            "model",
            "schema_version",
            "libreyolo_version",
            "model_family",
            "size",
            "task",
            "nc",
            "names",
            "imgsz",
        )
    ):
        raise ValueError("Incomplete native checkpoint metadata")
    count = len(expected["classes"])
    if (
        blob["schema_version"] != "1.0"
        or blob["libreyolo_version"] != "1.6.0"
        or blob["model_family"] != "yolo9"
        or blob["size"] not in {"t", "s", "m", "c"}
        or blob["task"] != "detect"
        or type(blob["nc"]) is not int
        or blob["nc"] != count
        or ordered_names(blob["names"], count) != expected["classes"]
        or type(blob["imgsz"]) is not int
        or not 128 <= blob["imgsz"] <= 1280
        or blob["imgsz"] % 32
        or blob.get("letterbox_pad", "topleft") not in {"center", "topleft"}
        or any(
            key in blob
            for key in ("aap_resume", "quant", "quantization", "input_profile")
        )
        or expected.get("artifact_format") != LIBRE_FORMAT
        or expected.get("model_family") != blob["model_family"]
        or expected.get("model_size") != blob["size"]
    ):
        raise ValueError("Native checkpoint task/classes/format identity differs")
    validate_checkpoint_metadata(blob, strict=True)


def verify_loaded_tensors(source, loaded):
    import torch

    if not isinstance(source, dict) or any(not isinstance(key, str) for key in source):
        raise ValueError("Expected a native tensor state dictionary")
    main = {
        key: value
        for key, value in source.items()
        if not key.startswith(("aux.", "aux_head."))
    }
    if set(main) != set(loaded):
        raise ValueError("Native main tensor keys differ; partial loading is forbidden")
    for key, tensor in source.items():
        if not isinstance(tensor, torch.Tensor) or (
            tensor.is_floating_point() and not bool(torch.isfinite(tensor).all())
        ):
            raise ValueError("Native checkpoint contains invalid tensor values")
        if key not in main:
            continue
        actual = loaded[key].detach().cpu()
        if (
            tensor.shape != actual.shape
            or (not tensor.is_floating_point() and tensor.dtype != actual.dtype)
            or not torch.equal(tensor.to(dtype=actual.dtype, device="cpu"), actual)
        ):
            raise ValueError("Native main tensor was not loaded exactly")


class LibreDetectionModel:
    """One native model owned by a serialized pool borrower; CPU is qualified."""

    def __init__(self, path, expected):
        import torch
        from libreyolo import LibreYOLO

        actual = {name: version(name) for name in ("libreyolo", "torch", "torchvision")}
        if (
            expected.get("framework") != "libreyolo"
            or expected.get("framework_version") != "1.6.0"
            or expected.get("runtime_qualification") != LIBRE_RUNTIME
            or any(actual[name] != LIBRE_RUNTIME[name] for name in actual)
        ):
            raise ValueError("Unqualified native inference runtime")
        blob = torch.load(path, map_location="cpu", weights_only=True)
        validate_metadata(blob, expected)
        self._native = LibreYOLO(str(path), device="cpu", task="detect")
        verify_loaded_tensors(blob["model"], self._native.model.state_dict())
        names = ordered_names(self._native.names, len(expected["classes"]))
        if names != expected["classes"]:
            raise ValueError("Loaded native classes differ from checkpoint identity")
        self.names = dict(enumerate(names))
        self._imgsz = blob["imgsz"]
        self.task = "detect"

    @property
    def device(self):
        return self._native.device

    def to(self, device):
        if str(device) != "cpu":
            raise ValueError("LibreYOLO managed inference is qualified on CPU only")
        self._native._runner._set_device("cpu")
        return self

    def predict(self, image, *, conf, iou, max_det, classes, device, verbose=False):
        if str(device) != "cpu" or str(self.device) != "cpu":
            raise ValueError("Native prediction device differs from CPU qualification")
        result = self._native.predict(
            image,
            conf=conf,
            iou=iou,
            max_det=max_det,
            classes=classes,
            imgsz=self._imgsz,
            device="cpu",
            augment=False,
            save=False,
            show=False,
            stream=False,
            batch=1,
            tiling=False,
            cuda_graph=False,
        )
        validate_result(result, image.size, self.names, classes, max_det)
        # Single-image LibreYOLO returns Results; [0] would drop all but one box.
        return [result]


def validate_result(result, dimensions, names, classes, max_det):
    width, height = dimensions
    if tuple(result.orig_shape) != (height, width) or ordered_names(
        result.names, len(names)
    ) != list(names.values()):
        raise ValueError("Native result dimensions/classes differ from input identity")
    boxes = result.boxes
    if boxes is None:
        raise ValueError("Native detection result is missing boxes")

    def array(value):
        return np.asarray(value.detach().cpu() if hasattr(value, "detach") else value)

    xyxy, scores, indexes = array(boxes.xyxy), array(boxes.conf), array(boxes.cls)
    count = len(xyxy)
    if (
        xyxy.shape != (count, 4)
        or scores.shape != (count,)
        or indexes.shape != (count,)
        or count > max_det
        or not all(np.isfinite(value).all() for value in (xyxy, scores, indexes))
        or np.any(scores < 0)
        or np.any(scores > 1)
        or np.any(indexes != np.floor(indexes))
        or np.any(indexes < 0)
        or np.any(indexes >= len(names))
        or classes
        and not np.isin(indexes, classes).all()
        or np.any(xyxy[:, 0] < 0)
        or np.any(xyxy[:, 1] < 0)
        or np.any(xyxy[:, 2] > width)
        or np.any(xyxy[:, 3] > height)
        or np.any(xyxy[:, 2] <= xyxy[:, 0])
        or np.any(xyxy[:, 3] <= xyxy[:, 1])
    ):
        raise ValueError("Native detection geometry/class/confidence contract differs")
