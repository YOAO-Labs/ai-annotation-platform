"""Framework-agnostic validation for retained-model weights-only initialization."""

import re
import uuid

from yoao_task_contract import normalize_task

from .dataset import SEGMENTATION, data_contract, data_task
from .trainer_plugin import canonical_json_digest as digest

MAX_WEIGHTS_BYTES = 2 * 1024**3
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
    if not isinstance(pin, dict):
        raise ValueError("初始化来源必须为 JSON 对象")
    if not isinstance(pin.get("task"), str) or not pin["task"].strip():
        raise ValueError("初始化来源缺少任务身份")
    try:
        normalized_task = normalize_task(pin["task"])
    except ValueError as exc:
        raise ValueError("初始化来源任务身份无效") from exc
    segmentation = normalized_task == SEGMENTATION
    expected_keys = PIN_KEYS | ({"data_contract"} if segmentation else set())
    if set(pin) != expected_keys:
        raise ValueError("初始化来源的冻结字段不完整")
    if pin["kind"] != "trained_model":
        raise ValueError("初始化来源类型无效")
    for key in ("framework", "framework_version", "model_family", "base_model"):
        value = pin.get(key)
        if not isinstance(value, str) or not 0 < len(value.strip()) <= 128:
            raise ValueError(f"初始化来源字段 {key} 无效")
    if segmentation and (
        not isinstance(pin.get("data_contract"), dict)
        or data_task(pin["data_contract"]) != SEGMENTATION
        or data_contract(pin["data_contract"]) != pin["data_contract"]
    ):
        raise ValueError("分割初始化缺少精确冻结的来源数据桥接")
    for key in ("project_id", "model_version_id", "run_id", "attempt_id", "dataset_id"):
        if not isinstance(pin[key], str):
            raise ValueError("初始化来源标识无效")
        try:
            if str(uuid.UUID(pin[key])) != pin[key]:
                raise ValueError("初始化来源标识无效")
        except ValueError as exc:
            raise ValueError("初始化来源标识无效") from exc
    if not isinstance(pin["model_name"], str) or not 0 < len(pin["model_name"]) <= 120:
        raise ValueError("初始化来源名称无效")
    for key in (
        "policy_digest",
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
        or not runtime
        or len(runtime) > 32
        or any(
            not isinstance(key, str)
            or not 0 < len(key) <= 64
            or (
                value is not None
                and (not isinstance(value, str) or not 0 < len(value) <= 200)
            )
            for key, value in runtime.items()
        )
    ):
        raise ValueError("初始化来源运行环境无效")
    image_digest = runtime.get("image_digest")
    if image_digest is not None and (
        not isinstance(image_digest, str)
        or not image_digest.startswith("sha256:")
        or not is_sha256(image_digest[7:])
    ):
        raise ValueError("初始化来源镜像摘要无效")
    evaluation = pin["evaluation"]
    expected_eval_keys = {"report_sha256", "report_digest", "validation_digest"} | (
        {"normalized_model_digest", "descriptor_digest", "weights_sha256"}
        if segmentation
        else set()
    )
    if (
        not isinstance(evaluation, dict)
        or set(evaluation) != expected_eval_keys
        or not all(is_sha256(value) for value in evaluation.values())
        or evaluation["validation_digest"] != pin["validation_digest"]
        or (segmentation and evaluation["weights_sha256"] != artifact["sha256"])
        or digest({key: value for key, value in pin.items() if key != "source_digest"})
        != pin["source_digest"]
    ):
        raise ValueError("初始化来源或评估证据摘要不匹配")
    return pin


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
    if normalize_task(pin["task"]) == SEGMENTATION and (
        not is_sha256(evidence.get("mask_head_tensor_digest"))
        or evidence["mask_head_tensor_digest"]
        != evidence.get("loaded_mask_head_tensor_digest")
        or type(evidence.get("mask_head_tensor_count")) is not int
        or evidence["mask_head_tensor_count"] <= 0
    ):
        raise ValueError("分割初始化未完整转移原生 mask head 权重")
