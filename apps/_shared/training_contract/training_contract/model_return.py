"""Immutable qualification for native managed model return, separate from training."""

from copy import deepcopy
import re
from aap_task_contract import normalize_task
from .dataset import DETECTION, SEGMENTATION, config_task, data_contract
from .models import (
    ULTRALYTICS_VERSION,
    LEGACY_ULTRALYTICS_VERSION,
    model_spec,
    supports_version,
)


LIBRE_RUNTIME = {
    "revision": 1,
    "libreyolo": "1.6.0",
    "torch": "2.14.1+cu132",
    "torchvision": "0.29.1+cu132",
    "device": "cpu",
}
LIBRE_FORMAT = "libreyolo-native-state-dict-1.0"

SEGMENTATION_OUTPUT_CONTRACT = "aap-instance-rle-1"
SEGMENTATION_RUNTIME = {
    "revision": 1,
    "ultralytics": "8.4.177",
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


def segmentation_runtime(framework_version):
    if framework_version == LEGACY_ULTRALYTICS_VERSION:
        return deepcopy(SEGMENTATION_RUNTIME)
    if framework_version != ULTRALYTICS_VERSION:
        raise ValueError("该分割框架版本尚未登记受控推理资格")
    return {
        **deepcopy(SEGMENTATION_RUNTIME),
        "revision": 2,
        "ultralytics": ULTRALYTICS_VERSION,
        "source_sha256": {
            "ultralytics.models.yolo.segment.predict": "66371a017252aa2e8606ba13dbc6c76307088d69159a5f46970138a9412846c5",
            "ultralytics.utils.ops": "7b15cd980abbd578a8be91661c2399b2cbdea0207f1e61b0fb74cfac659bed77",
        },
    }


def detection_runtime(framework_version):
    if framework_version != ULTRALYTICS_VERSION:
        raise ValueError("该检测框架版本没有显式的新版受控推理资格")
    return {
        "revision": 2,
        "ultralytics": ULTRALYTICS_VERSION,
        "torch": "2.14.1+cu132",
        "torchvision": "0.29.1+cu132",
        "source_sha256": {
            "ultralytics.utils.ops": "7b15cd980abbd578a8be91661c2399b2cbdea0207f1e61b0fb74cfac659bed77",
        },
    }


def segmentation_identity(base_model, framework_version=LEGACY_ULTRALYTICS_VERSION):
    spec = model_spec("ultralytics", base_model)
    if spec.task != SEGMENTATION or not supports_version(spec, framework_version):
        raise ValueError("该分割模型规格尚未通过受控推理资格检查")
    runtime = segmentation_runtime(framework_version)
    if framework_version == ULTRALYTICS_VERSION:
        runtime.update(end2end=spec.end2end, nms=False if spec.end2end else None)
    return {
        "model_family": spec.family,
        "model_size": spec.scale,
        "output_contract": SEGMENTATION_OUTPUT_CONTRACT,
        "runtime_qualification": runtime,
    }


def ultralytics_identity(base_model, framework_version):
    spec = model_spec("ultralytics", base_model)
    if not supports_version(spec, framework_version):
        raise ValueError("该模型与框架版本没有匹配的受控实现")
    if spec.task == SEGMENTATION:
        return segmentation_identity(base_model, framework_version)
    if framework_version == LEGACY_ULTRALYTICS_VERSION:
        return None
    runtime = detection_runtime(framework_version)
    runtime.update(end2end=spec.end2end, nms=False if spec.end2end else None)
    return {
        "model_family": spec.family,
        "model_size": spec.scale,
        "runtime_qualification": runtime,
    }


def _segmentation_eligibility(config):
    identity = (config.get("configuration") or {}).get("identity", {})
    version = config.get("framework_version")
    spec = model_spec("ultralytics", (config.get("base_model") or {}).get("id"))
    if (
        config.get("framework") != "ultralytics"
        or not supports_version(spec, version)
        or config.get("adapter_revision") != 2
        or not identity
        or normalize_task(identity.get("task")) != SEGMENTATION
        or identity.get("model_family") != spec.family
        or identity.get("framework") != "ultralytics"
        or identity.get("framework_version") != version
        or normalize_task(data_contract(config.get("data_contract") or {})["task"])
        != SEGMENTATION
    ):
        raise ValueError("该分割任务、数据桥接或运行配置尚未通过模型回流资格检查")
    return segmentation_identity(spec.id, version)


def libre_identity(base_model):
    match = re.fullmatch(r"libreyolo9([tsmc])", base_model.lower())
    if match is None:
        raise ValueError("该 LibreYOLO 家族或规格尚未通过原生推理资格检查")
    return {
        "model_family": "yolo9",
        "model_size": match[1],
        "artifact_format": LIBRE_FORMAT,
        "runtime_qualification": deepcopy(LIBRE_RUNTIME),
    }


def registration_eligibility(config):
    framework = config.get("framework")
    try:
        task = config_task(config)
    except ValueError:
        task = None
    if task == SEGMENTATION:
        try:
            qualification = _segmentation_eligibility(config)
        except (ValueError, TypeError, KeyError) as exc:
            return {"qualified": False, "reason": str(exc), "qualification": None}
        return {"qualified": True, "reason": None, "qualification": qualification}
    if task != DETECTION or str(
        (config.get("base_model") or {}).get("id", "")
    ).lower().endswith("-seg"):
        return {
            "qualified": False,
            "reason": "该任务尚未通过模型回流与推理资格检查；可下载原生权重",
            "qualification": None,
        }
    if framework == "ultralytics":
        if config.get("framework_version") == ULTRALYTICS_VERSION:
            try:
                qualification = ultralytics_identity(
                    (config.get("base_model") or {}).get("id"), ULTRALYTICS_VERSION
                )
            except (ValueError, TypeError) as exc:
                return {"qualified": False, "reason": str(exc), "qualification": None}
            return {"qualified": True, "reason": None, "qualification": qualification}
        if config.get("framework_version") not in {None, LEGACY_ULTRALYTICS_VERSION}:
            return {
                "qualified": False,
                "reason": "该显式框架版本尚未通过受控模型回流资格检查",
                "qualification": None,
            }
        return {"qualified": True, "reason": None, "qualification": None}
    if framework != "libreyolo" or config.get("framework_version") != "1.6.0":
        return {
            "qualified": False,
            "reason": "该训练器或版本尚未通过模型回流资格检查；可下载原生权重",
            "qualification": None,
        }
    identity = (config.get("configuration") or {}).get("identity", {})
    try:
        if config.get("adapter_revision", 1) not in {1, 2}:
            raise ValueError("该训练适配器修订尚未通过模型回流资格检查")
        if config.get("adapter_revision") == 2 and not identity:
            raise ValueError("冻结训练配置缺少模型身份")
        if identity and (
            normalize_task(identity.get("task")) != DETECTION
            or identity.get("model_family") != "yolo9"
            or identity.get("framework") != framework
            or identity.get("framework_version") != "1.6.0"
        ):
            raise ValueError("该任务或模型家族尚未通过原生推理资格检查")
        qualification = libre_identity((config.get("base_model") or {}).get("id", ""))
    except ValueError as exc:
        return {"qualified": False, "reason": str(exc), "qualification": None}
    return {"qualified": True, "reason": None, "qualification": qualification}
