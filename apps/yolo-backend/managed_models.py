"""Authenticated, immutable installation of qualified platform-produced weights."""

from __future__ import annotations

import hashlib
import importlib
import json
import secrets
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from aap_backend_runtime.managed_models import ManagedModelStore as _ManagedModelStore
from aap_backend_runtime.backend_auth import backend_credential
from training_contract.model_return import ultralytics_identity
from training_contract.inference_runtime import inference_runtime
from training_contract.models import model_spec
from training_contract.native_architecture import (
    validate_architecture_yaml,
    matches_architecture_signature,
)
from managed_segmentation import validate_receipt as validate_segmentation_receipt


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def runtime_receipt(receipt):
    """Decorate a response without changing the immutable on-disk training receipt."""
    if receipt.get("framework") != "ultralytics":
        raise HTTPException(status_code=409, detail="Unsupported managed model framework")
    framework = importlib.import_module("ultralytics")
    try:
        runtime = inference_runtime(receipt, framework.__version__)
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail="Unqualified training/inference runtime combination"
        ) from exc
    return {
        **receipt,
        "inference_runtime": runtime,
    }


class ManagedModelRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_version_id: uuid.UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    classes_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    inference_runtime: dict | None = None


class InstallRequest(ManagedModelRef):
    classes: list[str] = Field(min_length=1, max_length=1000)
    framework: Literal["ultralytics"]
    framework_version: str = Field(min_length=1, max_length=50)
    task: Literal["detection", "segmentation"]
    size_bytes: int = Field(gt=0, le=2 * 1024**3)
    download_url: str = Field(max_length=10000)
    model_family: str | None = None
    model_size: str | None = None
    artifact_format: str | None = None
    artifact_size_bytes: int | None = None
    runtime_qualification: dict | None = None
    output_contract: str | None = None

    @model_validator(mode="after")
    def validate_identity(self):
        if self.inference_runtime is not None:
            raise ValueError(
                "Inference identity is not part of the immutable training artifact"
            )
        url = urlsplit(self.download_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.fragment
        ):
            raise ValueError("Expected a platform HTTP object URL")
        if digest(self.classes) != self.classes_digest or any(
            not name or len(name) > 120 for name in self.classes
        ):
            raise ValueError("Class identity mismatch")
        extra = (
            self.model_family,
            self.model_size,
            self.artifact_format,
            self.artifact_size_bytes,
            self.runtime_qualification,
        )
        if self.task == "segmentation":
            validate_segmentation_receipt(self.model_dump(mode="json"))
        elif self.output_contract is not None:
            raise ValueError("Unexpected output contract for detection install")
        elif self.framework_version == "8.4.183":
            validate_detection_receipt(self.model_dump(mode="json"))
        elif self.framework_version != "8.4.177" or any(
            value is not None for value in extra
        ):
            raise ValueError("Unexpected qualification fields for legacy install")
        return self


def authenticate(request: Request):
    expected = backend_credential("yolo")
    supplied = request.headers.get("Authorization", "")
    if len(expected) < 32 or not secrets.compare_digest(supplied, f"Bearer {expected}"):
        raise HTTPException(
            status_code=403, detail="Controlled model authentication failed"
        )




def validate_detection_receipt(receipt):
    expected = ultralytics_identity(
        f"{receipt.get('model_family')}{receipt.get('model_size')}",
        framework_version=receipt.get("framework_version"),
    )
    if (
        receipt.get("task") != "detection"
        or receipt.get("framework") != "ultralytics"
        or receipt.get("framework_version") != "8.4.183"
        or any(receipt.get(key) != value for key, value in expected.items())
        or receipt.get("artifact_format") is not None
        or receipt.get("artifact_size_bytes") is not None
        or receipt.get("output_contract") is not None
    ):
        raise ValueError("Unqualified detection receipt")


def verify_native_runtime(receipt):
    runtime = receipt["runtime_qualification"]
    for name in ("ultralytics", "torch", "torchvision"):
        if importlib.import_module(name).__version__ != runtime[name]:
            raise ValueError("Unqualified native inference runtime")
    for name, expected in runtime.get("source_sha256", {}).items():
        source = Path(importlib.import_module(name).__file__).read_bytes()
        if hashlib.sha256(source).hexdigest() != expected:
            raise ValueError("Native inference source identity changed")


def _reference_model(base_model, classes):
    import torch
    from ultralytics.nn.tasks import DetectionModel, SegmentationModel

    source_classes = None
    if isinstance(base_model, tuple):
        base_model, source_classes = base_model
    native_type = SegmentationModel if base_model.endswith("-seg") else DetectionModel
    with torch.random.fork_rng(devices=[]), torch.device("cpu"), torch.no_grad():
        model = native_type(base_model + ".yaml", nc=classes, verbose=False)
        if source_classes is not None:
            source = native_type(base_model + ".yaml", nc=source_classes, verbose=False)
            model.load(source, verbose=False)
        return model


def _validate_installed_receipt(receipt, path):
    if receipt.get("framework") != "ultralytics":
        raise ValueError("Unsupported managed model framework")
    if receipt.get("task") == "segmentation":
        validate_segmentation_receipt(receipt)
        if digest(receipt["classes"]) != receipt["classes_digest"]:
            raise ValueError("Installed segmentation classes differ")
    elif receipt.get("output_contract") is not None:
        raise ValueError("Unexpected detection output contract")
    elif receipt.get("framework") == "ultralytics" and receipt.get("framework_version") == "8.4.183":
        validate_detection_receipt(receipt)


class ManagedModelStore(_ManagedModelStore):
    def __init__(self, root: Path):
        super().__init__(root, inspect=self.inspect, validate_receipt=_validate_installed_receipt)

    @staticmethod
    def inspect(path, expected):
        if expected.get("framework") != "ultralytics":
            raise ValueError("Unsupported managed model framework")
        import ultralytics
        from ultralytics import YOLO

        runtime = inference_runtime(expected, ultralytics.__version__)
        qualified_native = ultralytics.__version__ == "8.4.183"
        model = YOLO(str(path))
        model.to("cpu")
        segmentation = expected["task"] == "segmentation"
        if model.task != ("segment" if segmentation else "detect") or not isinstance(
            model.names, dict
        ):
            raise ValueError("Expected a native task-matched checkpoint")
        if any(type(index) is not int for index in model.names) or set(
            model.names
        ) != set(range(len(model.names))):
            raise ValueError("Checkpoint names must have contiguous native indexes")
        names = [model.names[i] for i in range(len(model.names))]
        if names != expected["classes"] or digest(names) != expected["classes_digest"]:
            raise ValueError(
                "Checkpoint native classes differ from the registered order"
            )
        if segmentation or qualified_native:
            from ultralytics.nn.modules import head as native_heads
            from ultralytics.nn.tasks import yaml_model_load

            family, scale = expected.get("model_family"), expected.get("model_size")
            if expected["framework_version"] == "8.4.177" and not segmentation:
                family, scale = "yolo11", model.model.yaml.get("scale")
            base_model = f"{family}{scale}{'-seg' if segmentation else ''}"
            spec = model_spec("ultralytics", base_model)
            architecture = model.model.yaml
            head = model.model.model[-1]
            validate_architecture_yaml(
                architecture, spec, yaml_model_load(spec.yaml_filename)
            )
            if (
                type(head) is not getattr(native_heads, spec.head)
                or head.nc != len(names)
                or (getattr(head, "one2one_cv2", None) is not None) != spec.end2end
                or spec.end2end
                and head.reg_max != 1
                or not matches_architecture_signature(
                    model.model,
                    spec.id,
                    head.nc,
                    _reference_model,
                    ultralytics.__version__,
                )
            ):
                raise ValueError(
                    "Unqualified native model family, scale, head or inference branch"
                )
            if not segmentation:
                if expected["framework_version"] == "8.4.183":
                    validate_detection_receipt(expected)
                verify_native_runtime(
                    {"runtime_qualification": runtime["qualification"]}
                )
        if segmentation:
            from managed_segmentation import verify_runtime

            validate_segmentation_receipt(expected)
            verify_runtime(ultralytics.__version__)
            if (
                not names
                or len(set(names)) != len(names)
                or any(not isinstance(name, str) or not name for name in names)
            ):
                raise ValueError("Unqualified segmentation architecture or classes")
        if segmentation or qualified_native:
            import torch

            tensors = list(model.model.state_dict().values())
            if not tensors or any(
                not torch.isfinite(tensor).all().item() for tensor in tensors
            ):
                raise ValueError("Native checkpoint has missing or non-finite state")
        return model

