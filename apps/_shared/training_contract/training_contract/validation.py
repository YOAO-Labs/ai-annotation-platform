"""Strict JSON validation and native observation checks, with no framework imports."""

import math
from aap_task_contract import normalize_task
from pathlib import PurePath

from .catalog import FIXED, SEGMENTATION_FIXED, compatible_definitions, schema_identity
from .dataset import SEGMENTATION, data_contract, is_instance_rle

CANONICAL = ("epochs", "batch", "imgsz", "seed", "lr0", "patience")


def validate_value(value, rule):
    if "anyOf" in rule:
        for option in rule["anyOf"]:
            try:
                validate_value(value, option)
                return
            except ValueError:
                pass
        raise ValueError("类型或取值不符合任一允许形式")
    if "enum" in rule:
        if not any(
            type(value) is type(item) and value == item for item in rule["enum"]
        ):
            raise ValueError(f"必须选择 {rule['enum']}")
        return
    kind = rule["type"]
    types = {
        "null": type(None),
        "boolean": bool,
        "integer": int,
        "string": str,
        "array": list,
    }
    if kind == "number":
        valid = type(value) is int or (type(value) is float and math.isfinite(value))
    else:
        valid = type(value) is types[kind]
    if not valid:
        raise ValueError(f"必须为 {kind}，不接受隐式类型转换")
    if kind in {"number", "integer"}:
        if type(value) is float and not math.isfinite(value):
            raise ValueError("必须为有限数值")
        for key, predicate in (
            ("minimum", lambda bound: value >= bound),
            ("maximum", lambda bound: value <= bound),
            ("exclusiveMinimum", lambda bound: value > bound),
        ):
            if key in rule and not predicate(rule[key]):
                raise ValueError(f"不满足 {key}={rule[key]}")
        if "multipleOf" in rule and value % rule["multipleOf"] != 0:
            raise ValueError(f"必须为 {rule['multipleOf']} 的整数倍")
    if kind == "array":
        if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", 100):
            raise ValueError("列表长度超出允许范围")
        for item in value:
            validate_value(item, rule["items"])
        if rule.get("uniqueItems") and len(set(value)) != len(value):
            raise ValueError("列表元素不能重复")
        if rule.get("ordered") and value != sorted(value):
            raise ValueError("区间下界不能大于上界")


def resolve(framework, base_model, request):
    schemas = compatible_definitions(framework, base_model)
    schema = next(
        (
            item
            for item in schemas
            if request["schema_id"] == item["schema_id"]
            and request["schema_digest"] == item["schema_digest"]
        ),
        None,
    )
    if schema is None:
        raise ValueError("参数定义已变化或不属于当前模型，请重新读取目录")
    submitted = request["values"]
    if not isinstance(submitted, dict) or len(submitted) > 150:
        raise ValueError("参数必须为有界 JSON 对象")
    fields = {field["key"]: field for field in schema["fields"]}
    aliases = {
        alias: field["key"] for field in schema["fields"] for alias in field["aliases"]
    }
    user_values = {}
    for supplied, value in submitted.items():
        key = aliases.get(supplied, supplied)
        field = fields.get(key)
        if not field or field["status"] != "editable":
            raise ValueError(f"{supplied}：未知、平台保留或当前不适用的参数")
        if key in user_values:
            raise ValueError(f"{key}：别名与原生键不能同时提交")
        try:
            validate_value(value, field["rules"])
        except ValueError as exc:
            raise ValueError(f"{key}：{exc}") from exc
        user_values[key] = value
    effective = {
        field["key"]: field["default"]
        for field in schema["fields"]
        if field["status"] == "editable"
    }
    effective.update(user_values)
    origins = {
        key: "user" if key in user_values else "reviewed-default" for key in effective
    }
    fixed = {**FIXED[framework]}
    if framework == "ultralytics" and schema["model_family"] == "yolo26":
        fixed["nms"] = False
    if fields.get("pretrained", {}).get("status") == "editable":
        fixed.pop("pretrained", None)
    if normalize_task(schema["task"]) == SEGMENTATION:
        fixed.update(SEGMENTATION_FIXED)
    effective.update(fixed)
    origins.update({key: "platform" for key in fixed})
    inactive = {}
    if framework == "ultralytics":
        if effective["optimizer"] == "SGD" and effective["momentum"] <= 0:
            raise ValueError("SGD 的原生 Nesterov 要求 momentum > 0")
        warmup_limit = max(effective["epochs"] - 1, 0)
        if effective["warmup_epochs"] > warmup_limit:
            if "warmup_epochs" in user_values:
                raise ValueError("warmup_epochs 不能超过 epochs - 1")
            effective["warmup_epochs"] = warmup_limit
            origins["warmup_epochs"] = "dependency:epochs"
        if effective["warmup_epochs"] == 0:
            inactive.update(
                {
                    key: "预热关闭时不使用此参数"
                    for key in ("warmup_bias_lr", "warmup_momentum")
                }
            )
    if framework == "ultralytics" and effective["optimizer"] != "SGD":
        inactive["warmup_momentum"] = "当前优化器不使用 SGD 动量预热"
    if framework == "ultralytics" and effective["rect"]:
        for key in ("mosaic", "mixup", "cutmix"):
            inactive[key] = "矩形批训练会关闭该增强"
            effective[key] = 0.0
            origins[key] = "dependency:rect"
    if framework == "libreyolo":
        if effective["warmup_epochs"] == 0:
            inactive.update(
                {
                    key: "预热关闭时不使用此参数"
                    for key in ("warmup_lr_start", "warmup_momentum")
                }
            )
        if not effective["ema"]:
            inactive["ema_decay"] = "EMA 关闭时不使用该参数"
        if effective["optimizer"] != "sgd":
            inactive.update(
                {
                    key: "当前优化器不使用此参数"
                    for key in ("momentum", "nesterov", "warmup_momentum")
                }
            )
        elif effective["nesterov"] and effective["momentum"] <= 0:
            raise ValueError("Nesterov 要求 momentum > 0")
        if effective["scheduler"] != "linear":
            inactive["warmup_momentum"] = "该参数仅用于 linear 计划的 SGD 动量"
        if (
            effective["warmup_epochs"] > 0
            and effective["warmup_lr_start"] > effective["lr0"]
        ):
            if "warmup_lr_start" in user_values:
                raise ValueError("warmup_lr_start 不能大于 lr0")
            effective["warmup_lr_start"] = effective["lr0"]
            origins["warmup_lr_start"] = "dependency:lr0"
        if effective["mosaic_prob"] == 0:
            inactive.update(
                {
                    key: "YOLO9 仅在 Mosaic 分支使用此参数"
                    for key in (
                        "degrees",
                        "translate",
                        "mosaic_scale",
                        "shear",
                        "perspective",
                        "mixup_prob",
                    )
                }
            )
    tail = "close_mosaic" if framework == "ultralytics" else "no_aug_epochs"
    if effective[tail] >= effective["epochs"]:
        if tail in user_values:
            keys = (
                ("mosaic", "mixup", "cutmix")
                if framework == "ultralytics"
                else (
                    "mosaic_prob",
                    "mixup_prob",
                    "degrees",
                    "translate",
                    "mosaic_scale",
                    "shear",
                    "perspective",
                )
            )
            inactive.update({key: "关闭增强的尾段覆盖了整次训练" for key in keys})
        else:
            effective[tail] = max(0, effective["epochs"] - 1)
            origins[tail] = "dependency:epochs"
    for key in user_values.keys() & inactive.keys():
        raise ValueError(f"{key}：{inactive[key]}")
    return {
        "identity": schema_identity(schema),
        "user_values": user_values,
        "effective_values": effective,
        "origins": origins,
        "inactive_fields": inactive,
    }


def configuration_for_data_contract(configuration, contract):
    """Add bridge qualification without changing historical parameter schemas."""
    from copy import deepcopy

    if contract is None or not is_instance_rle(data_contract(contract)):
        return configuration
    result = deepcopy(configuration)
    result["effective_values"]["workers"] = 0
    result["origins"]["workers"] = "platform:ultralytics-instance-rle-1"
    return result


def verify_frozen(config):
    frozen = config["configuration"]
    identity = frozen["identity"]
    request = {
        "schema_id": identity["schema_id"],
        "schema_digest": identity["schema_digest"],
        "values": frozen["user_values"],
    }
    resolved = configuration_for_data_contract(
        resolve(config["framework"], config["base_model"]["id"], request),
        config.get("data_contract"),
    )
    if (
        resolved != frozen
        or config["adapter_revision"] != 2
        or config["framework_version"] != identity["framework_version"]
    ):
        raise ValueError(
            "Frozen parameter contract identity or effective values mismatch"
        )
    if config.get("initialization") and not resolved["effective_values"].get(
        "pretrained", True
    ):
        raise ValueError("随机初始化与项目权重初始化不能同时启用")
    if config.get("initialization"):
        from .initialization import validate_pin

        pin = validate_pin(config["initialization"])
        if (
            pin["framework"] != config["framework"]
            or pin["framework_version"] != config["framework_version"]
            or pin["base_model"] != config["base_model"]["id"]
            or pin["classes"] != config["classes"]
            or normalize_task(pin["task"]) != normalize_task(identity["task"])
        ):
            raise ValueError(
                "项目模型初始化与目标训练的框架版本、架构、任务或类别不一致"
            )
    if (
        identity["model_family"] == "yolo26"
        and normalize_task(identity["task"]) == SEGMENTATION
    ):
        values = resolved["effective_values"]
        elements = values["batch"] * len(config["classes"]) * values["imgsz"] ** 2
        if elements > 64_000_000:
            raise ValueError(
                f"语义辅助目标超出 64000000 元素上限：batch={values['batch']}、类别={len(config['classes'])}、imgsz={values['imgsz']}；请减小批大小或输入尺寸"
            )
    if config["parameters"] != {
        key: resolved["effective_values"][key] for key in CANONICAL
    }:
        raise ValueError(
            "Canonical parameters differ from the effective native configuration"
        )
    return resolved


def normalized(value):
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, tuple):
        return [normalized(item) for item in value]
    if isinstance(value, list):
        return [normalized(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalized(item) for key, item in value.items()}
    return value


def verify_native(config, observed, runtime_options):
    frozen = verify_frozen(config)
    if not isinstance(observed, dict) or not observed:
        raise ValueError("Native resolved configuration observation is required")
    allowed = (
        {"data", "model", "project", "name", "exist_ok", "resume"}
        if config["framework"] == "ultralytics"
        else {
            "data",
            "project",
            "name",
            "exist_ok",
            "resume",
            "num_classes",
            "size",
            "data_dir",
        }
    )
    if set(runtime_options) != allowed or set(runtime_options) & set(
        frozen["effective_values"]
    ):
        raise ValueError("Runtime materialization cannot override effective parameters")
    if (
        runtime_options["name"] != "framework"
        or runtime_options["exist_ok"] is not False
    ):
        raise ValueError("Native output ownership mismatch")
    if config["framework"] == "libreyolo" and (
        runtime_options["num_classes"] != len(config["classes"])
        or runtime_options["size"] != config["base_model"]["id"][-1].lower()
        or runtime_options["data_dir"] is not None
    ):
        raise ValueError("Native dataset/model derived identity mismatch")
    expected = {**native_values(frozen), **runtime_options}
    differences = []
    for key, value in expected.items():
        actual = normalized(observed.get(key))
        # Native device parsing can preserve integer GPU indexes as strings.
        if key == "device" and config["framework"] == "ultralytics":
            actual, value = str(actual), str(value)
        if key not in observed or actual != normalized(value):
            differences.append(key)
    if differences:
        raise ValueError(
            "Native resolved configuration mismatch: " + ", ".join(sorted(differences))
        )
    return {"verified": True, "checked_fields": sorted(expected), **frozen["identity"]}


def native_values(configuration):
    """Platform evaluation options are frozen but are not upstream train arguments."""
    return {
        key: value
        for key, value in configuration["effective_values"].items()
        if key != "evaluate_train"
    }
