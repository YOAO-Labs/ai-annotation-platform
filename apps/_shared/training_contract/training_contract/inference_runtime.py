"""Explicit managed training-to-inference compatibility, independent of artifacts."""

from copy import deepcopy
from types import MappingProxyType

from aap_task_contract import normalize_task

from .model_return import LIBRE_RUNTIME, detection_runtime, segmentation_runtime
from .models import LEGACY_ULTRALYTICS_VERSION, ULTRALYTICS_VERSION

# Execution qualification only: do not use this as an artifact identity.
LIBRE_CUDA_RUNTIME = MappingProxyType({
    "revision": 1,
    "libreyolo": "1.6.0",
    "torch": "2.14.1+cu132",
    "torchvision": "0.29.1+cu132",
    "device": "cuda",
    "cuda_build": "13.2",
    "precision": "fp32",
    "half": False,
    "amp": False,
    "tf32": False,
})


def inference_runtime(receipt, version, *, device="cpu"):
    if device not in ("cpu", "cuda"):
        raise ValueError("Unqualified inference device kind")
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
        qualification = (
            deepcopy(LIBRE_RUNTIME) if device == "cpu" else dict(LIBRE_CUDA_RUNTIME)
        )
    elif framework == "ultralytics":
        if device != "cpu":
            raise ValueError("Unqualified Ultralytics inference device profile")
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
    if not isinstance(runtime, dict):
        raise ValueError("Inference runtime differs from its qualified identity")
    device = "cpu"
    if receipt.get("framework") == "libreyolo":
        qualification = runtime.get("qualification")
        if not isinstance(qualification, dict):
            raise ValueError("Inference runtime lacks a qualified device")
        device = qualification.get("device")
    expected = inference_runtime(receipt, runtime.get("version"), device=device)
    if runtime != expected or (
        device == "cuda"
        and (
            any(type(runtime[key]) is not type(value) for key, value in expected.items())
            or any(
                type(runtime["qualification"][key]) is not type(value)
                for key, value in expected["qualification"].items()
            )
        )
    ):
        raise ValueError("Inference runtime differs from its qualified identity")
    return runtime
