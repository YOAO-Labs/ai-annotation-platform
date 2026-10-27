#!/usr/bin/env python3
"""Render complete training parameter tables from the shared offline contract.

Run with apps/api/.venv/bin/python, optionally passing --check. Only marked
blocks are replaced, so explanatory prose remains manually maintained. This
tool imports no training framework and needs only the Python standard library.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import re
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "apps/_shared/training_contract"),
    str(ROOT / "apps/_shared/task_contract"),
]

from training_contract import definition  # noqa: E402


LABELS = {
    "overlap_mask": "合并实例训练掩码",
    "mask_ratio": "训练掩码下采样比例",
    "dropout": "分类头 Dropout",
    "dnn": "OpenCV DNN 推理",
    "source": "预测输入来源",
    "vid_stride": "视频帧步长",
    "stream_buffer": "视频流缓冲",
    "visualize": "特征可视化",
    "augment": "预测时增强",
    "retina_masks": "原图分辨率推理掩码",
    "embed": "提取特征层",
    "show": "显示结果窗口",
    "save_frames": "保存视频帧",
    "save_txt": "保存预测文本",
    "save_conf": "保存预测置信度",
    "save_crop": "保存目标裁剪图",
    "show_labels": "显示类别名称",
    "show_conf": "显示置信度",
    "show_boxes": "显示检测框",
    "line_width": "绘图线宽",
    "format": "模型导出格式",
    "optimize": "导出优化",
    "dynamic": "动态导出尺寸",
    "simplify": "简化导出模型",
    "opset": "ONNX 算子集",
    "workspace": "导出工作空间",
    "pose": "姿态损失权重",
    "kobj": "关键点目标损失权重",
    "rle": "关键点 RLE 损失权重",
    "angle": "旋转角损失权重",
    "dlog": "深度对数损失权重",
    "dgrad": "深度梯度损失权重",
    "dlam": "深度损失组合权重",
    "copy_paste": "实例 Copy/Paste 概率",
    "copy_paste_mode": "Copy/Paste 来源模式",
    "auto_augment": "分类自动增强策略",
    "erasing": "分类随机擦除概率",
    "tracker": "视频跟踪器配置",
    "distill_loss_type": "蒸馏损失类型",
    "distill_mask_ratio": "蒸馏掩码比例",
    "distill_tau": "蒸馏温度",
    "distill_normalize": "蒸馏特征归一化",
    "average_best": "最佳检查点平均数量",
    "export_check": "训练后导出检查",
    "precise_bn": "精确 BN 统计批次数",
    "profile_then_stop": "性能分析后停止",
    "profile_warmup": "性能分析预热步数",
    "profile_steps": "性能分析采样步数",
    "profile_trace": "保存性能追踪",
    "profile_open": "打开性能追踪",
    "mixup": "其他路径的 MixUp 参数",
    "cutmix": "其他路径的 CutMix 参数",
    "scale": "其他路径的缩放区间",
    "crop_pct": "分类裁剪比例",
    "mask_downsample_ratio": "分割掩码下采样比例",
    "rot90": "旋转框直角旋转概率",
}
RUNTIME_VALUES = {
    "data": "冻结训练包的本地 YAML 路径",
    "model": "受控架构／初始化权重／续训检查点",
    "project": "本次执行的独立产物目录",
    "name": '`"framework"`',
    "exist_ok": "`false`",
    "resume": "首次 `false`；续训使用已核验检查点路径",
    "size": "由基础型号派生 `t/s/m/c`",
    "num_classes": "冻结的原生类别数量",
    "data_dir": "`null`，使用冻结 YAML 中的路径",
}
STATUS = {
    "editable": "可编辑",
    "platform-managed": "平台固定",
    "unqualified": "尚未验证",
    "inapplicable": "其他任务／模式",
}


def literal(value):
    return "`" + json.dumps(value, ensure_ascii=False, separators=(", ", ": ")) + "`"


def label(field, framework):
    key = field["key"]
    name = field["label"]
    if name == key:
        name = LABELS.get(key)
    if framework == "libreyolo" and key == "momentum":
        name = "SGD 动量"
    if not name:
        raise ValueError(f"Missing parameter label: {framework}.{key}")
    return f"`{key}`<br>{name}"


def rule_text(rule):
    if "anyOf" in rule:
        return " 或 ".join(rule_text(part) for part in rule["anyOf"])
    if "enum" in rule:
        return "、".join(literal(item) for item in rule["enum"])
    kind = rule["type"]
    if kind == "boolean":
        return "布尔值"
    if kind == "null":
        return "`null`"
    if kind == "array":
        count = f"{rule.get('minItems', 0)} 至 {rule.get('maxItems', 100)} 项"
        if rule.get("minItems") == rule.get("maxItems"):
            count = f"{rule['minItems']} 项"
        suffix = "；按升序排列" if rule.get("ordered") else ""
        return f"{count}数组，元素为 {rule_text(rule['items'])}{suffix}"
    bounds = []
    if "minimum" in rule:
        bounds.append(f"≥ {rule['minimum']}")
    if "exclusiveMinimum" in rule:
        bounds.append(f"> {rule['exclusiveMinimum']}")
    if "maximum" in rule:
        bounds.append(f"≤ {rule['maximum']}")
    suffix = f"；{rule['multipleOf']} 的倍数" if "multipleOf" in rule else ""
    return ("整数" if kind == "integer" else "数值") + "，" + "，".join(bounds) + suffix


def platform_value(field):
    if field["status"] == "platform-managed" and field["key"] in RUNTIME_VALUES:
        return RUNTIME_VALUES[field["key"]]
    return literal(field["default"])


def table(headers, rows):
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
        ]
        + ["| " + " | ".join(row) + " |" for row in rows]
    )


def render(framework, base_model):
    schema = definition(framework, base_model)
    counts = Counter(field["status"] for field in schema["fields"])
    blocks = []
    seen = set()
    for status in STATUS:
        fields = [field for field in schema["fields"] if field["status"] == status]
        blocks.append(f"### {STATUS[status]}（{counts[status]} 项）")
        rows = []
        for field in fields:
            key = field["key"]
            if key in seen:
                raise ValueError(f"Duplicate parameter: {framework}.{key}")
            seen.add(key)
            native = (
                "平台扩展，无原生项"
                if field["defined_in"] == "platform"
                else literal(field["native_default"])
            )
            if status == "editable":
                note = rule_text(field["rules"])
                if field["aliases"]:
                    note += "；别名 " + "、".join(
                        literal(alias) for alias in field["aliases"]
                    )
                platform = platform_value(field)
            elif status == "platform-managed":
                platform, note = platform_value(field), "由平台管理，不能覆盖"
            else:
                platform = "不开放"
                note = (
                    "需要额外训练、恢复或产物资格验证"
                    if status == "unqualified"
                    else "当前检测训练不使用，或属于其他路径"
                )
                if framework == "libreyolo" and key in {"cuda_graph", "compile"}:
                    platform, note = "执行器固定 `false`", "功能尚未开放"
            rows.append([label(field, framework), native, platform, note])
        blocks.append(
            table(["参数与用途", "仓库默认", "平台默认或处理", "范围与说明"], rows)
        )
    if seen != {field["key"] for field in schema["fields"]}:
        raise ValueError("Incomplete parameter table")
    return "\n\n".join(blocks)


def segmentation():
    detect = {
        field["key"]: field for field in definition("ultralytics", "yolo11n")["fields"]
    }
    rows = []
    for field in definition("ultralytics", "yolo11n-seg")["fields"]:
        before = detect[field["key"]]
        if (field["default"], field["status"], field["rules"]) == (
            before["default"],
            before["status"],
            before["rules"],
        ):
            continue
        previous = STATUS[before["status"]]
        if before["status"] in {"editable", "platform-managed"}:
            previous += "，" + platform_value(before)
        note = (
            "分割任务身份" if field["key"] == "task" else "受控分割的实例掩码或变换约束"
        )
        rows.append(
            [
                label(field, "ultralytics"),
                previous,
                STATUS[field["status"]] + "，" + platform_value(field),
                note,
            ]
        )
    rows.append(
        [
            "`workers`<br>数据加载进程数",
            "平台固定 `2`",
            "简单多边形 `2`；原生实例 RLE `0`",
            "原生 RLE 数据桥接进一步固定，不由用户覆盖",
        ]
    )
    return table(["参数", "检测", "实例分割", "原因"], rows)


def normalized(text):
    # Prettier aligns Markdown tables without changing their cell values.
    lines = []
    for line in text.strip().splitlines():
        if line.lstrip().startswith("|"):
            parts = [part.strip() for part in line.split("|")]
            line = "|".join(
                "---" if re.fullmatch(r":?-+:?", part) else part for part in parts
            )
        lines.append(line.strip())
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    directory = ROOT / "docs-site/user-guide/online-learning"
    updates = {
        "ultralytics.md": {
            "ultralytics": render("ultralytics", "yolo11n"),
            "segmentation": segmentation(),
        },
        "libreyolo.md": {"libreyolo": render("libreyolo", "libreyolo9t")},
    }
    failures = []
    for filename, blocks in updates.items():
        path = directory / filename
        text = path.read_text()
        for name, content in blocks.items():
            start = f"<!-- training-reference:{name}:start -->"
            end = f"<!-- training-reference:{name}:end -->"
            if text.count(start) != 1 or text.count(end) != 1:
                raise ValueError(f"Missing or repeated table markers in {filename}")
            prefix, rest = text.split(start)
            current, suffix = rest.split(end)
            if normalized(current) != normalized(content):
                failures.append(f"{filename}:{name}")
            text = prefix + start + "\n\n" + content + "\n\n" + end + suffix
        if not args.check:
            path.write_text(text)
    if args.check and failures:
        raise SystemExit(
            "Training parameter tables need regeneration: " + ", ".join(failures)
        )
    counts = {
        framework: len(definition(framework)["fields"])
        for framework in ("ultralytics", "libreyolo")
    }
    print(
        "Training parameter tables "
        + ("are in sync" if args.check else "generated")
        + f": {counts}; segmentation differences included"
    )


if __name__ == "__main__":
    main()
