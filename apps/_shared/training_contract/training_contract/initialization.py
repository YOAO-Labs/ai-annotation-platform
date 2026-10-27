"""Offline qualification for retained-model weights-only initialization."""

import re
import uuid
from aap_task_contract import normalize_task

from .catalog import digest
from .dataset import SEGMENTATION, config_task, data_contract, data_task
from .models import (
    ULTRALYTICS_VERSION,
    LEGACY_ULTRALYTICS_VERSION,
    model_spec,
    supports_version,
)

MAX_WEIGHTS_BYTES = 2 * 1024**3
POLICY = {
    "revision": 1,
    "purpose": "trained-model-initialization",
    "framework": "ultralytics",
    "framework_version": "8.4.177",
    "torch": "2.14.1",
    "torchvision": "0.29.1",
    "python": "3.12",
    "cuda": "13.2",
    "task": "image-detection",
    "model_family": "yolo11",
    "class_identity": "exact-native-order",
    "architecture": "same-controlled-variant",
    "transfer": "all-state-tensors",
    "state": "fresh-training",
    "maximum_bytes": MAX_WEIGHTS_BYTES,
}
POLICY_DIGEST = digest(POLICY)
SEGMENTATION_POLICY = {
    **POLICY,
    "revision": 2,
    "task": SEGMENTATION,
    "qualified_models": ["yolo11n-seg"],
    "source_data_identity": "exact-frozen-contract-and-bridge",
    "evaluation_identity": "raw-report-and-normalized-registration-separately-frozen",
}
SEGMENTATION_POLICY_DIGEST = digest(SEGMENTATION_POLICY)


def policy_for_model(base_model, framework_version):
    spec = model_spec("ultralytics", base_model)
    if not supports_version(spec, framework_version):
        raise ValueError("初始化模型与原生框架版本不兼容")
    segmentation = spec.task == SEGMENTATION
    if framework_version == LEGACY_ULTRALYTICS_VERSION:
        return SEGMENTATION_POLICY if segmentation else POLICY
    return {
        **(SEGMENTATION_POLICY if segmentation else POLICY),
        "revision": 3,
        "framework_version": ULTRALYTICS_VERSION,
        "task": spec.task,
        "model_family": spec.family,
        "qualified_models": [
            f"{spec.family}{scale}{'-seg' if segmentation else ''}" for scale in "nsmlx"
        ],
    }


def supported_policy_digests(framework_version):
    if framework_version == LEGACY_ULTRALYTICS_VERSION:
        return [POLICY_DIGEST, SEGMENTATION_POLICY_DIGEST]
    if framework_version == ULTRALYTICS_VERSION:
        return [
            digest(policy_for_model(f"{family}n{suffix}", framework_version))
            for family in ("yolov8", "yolo11", "yolo26")
            for suffix in ("", "-seg")
        ]
    return []


def policy_for_pin(pin):
    if isinstance(pin, dict) and pin.get("policy_digest") == POLICY_DIGEST:
        return POLICY
    if isinstance(pin, dict) and pin.get("policy_digest") == SEGMENTATION_POLICY_DIGEST:
        return SEGMENTATION_POLICY
    if isinstance(pin, dict):
        policy = policy_for_model(pin.get("base_model"), pin.get("framework_version"))
        if pin.get("policy_digest") == digest(policy):
            return policy
    raise ValueError("初始化来源尚未通过当前原生资格验证")


VARIANTS = {f"yolo11{scale}" for scale in "nsmlx"}
PIN_KEYS = {
    "kind",
    "policy_digest",
    "project_id",
    "model_version_id",
    "model_name",
    "run_id",
    "attempt_id",
    "dataset_id",
    "framework",
    "framework_version",
    "task",
    "model_family",
    "base_model",
    "artifact",
    "classes",
    "classes_digest",
    "bindings",
    "binding_digest",
    "config_digest",
    "dataset_digest",
    "validation_digest",
    "runtime",
    "evaluation",
    "source_digest",
}


def is_sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def artifact_root(pin):
    """Accept the original address or one bounded managed training root."""
    key = pin["artifact"]["key"]
    logical = f"projects/{pin['project_id']}/runs/{pin['run_id']}/attempts/{pin['attempt_id']}/"
    if not isinstance(key, str):
        raise ValueError("初始化权重地址无效")
    if key.startswith(logical):
        return ""
    marker = "_aap_managed/training/"
    root, separator, suffix = key.rpartition(marker + logical)
    prefix = root + marker
    if (
        not separator
        or not suffix
        or prefix.startswith("__aap_reference__/")
        or len(prefix) > 266
        or len(prefix.encode("utf-8")) > 707
        or len(key.encode("utf-8")) > 1024
        or "\\" in key
        or any(ord(char) < 32 or ord(char) == 127 for char in key)
        or any(part in {"", ".", ".."} for part in key.split("/"))
    ):
        raise ValueError("初始化权重地址无效")
    return prefix


def saved_prefix_compatible(config, runtime):
    pin = config.get("initialization")
    return (
        not pin
        or not artifact_root(pin)
        or (
            type(runtime.get("training_saved_prefix_schema", 1)) is int
            and runtime.get("training_saved_prefix_schema", 1) >= 2
        )
    )


def validate_pin(pin):
    policy = policy_for_pin(pin)
    segmentation = normalize_task(policy["task"]) == SEGMENTATION
    if set(pin) != PIN_KEYS | ({"data_contract"} if segmentation else set()):
        raise ValueError("初始化来源的冻结字段不完整")
    if (
        pin["kind"] != "trained_model"
        or any(
            pin[key] != policy[key]
            for key in ("framework", "framework_version", "task", "model_family")
        )
        or not isinstance(pin["base_model"], str)
        or pin["base_model"] not in policy.get("qualified_models", VARIANTS)
    ):
        raise ValueError("初始化来源尚未通过当前原生资格验证")
    if segmentation and (
        data_task(pin["data_contract"]) != SEGMENTATION
        or data_contract(pin["data_contract"]) != pin["data_contract"]
    ):
        raise ValueError("分割初始化缺少精确冻结的来源数据桥接")
    for key in ("project_id", "model_version_id", "run_id", "attempt_id", "dataset_id"):
        if not isinstance(pin[key], str) or str(uuid.UUID(pin[key])) != pin[key]:
            raise ValueError("初始化来源标识无效")
    if not isinstance(pin["model_name"], str) or not 0 < len(pin["model_name"]) <= 120:
        raise ValueError("初始化来源名称无效")
    for key in (
        "source_digest",
        "classes_digest",
        "binding_digest",
        "config_digest",
        "dataset_digest",
        "validation_digest",
    ):
        if not is_sha256(pin[key]):
            raise ValueError("初始化来源摘要不完整")
    artifact = pin["artifact"]
    if (
        not isinstance(artifact, dict)
        or set(artifact) != {"key", "role", "sha256", "size_bytes"}
        or artifact["role"] != "weights"
        or not isinstance(artifact["key"], str)
        or len(artifact["key"]) > 500
        or not is_sha256(artifact["sha256"])
        or type(artifact["size_bytes"]) is not int
        or not 0 < artifact["size_bytes"] <= MAX_WEIGHTS_BYTES
    ):
        raise ValueError("初始化权重身份或大小无效")
    artifact_root(pin)
    classes, bindings = pin["classes"], pin["bindings"]
    if (
        not isinstance(classes, list)
        or not 0 < len(classes) <= 10000
        or not all(
            isinstance(value, str) and 0 < len(value) <= 2000 for value in classes
        )
        or len(set(classes)) != len(classes)
        or not isinstance(bindings, list)
        or len(bindings) != len(classes)
        or not all(
            isinstance(value, str) and 0 < len(value) <= 2000 for value in bindings
        )
        or digest(classes) != pin["classes_digest"]
        or digest({"classes": classes, "bindings": bindings}) != pin["binding_digest"]
    ):
        raise ValueError("初始化原生类别与绑定身份不完整")
    runtime = pin["runtime"]
    if (
        not isinstance(runtime, dict)
        or set(runtime) != {"python", "torch", "torchvision", "cuda", "image_digest"}
        or not all(
            isinstance(value, str) and 0 < len(value) <= 150
            for value in runtime.values()
        )
        or runtime["torch"].split("+", 1)[0] != policy["torch"]
        or runtime["torchvision"].split("+", 1)[0] != policy["torchvision"]
        or re.fullmatch(r"3\.12\.\d+", runtime["python"]) is None
        or runtime["cuda"] != policy["cuda"]
        or not runtime["image_digest"].startswith("sha256:")
        or not is_sha256(runtime["image_digest"][7:])
    ):
        raise ValueError("初始化来源运行环境尚未通过资格验证")
    evaluation = pin["evaluation"]
    if (
        not isinstance(evaluation, dict)
        or set(evaluation)
        != (
            {"report_sha256", "report_digest", "validation_digest"}
            | (
                {"normalized_model_digest", "descriptor_digest", "weights_sha256"}
                if segmentation
                else set()
            )
        )
        or not all(is_sha256(value) for value in evaluation.values())
        or evaluation["validation_digest"] != pin["validation_digest"]
        or segmentation
        and evaluation["weights_sha256"] != artifact["sha256"]
        or digest({key: value for key, value in pin.items() if key != "source_digest"})
        != pin["source_digest"]
    ):
        raise ValueError("初始化来源或评估证据摘要不匹配")
    return pin


def compatible_runtime(config, runtime):
    pin = validate_pin(config["initialization"])
    policy = policy_for_pin(pin)
    return bool(
        saved_prefix_compatible(config, runtime)
        and config.get("adapter_revision") == 2
        and config.get("framework") == pin["framework"]
        and config.get("framework_version") == pin["framework_version"]
        and config.get("base_model", {}).get("id") == pin["base_model"]
        and config.get("classes") == pin["classes"]
        and (
            runtime.get("initialization_policy_digest") == pin["policy_digest"]
            or isinstance(runtime.get("initialization_policy_digests"), list)
            and pin["policy_digest"] in runtime["initialization_policy_digests"]
        )
        and (
            normalize_task(policy["task"]) != SEGMENTATION
            or config_task(config) == SEGMENTATION
            and data_task(config["data_contract"]) == SEGMENTATION
        )
        and runtime.get("torch", "").split("+", 1)[0] == policy["torch"]
        and runtime.get("torchvision", "").split("+", 1)[0] == policy["torchvision"]
        and re.fullmatch(r"3\.12\.\d+", runtime.get("python", "")) is not None
        and runtime.get("cuda") == policy["cuda"]
    )


def verify_evidence(config, evidence, *, resumed):
    pin = validate_pin(config["initialization"])
    if not isinstance(evidence, dict) or (
        evidence.get("policy_digest") != pin["policy_digest"]
        or evidence.get("source_digest") != pin["source_digest"]
        or evidence.get("source_sha256") != pin["artifact"]["sha256"]
        or evidence.get("mode") != ("checkpoint_resume" if resumed else "weights_only")
    ):
        raise ValueError("原生初始化回执与冻结来源或执行方式不符")
    if resumed:
        if type(evidence.get("start_epoch")) is not int or evidence["start_epoch"] <= 0:
            raise ValueError("原生续训未恢复本实验的执行轮次")
        return
    if (
        not is_sha256(evidence.get("source_tensor_digest"))
        or evidence.get("source_tensor_digest") != evidence.get("loaded_tensor_digest")
        or type(evidence.get("tensor_count")) is not int
        or evidence["tensor_count"] <= 0
        or type(evidence.get("start_epoch")) is not int
        or evidence["start_epoch"] != 0
        or type(evidence.get("optimizer_state_count")) is not int
        or evidence["optimizer_state_count"] != 0
        or type(evidence.get("scheduler_last_epoch")) is not int
        or evidence["scheduler_last_epoch"] not in {-1, 0}
        or evidence.get("scaler_state") != {}
        or type(evidence.get("ema_updates")) is not int
        or evidence["ema_updates"] != 0
        or evidence.get("best_fitness") not in (None, 0.0)
        or evidence.get("best_weights_present") is not False
    ):
        raise ValueError("原生初始化未完整转移权重或未重置训练状态")

    if normalize_task(policy_for_pin(pin)["task"]) == SEGMENTATION and (
        not is_sha256(evidence.get("mask_head_tensor_digest"))
        or evidence["mask_head_tensor_digest"]
        != evidence.get("loaded_mask_head_tensor_digest")
        or type(evidence.get("mask_head_tensor_count")) is not int
        or evidence["mask_head_tensor_count"] <= 0
    ):
        raise ValueError("分割初始化未完整转移原生 mask head 权重")
