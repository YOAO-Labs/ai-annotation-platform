"""Native detection provider; authorization and pooling belong to the SDK."""
from __future__ import annotations

import hashlib
import json

from libreyolo_native import LibreDetectionModel
from aap_inference_plugin import Detection
from training_contract.inference_runtime import inference_runtime
from training_contract.model_return import libre_identity


def validate_receipt(receipt):
    size = receipt.get("model_size")
    if size not in {"t", "s", "m", "c"}:
        raise ValueError("Unsupported LibreYOLO model size")
    identity = libre_identity(f"LibreYOLO9{size}")
    if (receipt.get("framework") != "libreyolo"
        or receipt.get("framework_version") != "1.6.0"
        or receipt.get("task") != "detection"
        or ("size_bytes" in receipt and receipt.get("artifact_size_bytes") != receipt["size_bytes"])
        or any(receipt.get(key) != value for key, value in identity.items())):
        raise ValueError("Unqualified LibreYOLO receipt")


    classes = receipt.get("classes")
    if (not isinstance(classes, list) or not classes
        or any(not isinstance(name, str) or not name or len(name) > 120 for name in classes)):
        raise ValueError("Invalid native classes")
    digest = hashlib.sha256(json.dumps(
        classes, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    if digest != receipt.get("classes_digest"):
        raise ValueError("Class identity mismatch")

class Model:
    def __init__(self, native):
        self.native = native
        self.classes = list(native.names.values())

    def predict(self, image, parameters):
        result = self.native.predict(
            image, conf=parameters.get("conf", 0.25), iou=parameters.get("iou", 0.45),
            max_det=parameters.get("max_det", 300), classes=parameters.get("classes"),
            device=str(self.native._device),
        )[0]
        boxes = result.boxes
        def values(value):
            if hasattr(value, "detach"):
                value = value.detach().cpu()
            return value.tolist()
        xyxy = values(boxes.xyxy)
        scores = values(boxes.conf)
        indexes = values(boxes.cls)
        return [Detection(*box, int(index), float(score))
                for box, index, score in zip(xyxy, indexes, scores, strict=True)]

    def close(self):
        self.native.close()


class Provider:
    def load(self, model_id, device, config):
        if not model_id.startswith("trained:"):
            from .assets import assets, verify

            item = assets().get(model_id)
            if item is None:
                raise ValueError("Unsupported pretrained model size")
            path = verify(config["path"], item)
            expected = {"framework": "libreyolo", "framework_version": "1.6.0",
                        **libre_identity(f"LibreYOLO9{model_id}")}
            return Model(LibreDetectionModel(path, expected, device=device, pretrained_size=model_id))
        receipt = config["receipt"]
        validate_receipt(receipt)
        if model_id != f"trained:{receipt['model_version_id']}":
            raise ValueError("Trained model identity differs from receipt")
        frozen = config["frozen_model_ref"]
        for field in ("model_version_id", "sha256", "classes_digest"):
            if frozen.get(field) != receipt.get(field):
                raise ValueError(f"Frozen {field} differs from receipt")
        runtime = inference_runtime(receipt, "1.6.0", device="cuda" if device.startswith("cuda:") else "cpu")
        frozen_runtime = frozen.get("inference_runtime")
        if frozen_runtime != runtime and not (device == "cpu" and frozen_runtime is None):
            raise ValueError("Frozen inference runtime differs from deployment")
        return Model(LibreDetectionModel(config["path"], receipt, device=device))
