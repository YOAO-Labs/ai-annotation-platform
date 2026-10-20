"""Immutable qualification for native managed model return, separate from training."""

from copy import deepcopy
import re
from aap_task_contract import normalize_task
from .dataset import DETECTION, SEGMENTATION, config_task, data_contract


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


def segmentation_identity(base_model):
    if str(base_model).lower() != "yolo11n-seg":
        raise ValueError("该分割模型规格尚未通过受控推理资格检查")
    return {
        "model_family": "yolo11",
        "model_size": "n",
        "output_contract": SEGMENTATION_OUTPUT_CONTRACT,
        "runtime_qualification": deepcopy(SEGMENTATION_RUNTIME),
    }


def _segmentation_eligibility(config):
    identity = (config.get("configuration") or {}).get("identity", {})
    if (
        config.get("framework") != "ultralytics"
        or config.get("framework_version") != "8.4.177"
        or config.get("adapter_revision") != 2
        or not identity
        or normalize_task(identity.get("task")) != SEGMENTATION
        or identity.get("model_family") != "yolo11"
        or identity.get("framework") != "ultralytics"
        or identity.get("framework_version") != "8.4.177"
        or normalize_task(data_contract(config.get("data_contract") or {})["task"])
        != SEGMENTATION
    ):
        raise ValueError("该分割任务、数据桥接或运行配置尚未通过模型回流资格检查")
    return segmentation_identity((config.get("base_model") or {}).get("id"))


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
