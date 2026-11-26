"""Shared trainer-plugin DB fixtures and model-return/runtime test builders."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any

from training_contract.dataset import (
    DETECTION,
    SEGMENTATION,
    SEGMENTATION_OUTPUT_CONTRACT,
)
from training_contract.trainer_plugin import (
    TRAINER_PROTOCOL_VERSION,
    ServingCompatibilityV1,
    TrainArtifactV1,
    TrainerArchitectureV1,
    TrainerCapabilitiesV1,
    TrainReceiptV1,
    canonical_json_digest,
)
from yoao_task_contract import normalize_task

from app.schemas.trainer_plugin import (
    RegisterTrainerPluginRequest,
    TrainerPluginRecord,
)
from app.schemas.training_run import ExecutorClaim
from app.services.trainer_plugins import (
    entrypoint_digest,
    validate_registration_payload,
)
from app.services.trainer_plugins_data import TrainerPluginsPostgresData

ULTRALYTICS_VERSION = "8.4.183"
LEGACY_ULTRALYTICS_VERSION = "8.4.177"
LIBRE_VERSION = "1.6.0"

LIBRE_FORMAT = "libreyolo-native-state-dict-1.0"
LIBRE_RUNTIME: dict[str, Any] = {
    "revision": 1,
    "libreyolo": LIBRE_VERSION,
    "torch": "2.14.1+cu132",
    "torchvision": "0.29.1+cu132",
    "device": "cpu",
}
LIBRE_CUDA_RUNTIME = MappingProxyType(
    {
        "revision": 1,
        "libreyolo": LIBRE_VERSION,
        "torch": "2.14.1+cu132",
        "torchvision": "0.29.1+cu132",
        "device": "cuda",
        "cuda_build": "13.2",
        "precision": "fp32",
        "half": False,
        "amp": False,
        "tf32": False,
    }
)

SEGMENTATION_RUNTIME: dict[str, Any] = {
    "revision": 1,
    "ultralytics": LEGACY_ULTRALYTICS_VERSION,
    "torch": "2.14.1+cu132",
    "torchvision": "0.29.1+cu132",
    "retina_masks": True,
    "mask_processing": "ultralytics.utils.ops.process_mask_native",
    "mask_threshold": 0.0,
    "decoder": "aap-coco-rle-column-major-1",
    "source_sha256": {
        "ultralytics.models.yolo.segment.predict": "2f80c259782f330b12b3f4b5eb11345416b83e99d6af86da3bbe594089bde01c",
        "ultralytics.utils.ops": "c66d0eff4c6da3ba53247bc235de84bbe6db6682829ecaecd333c136070441cb",
    },
}

INIT_POLICY: dict[str, Any] = {
    "schema": 1,
    "scope": "retained-model-weights-only-initialization",
    "framework": "ultralytics",
    "framework_version": ULTRALYTICS_VERSION,
    "task": "detect",
    "torch": "2.14.1",
    "torchvision": "0.29.1",
    "cuda": "13.2",
}
SEGMENTATION_INIT_POLICY: dict[str, Any] = {
    **INIT_POLICY,
    "task": "segment",
}
POLICY_DIGEST = canonical_json_digest(INIT_POLICY)
SEGMENTATION_POLICY_DIGEST = canonical_json_digest(SEGMENTATION_INIT_POLICY)


def _parse_ultralytics_model(base_model: str) -> tuple[str, str, str, bool]:
    match = re.fullmatch(
        r"(yolov8|yolo11|yolo26)([nsmlx])(-seg)?",
        str(base_model or "").strip().lower(),
    )
    if match is None:
        raise ValueError(f"Unsupported Ultralytics test model: {base_model!r}")
    family, scale, seg_suffix = match.groups()
    task = SEGMENTATION if seg_suffix else DETECTION
    return family, scale, task, family == "yolo26"


def segmentation_runtime(framework_version: str) -> dict[str, Any]:
    if framework_version == LEGACY_ULTRALYTICS_VERSION:
        return deepcopy(SEGMENTATION_RUNTIME)
    if framework_version != ULTRALYTICS_VERSION:
        raise ValueError("Unsupported segmentation framework version")
    return {
        **deepcopy(SEGMENTATION_RUNTIME),
        "revision": 2,
        "ultralytics": ULTRALYTICS_VERSION,
        "source_sha256": {
            "ultralytics.models.yolo.segment.predict": "66371a017252aa2e8606ba13dbc6c76307088d69159a5f46970138a9412846c5",
            "ultralytics.utils.ops": "7b15cd980abbd578a8be91661c2399b2cbdea0207f1e61b0fb74cfac659bed77",
        },
    }


def detection_runtime(framework_version: str) -> dict[str, Any]:
    if framework_version != ULTRALYTICS_VERSION:
        raise ValueError("Unsupported detection framework version")
    return {
        "revision": 2,
        "ultralytics": ULTRALYTICS_VERSION,
        "torch": "2.14.1+cu132",
        "torchvision": "0.29.1+cu132",
        "source_sha256": {
            "ultralytics.utils.ops": "7b15cd980abbd578a8be91661c2399b2cbdea0207f1e61b0fb74cfac659bed77",
        },
    }


def segmentation_identity(
    base_model: str, framework_version: str = LEGACY_ULTRALYTICS_VERSION
) -> dict[str, Any]:
    family, scale, task, end2end = _parse_ultralytics_model(base_model)
    if task != SEGMENTATION:
        raise ValueError("Expected segmentation model")
    runtime = segmentation_runtime(framework_version)
    if framework_version == ULTRALYTICS_VERSION:
        runtime.update(end2end=end2end, nms=False if end2end else None)
    return {
        "model_family": family,
        "model_size": scale,
        "output_contract": SEGMENTATION_OUTPUT_CONTRACT,
        "runtime_qualification": runtime,
    }


def ultralytics_identity(
    base_model: str, framework_version: str = ULTRALYTICS_VERSION
) -> dict[str, Any] | None:
    family, scale, task, end2end = _parse_ultralytics_model(base_model)
    if task == SEGMENTATION:
        return segmentation_identity(base_model, framework_version)
    if framework_version == LEGACY_ULTRALYTICS_VERSION:
        return None
    runtime = detection_runtime(framework_version)
    runtime.update(end2end=end2end, nms=False if end2end else None)
    return {
        "model_family": family,
        "model_size": scale,
        "runtime_qualification": runtime,
    }


def libre_identity(base_model: str) -> dict[str, Any]:
    match = re.fullmatch(r"libreyolo9([tsmc])", str(base_model or "").strip().lower())
    if match is None:
        raise ValueError("Unsupported LibreYOLO test model")
    return {
        "model_family": "yolo9",
        "model_size": match.group(1),
        "artifact_format": LIBRE_FORMAT,
        "runtime_qualification": deepcopy(LIBRE_RUNTIME),
    }


def inference_runtime(
    receipt: dict[str, Any], version: str, *, device: str = "cpu"
) -> dict[str, Any]:
    if device not in ("cpu", "cuda"):
        raise ValueError("Unqualified inference device kind")
    framework = receipt.get("framework")
    trained = receipt.get("framework_version")
    task = normalize_task(receipt.get("task") or DETECTION)
    compatibility = "same-version"
    if framework == "libreyolo":
        if trained != version or version != LIBRE_VERSION or task != DETECTION:
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
        } or task not in {DETECTION, SEGMENTATION}:
            raise ValueError("Unqualified Ultralytics training identity")
        if trained != version:
            if (trained, version) != (LEGACY_ULTRALYTICS_VERSION, ULTRALYTICS_VERSION):
                raise ValueError(
                    "Unqualified Ultralytics training/inference combination"
                )
            compatibility = "ultralytics-yolo11-legacy-to-current"
        if task == SEGMENTATION:
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


def _ultralytics_fields(*, family: str, task: str) -> list[dict[str, Any]]:
    segmentation = task == SEGMENTATION
    fields: list[dict[str, Any]] = [
        {
            "key": "epochs",
            "label": "Epochs",
            "group": "basic",
            "status": "editable",
            "native_type": "int",
            "default": 50,
            "native_default": 100,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Controlled training epochs",
            "rules": {"type": "integer", "minimum": 1, "maximum": 1000},
            "aliases": [],
        },
        {
            "key": "batch",
            "label": "Batch",
            "group": "basic",
            "status": "editable",
            "native_type": "int",
            "default": 1 if (family == "yolo26" and segmentation) else 8,
            "native_default": 16,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Controlled batch size",
            "rules": {"type": "integer", "minimum": 1, "maximum": 128},
            "aliases": [],
        },
        {
            "key": "imgsz",
            "label": "Image Size",
            "group": "basic",
            "status": "editable",
            "native_type": "int",
            "default": 640,
            "native_default": 640,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Controlled input resolution",
            "rules": {
                "type": "integer",
                "minimum": 128,
                "maximum": 1280,
                "multipleOf": 32,
            },
            "aliases": [],
        },
        {
            "key": "seed",
            "label": "Seed",
            "group": "performance",
            "status": "editable",
            "native_type": "int",
            "default": 42,
            "native_default": 0,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Random seed",
            "rules": {"type": "integer", "minimum": 0, "maximum": 2147483647},
            "aliases": [],
        },
        {
            "key": "patience",
            "label": "Patience",
            "group": "basic",
            "status": "editable",
            "native_type": "int",
            "default": 50,
            "native_default": 100,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Early stopping patience",
            "rules": {"type": "integer", "minimum": 0, "maximum": 1000},
            "aliases": [],
        },
        {
            "key": "lr0",
            "label": "Initial LR",
            "group": "optimizer",
            "status": "editable",
            "native_type": "float",
            "default": 0.01,
            "native_default": 0.01,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Initial learning rate",
            "rules": {"type": "number", "exclusiveMinimum": 0, "maximum": 1.0},
            "aliases": [],
        },
        {
            "key": "optimizer",
            "label": "Optimizer",
            "group": "optimizer",
            "status": "editable",
            "native_type": "str",
            "default": "SGD",
            "native_default": "auto",
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Optimizer choice",
            "rules": {"enum": ["SGD", "Adam", "AdamW", "RMSProp"]},
            "aliases": [],
        },
        {
            "key": "mosaic",
            "label": "Mosaic",
            "group": "augmentation",
            "status": "editable",
            "native_type": "float",
            "default": 1.0,
            "native_default": 1.0,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Mosaic probability",
            "rules": {"type": "number", "minimum": 0, "maximum": 1},
            "aliases": [],
        },
        {
            "key": "mixup",
            "label": "MixUp",
            "group": "augmentation",
            "status": "editable",
            "native_type": "float",
            "default": 0.0,
            "native_default": 0.0,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "MixUp probability",
            "rules": {"type": "number", "minimum": 0, "maximum": 1},
            "aliases": [],
        },
        {
            "key": "close_mosaic",
            "label": "Close Mosaic",
            "group": "augmentation",
            "status": "editable",
            "native_type": "int",
            "default": 10,
            "native_default": 10,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Disable mosaic in final epochs",
            "rules": {"type": "integer", "minimum": 0, "maximum": 1000},
            "aliases": [],
        },
        {
            "key": "pretrained",
            "label": "Pretrained",
            "group": "finetuning",
            "status": "editable",
            "native_type": "bool",
            "default": True,
            "native_default": True,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Initialize from base weights",
            "rules": {"type": "boolean"},
            "aliases": [],
        },
        {
            "key": "evaluate_train",
            "label": "Evaluate Train Split",
            "group": "evaluation",
            "status": "editable",
            "native_type": "bool",
            "default": False,
            "native_default": None,
            "defined_in": "platform",
            "reason": "Evaluate train split each epoch",
            "rules": {"type": "boolean"},
            "aliases": [],
        },
        {
            "key": "dfl",
            "label": "DFL Gain",
            "group": "loss",
            "status": "inapplicable" if family == "yolo26" else "editable",
            "native_type": "float",
            "default": 1.5,
            "native_default": 1.5,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "DFL loss gain",
            "rules": {}
            if family == "yolo26"
            else {"type": "number", "minimum": 0, "maximum": 10},
            "aliases": [],
        },
        {
            "key": "rect",
            "label": "Rectangular",
            "group": "platform",
            "status": "platform-managed" if segmentation else "editable",
            "native_type": "bool",
            "default": False,
            "native_default": False,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Fixed false for segmentation",
            "rules": {} if segmentation else {"type": "boolean"},
            "aliases": [],
        },
        {
            "key": "nms",
            "label": "NMS",
            "group": "platform",
            "status": "platform-managed",
            "native_type": "bool",
            "default": False,
            "native_default": False,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Managed NMS flag",
            "rules": {},
            "aliases": [],
        },
        {
            "key": "workers",
            "label": "Workers",
            "group": "platform",
            "status": "platform-managed",
            "native_type": "int",
            "default": 0 if segmentation else 2,
            "native_default": 8,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Managed data loader workers",
            "rules": {},
            "aliases": [],
        },
        {
            "key": "device",
            "label": "Device",
            "group": "platform",
            "status": "platform-managed",
            "native_type": "str",
            "default": "0",
            "native_default": None,
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Managed GPU device",
            "rules": {},
            "aliases": [],
        },
        {
            "key": "task",
            "label": "Task",
            "group": "platform",
            "status": "platform-managed",
            "native_type": "str",
            "default": task,
            "native_default": "detect",
            "defined_in": "ultralytics/cfg/default.yaml",
            "reason": "Managed task",
            "rules": {},
            "aliases": [],
        },
    ]
    return fields


def ultralytics_plugin_manifest(
    *,
    plugin_id: str = "yoao.trainer.ultralytics",
    version: str = "1.0.0",
    framework_version: str = ULTRALYTICS_VERSION,
    requires_gpu: bool = True,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "manifest_schema": "yoao-extension-manifest/v1",
        "extension_point": "trainer",
        "plugin_id": plugin_id,
        "version": version,
        "display_name": "Ultralytics YOLO Trainer",
        "description": "Test fixture Ultralytics trainer plugin.",
        "homepage": "https://example.com/trainer-ultralytics",
        "publisher": "YOAO Official",
        "license": "AGPL-3.0-only",
        "protocol_version": TRAINER_PROTOCOL_VERSION,
        "entrypoint": ["yoao-trainer-ultralytics"],
        "capabilities": capabilities
        or ["train", "evaluate", "preview", "export_weights"],
        "resource": {"memory_mb": 8192},
        "extension": {
            "framework_id": "ultralytics",
            "framework_version": framework_version,
            "requires_gpu": requires_gpu,
        },
    }


def ultralytics_plugin_capabilities(
    *,
    framework_version: str = ULTRALYTICS_VERSION,
    base_models: dict[str, dict[str, str]] | None = None,
) -> TrainerCapabilitiesV1:
    models = base_models or {
        "yolo11n": {"sha256": "2" * 64, "filename": "yolo11n.pt"},
        "yolo11s": {"sha256": "3" * 64, "filename": "yolo11s.pt"},
        "yolo11n-seg": {"sha256": "4" * 64, "filename": "yolo11n-seg.pt"},
        "yolo26n": {"sha256": "5" * 64, "filename": "yolo26n.pt"},
        "yolo26n-seg": {"sha256": "6" * 64, "filename": "yolo26n-seg.pt"},
    }
    architectures: list[TrainerArchitectureV1] = []
    families_seen: set[str] = set()
    tasks_seen: set[str] = set()
    for model_id, info in models.items():
        family, _scale, task, end2end = _parse_ultralytics_model(model_id)
        families_seen.add(family)
        tasks_seen.add(task)
        architectures.append(
            TrainerArchitectureV1(
                id=model_id,
                family=family,
                tasks=[task],
                end2end=end2end,
                base_asset_filename=info.get("filename"),
                base_asset_sha256=info.get("sha256"),
            )
        )
    sub_schemas: dict[str, dict[str, Any]] = {}
    for family in sorted(families_seen):
        for task in (DETECTION, SEGMENTATION):
            fields = _ultralytics_fields(family=family, task=task)
            schema_id = f"ultralytics-{framework_version}-{family}-{task}-5"
            entry = {
                "schema_id": schema_id,
                "model_family": family,
                "task": task,
                "fields": fields,
            }
            entry["schema_digest"] = canonical_json_digest(entry)
            sub_schemas[f"{family}:{task}"] = entry
    default_entry = sub_schemas.get("yolo11:detect") or next(iter(sub_schemas.values()))
    serving_entries: list[ServingCompatibilityV1] = []
    for fams, end2end in ((["yolov8", "yolo11"], False), (["yolo26"], True)):
        for task in (DETECTION, SEGMENTATION):
            size_map = {
                arch.id: _parse_ultralytics_model(arch.id)[1]
                for arch in architectures
                if arch.family in fams and task in arch.tasks
            }
            rt_qual = (
                segmentation_runtime(framework_version)
                if task == SEGMENTATION
                else (
                    detection_runtime(framework_version)
                    if framework_version == ULTRALYTICS_VERSION
                    else {"ultralytics": framework_version}
                )
            )
            if framework_version == ULTRALYTICS_VERSION:
                rt_qual = {
                    **rt_qual,
                    "end2end": end2end,
                    "nms": False if end2end else None,
                }
            training_rt: dict[str, Any] = {
                "framework": "ultralytics",
                "framework_version": framework_version,
                "task": task,
                "model_families": list(fams),
                "model_sizes": size_map,
                "end2end": end2end,
                "nms": False if end2end else None,
                "runtime_qualification": rt_qual,
            }
            if task == SEGMENTATION:
                training_rt["output_contract"] = SEGMENTATION_OUTPUT_CONTRACT
            serving_entries.append(
                ServingCompatibilityV1(
                    framework="ultralytics",
                    format=None,
                    training_runtime=training_rt,
                    compatible_serving_runtimes=[
                        {
                            "framework": "ultralytics",
                            "format": None,
                            "min_version": framework_version,
                        }
                    ],
                )
            )
    return TrainerCapabilitiesV1(
        protocol_version=TRAINER_PROTOCOL_VERSION,
        framework_id="ultralytics",
        framework_version=framework_version,
        runtime_versions={
            "python": "3.12.11",
            "torch": "2.14.1+cu132",
            "torchvision": "0.29.1+cu132",
            "cuda": "13.2",
            "ultralytics": framework_version,
        },
        supported_tasks=sorted(tasks_seen) or [DETECTION],
        supported_dataset_contracts=[
            "ultralytics-bbox-1",
            "ultralytics-simple-polygon-1",
            "ultralytics-instance-rle-1",
        ],
        architectures=architectures,
        parameter_schema={
            "schema_id": default_entry["schema_id"],
            "schema_digest": default_entry["schema_digest"],
            "source": "ultralytics/cfg/default.yaml",
            "source_sha256": "a" * 64,
            "groups": {
                "basic": "Basic",
                "optimizer": "Optimizer",
                "augmentation": "Augmentation",
                "loss": "Loss",
                "finetuning": "Finetuning",
                "evaluation": "Evaluation",
                "performance": "Performance",
                "platform": "Platform",
            },
            "fields": default_entry["fields"],
            "schemas": sub_schemas,
        },
        evaluator_descriptors={
            "detect": {
                "id": "ultralytics-detect-val",
                "source_sha256": "b" * 64,
                "metrics": [
                    "metrics/mAP50-95",
                    "metrics/mAP50",
                    "metrics/precision",
                    "metrics/recall",
                    "mAP50-95",
                    "mAP50",
                    "precision",
                    "recall",
                ],
            },
            "segment": {
                "id": "ultralytics-segment-val",
                "source_sha256": "c" * 64,
                "metrics": ["mask_mAP50-95", "mask_mAP50", "box_mAP50-95"],
            },
        },
        policy_digests={
            "initialization": POLICY_DIGEST,
            "initialization_segment": SEGMENTATION_POLICY_DIGEST,
        },
        serving_compatibility=serving_entries,
    )


def libreyolo_plugin_manifest(
    *,
    plugin_id: str = "yoao.trainer.libreyolo",
    version: str = "1.0.0",
    requires_gpu: bool = True,
) -> dict[str, Any]:
    return {
        "manifest_schema": "yoao-extension-manifest/v1",
        "extension_point": "trainer",
        "plugin_id": plugin_id,
        "version": version,
        "display_name": "LibreYOLO Trainer",
        "description": "Test fixture LibreYOLO9 trainer plugin.",
        "homepage": "https://example.com/trainer-libreyolo",
        "publisher": "YOAO Official",
        "license": "MIT",
        "protocol_version": TRAINER_PROTOCOL_VERSION,
        "entrypoint": ["yoao-trainer-libreyolo"],
        "capabilities": ["train", "evaluate", "export_weights"],
        "resource": {"memory_mb": 8192},
        "extension": {
            "framework_id": "libreyolo",
            "framework_version": LIBRE_VERSION,
            "requires_gpu": requires_gpu,
        },
    }


def libreyolo_plugin_capabilities(
    *,
    base_models: dict[str, dict[str, str]] | None = None,
) -> TrainerCapabilitiesV1:
    models = base_models or {
        "libreyolo9t": {"sha256": "7" * 64, "filename": "LibreYOLO9t.pt"},
        "libreyolo9s": {"sha256": "8" * 64, "filename": "LibreYOLO9s.pt"},
    }
    architectures = [
        TrainerArchitectureV1(
            id=model_id,
            family="libreyolo9",
            tasks=[DETECTION],
            end2end=False,
            base_asset_filename=info.get("filename"),
            base_asset_sha256=info.get("sha256"),
        )
        for model_id, info in models.items()
    ]
    fields = [
        {
            "key": "epochs",
            "label": "Epochs",
            "group": "basic",
            "status": "editable",
            "native_type": "int",
            "default": 50,
            "native_default": 100,
            "defined_in": "libreyolo",
            "reason": "Controlled epochs",
            "rules": {"type": "integer", "minimum": 1, "maximum": 1000},
            "aliases": [],
        },
        {
            "key": "batch",
            "label": "Batch",
            "group": "basic",
            "status": "editable",
            "native_type": "int",
            "default": 8,
            "native_default": 16,
            "defined_in": "libreyolo",
            "reason": "Controlled batch",
            "rules": {"type": "integer", "minimum": 1, "maximum": 64},
            "aliases": [],
        },
        {
            "key": "imgsz",
            "label": "Image Size",
            "group": "basic",
            "status": "editable",
            "native_type": "int",
            "default": 640,
            "native_default": 640,
            "defined_in": "libreyolo",
            "reason": "Controlled imgsz",
            "rules": {
                "type": "integer",
                "minimum": 128,
                "maximum": 1280,
                "multipleOf": 32,
            },
            "aliases": [],
        },
        {
            "key": "seed",
            "label": "Seed",
            "group": "performance",
            "status": "editable",
            "native_type": "int",
            "default": 42,
            "native_default": 42,
            "defined_in": "libreyolo",
            "reason": "Random seed",
            "rules": {"type": "integer", "minimum": 0, "maximum": 2147483647},
            "aliases": [],
        },
    ]
    schema = {
        "schema_id": "libreyolo-1.6.0-yolo9-detect-3",
        "source": "libreyolo/training/config.py",
        "source_sha256": "d" * 64,
        "groups": {"basic": "Basic", "performance": "Performance"},
        "fields": fields,
    }
    schema["schema_digest"] = canonical_json_digest(schema)
    return TrainerCapabilitiesV1(
        protocol_version=TRAINER_PROTOCOL_VERSION,
        framework_id="libreyolo",
        framework_version=LIBRE_VERSION,
        runtime_versions={
            "python": "3.12.11",
            "torch": "2.14.1+cu132",
            "torchvision": "0.29.1+cu132",
            "libreyolo": LIBRE_VERSION,
        },
        supported_tasks=[DETECTION],
        supported_dataset_contracts=["ultralytics-bbox-1", "ultralytics-yolo-1"],
        architectures=architectures,
        parameter_schema=schema,
        evaluator_descriptors={
            "detect": {
                "id": "libreyolo-detect-val",
                "source_sha256": "e" * 64,
                "metrics": ["mAP50-95", "mAP50", "precision", "recall"],
            }
        },
        serving_compatibility=[
            ServingCompatibilityV1(
                framework="libreyolo",
                format=LIBRE_FORMAT,
                training_runtime={
                    "framework": "libreyolo",
                    "framework_version": LIBRE_VERSION,
                    "task": DETECTION,
                    "model_families": ["libreyolo9", "yolo9"],
                    "serving_model_family": "yolo9",
                    "model_sizes": {
                        arch.id.lower(): arch.id[-1].lower() for arch in architectures
                    },
                    "runtime_qualification": deepcopy(LIBRE_RUNTIME),
                },
                compatible_serving_runtimes=[
                    {
                        "framework": "libreyolo",
                        "format": LIBRE_FORMAT,
                        "min_version": LIBRE_VERSION,
                    }
                ],
            )
        ],
    )


async def seed_enabled_trainer_plugin(
    db,
    *,
    framework_id: str = "ultralytics",
    plugin_id: str | None = None,
    framework_version: str | None = None,
    execution_mode: str = "container",
    artifact: str | None = None,
    image_digest: str | None = None,
    base_models: dict[str, dict[str, str]] | None = None,
    requires_gpu: bool = True,
    capabilities_list: list[str] | None = None,
    enable: bool = True,
) -> TrainerPluginRecord:
    store = TrainerPluginsPostgresData(db)
    resolved_plugin_id = plugin_id or f"yoao.trainer.{framework_id}"
    existing = await store.get_plugin(resolved_plugin_id)
    if existing is not None:
        if enable and existing.status == "disabled":
            return await store.transition_plugin(
                {
                    "plugin_id": resolved_plugin_id,
                    "target_status": "enabled",
                    "expected_revision": existing.revision,
                }
            )
        return existing
    if framework_id == "libreyolo":
        manifest = libreyolo_plugin_manifest(
            plugin_id=resolved_plugin_id,
            requires_gpu=requires_gpu,
        )
        caps = libreyolo_plugin_capabilities(base_models=base_models)
    else:
        fw_ver = framework_version or ULTRALYTICS_VERSION
        manifest = ultralytics_plugin_manifest(
            plugin_id=resolved_plugin_id,
            framework_version=fw_ver,
            requires_gpu=requires_gpu,
            capabilities=capabilities_list,
        )
        caps = ultralytics_plugin_capabilities(
            framework_version=fw_ver,
            base_models=base_models,
        )
    resolved_artifact = None
    if execution_mode == "container":
        digest_part = image_digest or ("sha256:" + "1" * 64)
        resolved_artifact = (
            artifact or f"ghcr.io/yoao-labs/trainer-{framework_id}@{digest_part}"
        )
    payload = validate_registration_payload(
        RegisterTrainerPluginRequest(
            execution_mode=execution_mode,
            source_tier="official",
            artifact=resolved_artifact,
            manifest=manifest,
            capabilities_snapshot=caps,
        )
    )
    record = await store.register_plugin(payload)
    report: dict[str, Any] = {
        "passed": True,
        "plugin_id": record.plugin_id,
        "version": record.version,
        "execution_mode": record.execution_mode,
        "capabilities_digest": record.capabilities_digest,
    }
    if record.execution_mode == "container":
        report["artifact"] = record.artifact
    else:
        report["entrypoint_digest"] = entrypoint_digest(record.entrypoint)
    record = await store.qualify_plugin(
        {
            "plugin_id": record.plugin_id,
            "expected_revision": record.revision,
            "qualification_report_sha256": canonical_json_digest(report),
            "qualification_report": report,
        }
    )
    if enable:
        record = await store.transition_plugin(
            {
                "plugin_id": record.plugin_id,
                "target_status": "enabled",
                "expected_revision": record.revision,
            }
        )
    return record


async def disable_trainer_plugin(db, plugin_id: str) -> TrainerPluginRecord | None:
    store = TrainerPluginsPostgresData(db)
    record = await store.get_plugin(plugin_id)
    if record is None or record.status != "enabled":
        return record
    return await store.transition_plugin(
        {
            "plugin_id": plugin_id,
            "target_status": "disabled",
            "expected_revision": record.revision,
        }
    )


async def retire_trainer_plugin(db, plugin_id: str) -> TrainerPluginRecord | None:
    store = TrainerPluginsPostgresData(db)
    record = await store.get_plugin(plugin_id)
    if record is None or record.status == "retired":
        return record
    if record.status == "enabled":
        record = await store.transition_plugin(
            {
                "plugin_id": plugin_id,
                "target_status": "disabled",
                "expected_revision": record.revision,
            }
        )
    return await store.transition_plugin(
        {
            "plugin_id": plugin_id,
            "target_status": "retired",
            "expected_revision": record.revision,
        }
    )


DEFAULT_TEST_BASE_ASSET_SHAS: tuple[str, ...] = (
    "2" * 64,
    "3" * 64,
    "4" * 64,
    "5" * 64,
    "6" * 64,
    "7" * 64,
    "8" * 64,
    "9" * 64,
    "a" * 64,
    "55ed65c56c91713d23e8402371c6c49a6fd84f257f7dce452e8d70e41dcbe152",
)

ALL_ULTRALYTICS_MODEL_IDS: tuple[str, ...] = tuple(
    f"{family}{scale}{suffix}"
    for family in ("yolov8", "yolo11", "yolo26")
    for suffix in ("", "-seg")
    for scale in ("n", "s", "m", "l", "x")
)

ALL_LIBREYOLO_MODEL_IDS: tuple[str, ...] = (
    "libreyolo9t",
    "libreyolo9s",
    "libreyolo9m",
    "libreyolo9c",
)


def ultralytics_schema(
    base_model: str = "yolo11n",
    *,
    framework_version: str = ULTRALYTICS_VERSION,
) -> dict[str, Any]:
    family, _scale, task, _end2end = _parse_ultralytics_model(base_model)
    caps = ultralytics_plugin_capabilities(framework_version=framework_version)
    sub = caps.parameter_schema["schemas"][f"{family}:{task}"]
    return {
        "schema_id": sub["schema_id"],
        "schema_digest": sub["schema_digest"],
        "framework": "ultralytics",
        "framework_version": framework_version,
        "adapter_revision": 3,
        "task": task,
        "model_family": family,
        "fields": deepcopy(sub["fields"]),
    }


def closed_mosaic(config: dict[str, Any], epoch: int) -> bool:
    values = (config.get("configuration") or {}).get("effective_values") or config.get(
        "parameters", {}
    )
    epochs = int(values.get("epochs", 50))
    close_after = int(values.get("close_mosaic", 10))
    if close_after <= 0:
        return False
    return epoch > max(epochs - close_after, 0)


def build_v3_receipt(
    inventory: dict[str, dict[str, Any]],
    *,
    framework: str = "ultralytics",
    serving_format: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    items: list[TrainArtifactV1] = []
    for name, spec in inventory.items():
        raw_role = spec.get("role", "config")
        if name in {
            "result.json",
            "train.log",
            "manifest.json",
            "observability.json",
        } or raw_role in {"log", "manifest"}:
            continue
        if name == "input-membership.json":
            role = "input_membership"
        elif raw_role in {"weights", "checkpoint", "metrics"}:
            role = raw_role
        else:
            role = "extra"
        items.append(
            TrainArtifactV1(
                filename=name,
                role=role,
                sha256=spec["sha256"],
                size_bytes=spec["size_bytes"],
            )
        )
    receipt = TrainReceiptV1(
        protocol_version=TRAINER_PROTOCOL_VERSION,
        status="succeeded",
        artifacts=items,
        serving_compatibility=ServingCompatibilityV1(
            framework=framework,
            format=serving_format,
            training_runtime={"framework": framework},
            compatible_serving_runtimes=[],
        ),
    )
    raw = json.dumps(receipt.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    spec_out = {
        "name": "result.json",
        "role": "config",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    return raw, spec_out


def plugin_executor_claim(
    *,
    resource_id: str,
    gpu_uuid: str,
    image_digest: str = "sha256:" + "1" * 64,
    frameworks: dict[str, str] | None = None,
    execution_modes: list[str] | None = None,
    base_asset_sha256: list[str] | None = None,
    extra_runtime: dict[str, Any] | None = None,
) -> ExecutorClaim:
    import uuid

    runtime: dict[str, Any] = {
        "executor_protocol": 3,
        "trainer_protocols": [TRAINER_PROTOCOL_VERSION],
        "execution_modes": execution_modes or ["container", "local_process"],
        "image_digest": image_digest,
        "gpu_uuid": gpu_uuid,
        "frameworks": frameworks or {"ultralytics": ULTRALYTICS_VERSION},
        "base_asset_sha256": list(
            DEFAULT_TEST_BASE_ASSET_SHAS
            if base_asset_sha256 is None
            else base_asset_sha256
        ),
        **(extra_runtime or {}),
    }
    return ExecutorClaim(
        executor_id="owned-executor",
        boot_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        resource_id=resource_id,
        runtime=runtime,
    )
