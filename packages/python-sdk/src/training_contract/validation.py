"""Strict JSON value validation and canonical normalization without framework imports."""

import math
from pathlib import PurePath

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
