"""Strict native LibreYOLO detection for immutable trained checkpoints."""

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


def validate_pretrained_metadata(blob, size):
    """Validate the distinct official 1.1.1 checkpoint profile, not training receipts."""
    from libreyolo.utils.serialization import validate_checkpoint_metadata

    if (
        not isinstance(blob, dict)
        or set(blob) != {"model", "schema_version", "libreyolo_version", "model_family",
                         "size", "task", "nc", "imgsz", "names"}
        or blob["schema_version"] != "1.0"
        or blob["libreyolo_version"] != "1.1.1"
        or blob["model_family"] != "yolo9"
        or blob["size"] != size
        or size not in {"t", "s", "m", "c"}
        or blob["task"] != "detect"
        or type(blob["nc"]) is not int or blob["nc"] != 80
        or type(blob["imgsz"]) is not int or blob["imgsz"] != 640
    ):
        raise ValueError("Pretrained native metadata differs from verified profile")
    names = ordered_names(blob["names"], 80)
    validate_checkpoint_metadata(blob, strict=True)
    return names


def verify_loaded_tensors(source, loaded, *, pretrained_dfl_project=None):
    import torch

    if not isinstance(source, dict) or any(not isinstance(key, str) for key in source):
        raise ValueError("Expected a native tensor state dictionary")
    main = {
        key: value
        for key, value in source.items()
        if not key.startswith(("aux.", "aux_head."))
    }
    if pretrained_dfl_project is not None:
        # Official 1.1.1 checkpoints store DFL as a fixed convolution; 1.6
        # replaces it with a nonpersistent projection buffer and discards the
        # old key during loading. Verify that conversion against the live buffer.
        key = "head.dfl.conv.weight"
        tensor = main.pop(key, None)
        project = pretrained_dfl_project.detach().cpu()
        if (
            not isinstance(tensor, torch.Tensor)
            or not tensor.is_floating_point()
            or tensor.shape != (1, 16, 1, 1)
            or project.shape != (1, 1, 16, 1)
            or project.dtype != torch.float32
            or not torch.equal(project.flatten(), torch.arange(16, dtype=torch.float32))
            or not torch.equal(
                tensor.detach().cpu().flatten(), torch.arange(16, dtype=tensor.dtype)
            )
        ):
            raise ValueError("Native pretrained DFL projection differs from checkpoint")
    if set(main) != set(loaded):
        raise ValueError("Native main tensor keys differ; partial loading is forbidden")
    for key, tensor in source.items():
        if not isinstance(tensor, torch.Tensor) or (
            (tensor.is_floating_point() or tensor.is_complex())
            and not bool(torch.isfinite(tensor).all())
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
    """One native model owned by a serialized pool borrower."""

    def __init__(self, path, expected, *, device="cpu", pretrained_size=None):
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
        self._device = torch.device(device)
        if self._device.type not in {"cpu", "cuda"} or (
            self._device.type == "cpu" and self._device.index is not None
        ):
            raise ValueError("Unsupported native inference device")
        if self._device.type == "cuda":
            if self._device.index is None or not torch.cuda.is_available():
                raise ValueError("An explicit available CUDA device is required")
            if torch.version.cuda != "13.2":
                raise ValueError("Unqualified CUDA build")
            if self._device.index >= torch.cuda.device_count():
                raise ValueError("CUDA device is not visible")
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
        blob = torch.load(path, map_location="cpu", weights_only=True)
        if pretrained_size is None:
            validate_metadata(blob, expected)
            expected_names = expected["classes"]
        else:
            expected_names = validate_pretrained_metadata(blob, pretrained_size)
        self._native = LibreYOLO(str(path), device=str(self._device), task="detect")
        self._native.model.float()
        project = self._native.model.head.dfl.project if pretrained_size is not None else None
        verify_loaded_tensors(
            blob["model"], self._native.model.state_dict(), pretrained_dfl_project=project
        )
        names = ordered_names(self._native.names, len(expected_names))
        if names != expected_names:
            raise ValueError("Loaded native classes differ from checkpoint identity")
        self.names = dict(enumerate(names))
        self._imgsz = blob["imgsz"]
        self.task = "detect"
        self._verify_device()
        self.execution_facts = None
        self._forward_seen = False
        self._native.model.register_forward_pre_hook(self._check_forward_input)

    def _check_forward_input(self, _module, inputs):
        import torch

        if not inputs or not isinstance(inputs[0], torch.Tensor):
            raise ValueError("Native forward did not receive an image tensor")
        tensor = inputs[0]
        if tensor.device != self._device or tensor.dtype != torch.float32:
            raise ValueError("Native image tensor differs from the FP32 device profile")
        if torch.is_autocast_enabled(self._device.type):
            raise ValueError("Native forward used automatic mixed precision")
        self._verify_device()
        self._forward_seen = True

    def _verify_device(self):
        import torch

        for tensor in list(self._native.model.parameters()) + list(self._native.model.buffers()):
            if tensor.device != self._device:
                raise ValueError("Native model tensor is on the wrong device")
            if tensor.is_floating_point() and tensor.dtype != torch.float32:
                raise ValueError("Native model must use FP32")
        if self._device.type == "cuda" and (
            torch.backends.cuda.matmul.allow_tf32 or torch.backends.cudnn.allow_tf32
        ):
            raise ValueError("TF32 must remain disabled")

    @property
    def device(self):
        return self._native.device

    def to(self, device):
        if str(device) != str(self._device):
            raise ValueError("Native inference cannot change its qualified device")
        self._verify_device()
        return self

    def predict(self, image, *, conf, iou, max_det, classes, device, verbose=False):
        import torch

        if torch.device(device) != self._device:
            raise ValueError("Native prediction device differs from qualification")
        self._verify_device()
        self._forward_seen = False
        self.execution_facts = None
        try:
            with torch.inference_mode(), torch.autocast(device_type=self._device.type, enabled=False):
                result = self._native.predict(
                    image,
                    conf=conf,
                    iou=iou,
                    max_det=max_det,
                    classes=classes,
                    imgsz=self._imgsz,
                    device=str(self._device),
                    augment=False,
                    half=False,
                    save=False,
                    show=False,
                    stream=False,
                    batch=1,
                    tiling=False,
                    cuda_graph=False,
                )
        finally:
            if self._device.type == "cuda":
                torch.cuda.synchronize(self._device)
        self._verify_device()
        validate_result(result, image.size, self.names, classes, max_det)
        if not self._forward_seen:
            raise ValueError("Native prediction did not execute the verified model")
        self.execution_facts = {
            "precision": "fp32", "half": False, "amp": False,
            "tf32": False if self._device.type == "cuda" else None,
        }
        # Single-image LibreYOLO returns Results; [0] would drop all but one box.
        return [result]

    def close(self):
        import torch

        if self._device.type == "cuda":
            torch.cuda.synchronize(self._device)
        self._native = None
        if self._device.type == "cuda":
            torch.cuda.empty_cache()


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
