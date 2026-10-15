"""Offline qualification for retained-model weights-only initialization."""

import re
import uuid

from .catalog import digest

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


def validate_pin(pin):
    if not isinstance(pin, dict) or set(pin) != PIN_KEYS:
        raise ValueError("初始化来源的冻结字段不完整")
    if (
        pin["kind"] != "trained_model"
        or pin["policy_digest"] != POLICY_DIGEST
        or any(
            pin[key] != POLICY[key]
            for key in ("framework", "framework_version", "task", "model_family")
        )
        or not isinstance(pin["base_model"], str)
        or pin["base_model"] not in VARIANTS
    ):
        raise ValueError("初始化来源尚未通过当前原生资格验证")
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
        or not artifact["key"].startswith(
            f"projects/{pin['project_id']}/runs/{pin['run_id']}/attempts/{pin['attempt_id']}/"
        )
        or len(artifact["key"]) > 500
        or not is_sha256(artifact["sha256"])
        or type(artifact["size_bytes"]) is not int
        or not 0 < artifact["size_bytes"] <= MAX_WEIGHTS_BYTES
    ):
        raise ValueError("初始化权重身份或大小无效")
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
        or runtime["torch"].split("+", 1)[0] != POLICY["torch"]
        or runtime["torchvision"].split("+", 1)[0] != POLICY["torchvision"]
        or re.fullmatch(r"3\.12\.\d+", runtime["python"]) is None
        or runtime["cuda"] != POLICY["cuda"]
        or not runtime["image_digest"].startswith("sha256:")
        or not is_sha256(runtime["image_digest"][7:])
    ):
        raise ValueError("初始化来源运行环境尚未通过资格验证")
    evaluation = pin["evaluation"]
    if (
        not isinstance(evaluation, dict)
        or set(evaluation) != {"report_sha256", "report_digest", "validation_digest"}
        or not all(is_sha256(value) for value in evaluation.values())
        or evaluation["validation_digest"] != pin["validation_digest"]
        or digest({key: value for key, value in pin.items() if key != "source_digest"})
        != pin["source_digest"]
    ):
        raise ValueError("初始化来源或评估证据摘要不匹配")
    return pin


def compatible_runtime(config, runtime):
    pin = validate_pin(config["initialization"])
    return bool(
        config.get("adapter_revision") == 2
        and config.get("framework") == pin["framework"]
        and config.get("framework_version") == pin["framework_version"]
        and config.get("base_model", {}).get("id") == pin["base_model"]
        and config.get("classes") == pin["classes"]
        and runtime.get("initialization_policy_digest") == POLICY_DIGEST
        and runtime.get("torch", "").split("+", 1)[0] == POLICY["torch"]
        and runtime.get("torchvision", "").split("+", 1)[0] == POLICY["torchvision"]
        and re.fullmatch(r"3\.12\.\d+", runtime.get("python", "")) is not None
        and runtime.get("cuda") == POLICY["cuda"]
    )


def verify_evidence(config, evidence, *, resumed):
    pin = validate_pin(config["initialization"])
    if not isinstance(evidence, dict) or (
        evidence.get("policy_digest") != POLICY_DIGEST
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
