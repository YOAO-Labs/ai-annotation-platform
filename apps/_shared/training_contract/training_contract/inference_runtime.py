"""Explicit managed training-to-inference compatibility, independent of artifacts."""

from copy import deepcopy

from aap_task_contract import normalize_task

from .model_return import LIBRE_RUNTIME, detection_runtime, segmentation_runtime
from .models import LEGACY_ULTRALYTICS_VERSION, ULTRALYTICS_VERSION


def inference_runtime(receipt, version):
    framework = receipt.get("framework")
    trained = receipt.get("framework_version")
    task = normalize_task(receipt.get("task") or "detect")
    compatibility = "same-version"
    if framework == "libreyolo":
        if (
            trained != version
            or version != LIBRE_RUNTIME["libreyolo"]
            or task != "detect"
        ):
            raise ValueError("Unqualified LibreYOLO training/inference combination")
        qualification = deepcopy(LIBRE_RUNTIME)
    elif framework == "ultralytics":
        if trained not in {
            LEGACY_ULTRALYTICS_VERSION,
            ULTRALYTICS_VERSION,
        } or task not in {"detect", "segment"}:
            raise ValueError("Unqualified Ultralytics training identity")
        if trained != version:
            if (trained, version) != (LEGACY_ULTRALYTICS_VERSION, ULTRALYTICS_VERSION):
                raise ValueError(
                    "Unqualified Ultralytics training/inference combination"
                )
            if task == "detect" and (
                receipt.get("model_family") not in {None, "yolo11"}
                or receipt.get("model_size") not in {None, "n", "s", "m", "l", "x"}
            ):
                raise ValueError("Unqualified legacy detection architecture")
            if task == "segment" and (
                receipt.get("model_family"),
                receipt.get("model_size"),
            ) != ("yolo11", "n"):
                raise ValueError("Unqualified legacy segmentation architecture")
            compatibility = "ultralytics-yolo11-legacy-to-current"
        if task == "segment":
            qualification = segmentation_runtime(version)
        elif version == ULTRALYTICS_VERSION:
            qualification = detection_runtime(version)
        elif version == LEGACY_ULTRALYTICS_VERSION:
            qualification = {"ultralytics": version}
        else:
            raise ValueError("Unqualified Ultralytics inference version")
        if version == ULTRALYTICS_VERSION:
            end2end = receipt.get("model_family") == "yolo26"
            qualification.update(end2end=end2end, nms=False if end2end else None)
    else:
        raise ValueError("Unqualified inference framework")
    return {
        "revision": 1,
        "framework": framework,
        "training_version": trained,
        "version": version,
        "task": task,
        "compatibility": compatibility,
        "qualification": qualification,
    }


def validate_inference_runtime(receipt, runtime):
    if not isinstance(runtime, dict) or runtime != inference_runtime(
        receipt, runtime.get("version")
    ):
        raise ValueError("Inference runtime differs from its qualified identity")
    return runtime
