"""Immutable qualification for native managed model return, separate from training."""

from copy import deepcopy
import re
from .dataset import DETECTION, config_task


LIBRE_RUNTIME = {
    "revision": 1,
    "libreyolo": "1.6.0",
    "torch": "2.14.1+cu132",
    "torchvision": "0.29.1+cu132",
    "device": "cpu",
}
LIBRE_FORMAT = "libreyolo-native-state-dict-1.0"


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
    if config_task(config) != DETECTION or str(
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
            identity.get("task") != "image-detection"
            or identity.get("model_family") != "yolo9"
            or identity.get("framework") != framework
            or identity.get("framework_version") != "1.6.0"
        ):
            raise ValueError("该任务或模型家族尚未通过原生推理资格检查")
        qualification = libre_identity((config.get("base_model") or {}).get("id", ""))
    except ValueError as exc:
        return {"qualified": False, "reason": str(exc), "qualification": None}
    return {"qualified": True, "reason": None, "qualification": qualification}
