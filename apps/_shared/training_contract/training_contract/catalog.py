"""Reviewed YOLO11/YOLO9 detection inventory; upstream existence is not qualification."""

from copy import deepcopy
import hashlib
import json
from importlib.resources import files
from aap_task_contract import legacy_training_task, normalize_task
from .dataset import DETECTION, SEGMENTATION, INSTANCE_RLE_LIMITS
from .models import (
    MODEL_SPECS,
    ULTRALYTICS_VERSION,
    LEGACY_ULTRALYTICS_VERSION,
    model_spec,
    supports_version,
)

ADAPTERS = {
    "ultralytics": {
        "version": ULTRALYTICS_VERSION,
        "revision": 1,
        "model_registration": True,
    },
    "libreyolo": {"version": "1.6.0", "revision": 1, "model_registration": True},
}

GROUPS = {
    "basic": "基础训练",
    "optimizer": "优化器与学习率",
    "augmentation": "数据增强",
    "loss": "损失函数",
    "finetuning": "微调",
    "evaluation": "验证与产物",
    "performance": "性能与复现",
    "platform": "平台与执行",
    "other": "其他任务与模式",
}


def number(low, high, *, integer=False, positive=False, multiple=None):
    rule = {"type": "integer" if integer else "number", "maximum": high}
    rule["exclusiveMinimum" if positive else "minimum"] = low
    if multiple:
        rule["multipleOf"] = multiple
    return rule


def enum(*values):
    return {"enum": list(values)}


BOOL = {"type": "boolean"}
PROB = number(0, 1)
FREEZE = {
    "anyOf": [
        {"type": "null"},
        number(0, 9, integer=True),
        {
            "type": "array",
            "items": number(0, 9, integer=True),
            "minItems": 1,
            "maxItems": 10,
            "uniqueItems": True,
        },
    ]
}
SCALE_PAIR = {
    "type": "array",
    "items": number(0, 3, positive=True),
    "minItems": 2,
    "maxItems": 2,
    "ordered": True,
}

COMMON = {
    "epochs": ("训练轮次", "basic", number(1, 1000, integer=True), 50),
    "batch": ("固定批大小", "basic", number(1, 64, integer=True), 8),
    "imgsz": ("输入尺寸", "basic", number(128, 1280, integer=True, multiple=32), 640),
    "seed": ("随机种子", "performance", number(0, 2147483647, integer=True), 42),
    "patience": ("早停耐心轮次", "basic", number(0, 1000, integer=True), 50),
    "lr0": ("初始学习率", "optimizer", number(0, 0.1, positive=True), 0.01),
    "momentum": ("动量 / Adam β1", "optimizer", number(0, 0.9999), None),
    "weight_decay": ("权重衰减", "optimizer", number(0, 0.1), None),
    "warmup_momentum": ("预热动量", "optimizer", PROB, None),
    "nbs": ("标称批大小（梯度累积）", "optimizer", number(1, 256, integer=True), None),
    "degrees": ("随机旋转角度", "augmentation", number(0, 180), None),
    "translate": ("随机平移比例", "augmentation", PROB, None),
    "shear": ("随机剪切角度", "augmentation", number(0, 45), None),
    "perspective": ("透视幅度", "augmentation", number(0, 0.001), None),
    "flipud": ("上下翻转概率", "augmentation", PROB, None),
}
ULTRA = {
    "optimizer": ("优化器", "optimizer", enum("SGD", "Adam", "AdamW"), "SGD"),
    "lrf": ("最终学习率比例", "optimizer", PROB, None),
    "warmup_epochs": ("预热轮次", "optimizer", number(0, 100), None),
    "warmup_bias_lr": ("偏置预热学习率", "optimizer", number(0, 1), None),
    "cos_lr": ("余弦学习率计划", "optimizer", BOOL, None),
    "box": ("框回归损失权重", "loss", number(0, 100, positive=True), None),
    "cls": ("分类损失权重", "loss", number(0, 100, positive=True), None),
    "cls_pw": ("类别正样本加权", "loss", PROB, None),
    "dfl": ("分布焦点损失权重", "loss", number(0, 100, positive=True), None),
    "rect": ("矩形批训练", "augmentation", BOOL, None),
    "close_mosaic": (
        "末尾关闭 Mosaic 的轮次",
        "augmentation",
        number(0, 1000, integer=True),
        None,
    ),
    "multi_scale": ("多尺度训练幅度", "augmentation", number(0, 0.5), None),
    "hsv_h": ("色相扰动", "augmentation", PROB, None),
    "hsv_s": ("饱和度扰动", "augmentation", PROB, None),
    "hsv_v": ("明度扰动", "augmentation", PROB, None),
    "scale": (
        "随机缩放幅度或区间",
        "augmentation",
        {"anyOf": [PROB, SCALE_PAIR]},
        None,
    ),
    "fliplr": ("左右翻转概率", "augmentation", PROB, None),
    "bgr": ("通道顺序交换概率", "augmentation", PROB, None),
    "mosaic": ("Mosaic 概率", "augmentation", PROB, None),
    "mixup": ("MixUp 概率", "augmentation", PROB, None),
    "cutmix": ("CutMix 概率", "augmentation", PROB, None),
}
LIBRE = {
    "optimizer": ("优化器", "optimizer", enum("sgd", "adam", "adamw"), "sgd"),
    "nesterov": ("Nesterov 动量", "optimizer", BOOL, None),
    "scheduler": ("学习率计划", "optimizer", enum("linear", "cos", "warmcos"), None),
    "warmup_epochs": ("预热轮次", "optimizer", number(0, 100, integer=True), None),
    "warmup_lr_start": ("预热起始学习率", "optimizer", number(0, 0.1), None),
    "no_aug_epochs": (
        "末尾关闭增强的轮次",
        "augmentation",
        number(0, 1000, integer=True),
        None,
    ),
    "min_lr_ratio": ("最低学习率比例", "optimizer", PROB, None),
    "mosaic_prob": ("Mosaic 概率", "augmentation", PROB, None),
    "mixup_prob": ("MixUp 概率", "augmentation", PROB, None),
    "hsv_prob": ("HSV 扰动概率", "augmentation", PROB, None),
    "flip_prob": ("左右翻转概率", "augmentation", PROB, None),
    "mosaic_scale": ("Mosaic 缩放区间", "augmentation", SCALE_PAIR, None),
    "ema": ("指数移动平均权重", "optimizer", BOOL, None),
    "ema_decay": ("EMA 衰减系数", "optimizer", number(0, 1, positive=True), None),
    "min_samples": (
        "每轮数据集样本数下限",
        "basic",
        number(0, 20000, integer=True),
        None,
    ),
    "class_balanced": ("按类别平衡采样", "basic", BOOL, None),
    "max_labels": (
        "每张图片最大训练框数",
        "basic",
        number(1, 1000, integer=True),
        None,
    ),
    "aux_weight": ("辅助检测头损失权重", "loss", number(0, 10), None),
    "letterbox_pad": (
        "Letterbox 填充位置",
        "augmentation",
        enum("topleft", "center"),
        "center",
    ),
    "val_loss": ("计算验证损失", "evaluation", BOOL, None),
}

MANAGED = {
    "ultralytics": "task mode model data device workers project name exist_ok pretrained cls_remap save save_period resume amp cache deterministic fraction verbose val split conf iou max_det save_json plots profile cfg nms single_cls classes agnostic_nms".split(),
    "libreyolo": "size num_classes data data_dir device sync_bn amp amp_dtype project name exist_ok save_period eval_interval max_det eval_max_det faster_coco_eval save_plots plot_samples workers cache resume log_interval allow_download_scripts single_cls classes".split(),
}
UNQUALIFIED = {
    "ultralytics": "time compile channels_last quantize distill_model dis freeze".split(),
    "libreyolo": "cuda_graph compile distill_model dis distill_loss_type distill_mask_ratio distill_tau distill_normalize average_best export_check precise_bn profile profile_then_stop profile_warmup profile_steps profile_trace profile_open freeze".split(),
}
OTHER = {
    "ultralytics": "overlap_mask mask_ratio dropout pose kobj rle angle dlog dgrad dlam copy_paste copy_paste_mode auto_augment erasing retina_masks dnn source vid_stride stream_buffer visualize augment embed show save_frames save_txt save_conf save_crop show_labels show_conf show_boxes line_width format optimize dynamic simplify opset workspace tracker".split(),
    "libreyolo": "mask_downsample_ratio copy_paste copy_paste_mode rot90 auto_augment erasing cutmix scale crop_pct cls_pw class_weights lora mixup_scale mixup".split(),
}
FIXED = {
    "ultralytics": {
        "task": "detect",
        "mode": "train",
        "device": 0,
        "workers": 2,
        "amp": False,
        "cache": False,
        "deterministic": True,
        "save": True,
        "save_period": -1,
        "plots": False,
        "pretrained": True,
        "cls_remap": True,
        "val": True,
        "split": "val",
        "single_cls": False,
        "classes": None,
        "fraction": 1.0,
        "profile": False,
        "save_json": False,
        "conf": 0.001,
        "iou": 0.7,
        "max_det": 300,
        "agnostic_nms": False,
        "nms": None,
        "verbose": True,
        "cfg": None,
    },
    "libreyolo": {
        "device": "cuda:0",
        "workers": 2,
        "amp": False,
        "cache": False,
        "amp_dtype": "float16",
        "sync_bn": False,
        "eval_interval": 1,
        "save_period": 0,
        "save_plots": False,
        "max_det": 300,
        "eval_max_det": 100,
        "faster_coco_eval": False,
        "plot_samples": 8,
        "log_interval": 10,
        "allow_download_scripts": False,
        "single_cls": False,
        "classes": None,
    },
}

LABELS = {
    "task": "任务类型",
    "mode": "运行模式",
    "model": "初始化权重",
    "data": "固定数据集",
    "data_dir": "数据目录",
    "device": "执行设备",
    "workers": "数据加载进程数",
    "project": "输出根目录",
    "name": "输出子目录",
    "exist_ok": "允许已有目录",
    "save": "保存权重",
    "save_period": "周期保存",
    "resume": "断点恢复",
    "amp": "混合精度",
    "cache": "数据缓存",
    "deterministic": "确定性算法",
    "pretrained": "预训练初始化",
    "cls_remap": "类别重映射",
    "fraction": "数据使用比例",
    "verbose": "详细日志",
    "val": "每轮验证",
    "split": "验证分区",
    "conf": "评估置信度阈值",
    "iou": "评估 NMS 阈值",
    "max_det": "最大检测数",
    "save_json": "导出评测 JSON",
    "plots": "原生绘图",
    "profile": "性能分析",
    "cfg": "外部配置路径",
    "nms": "模型 NMS",
    "single_cls": "单类别模式",
    "classes": "类别过滤",
    "agnostic_nms": "类别无关 NMS",
    "size": "模型规格",
    "num_classes": "类别数量",
    "sync_bn": "同步批归一化",
    "amp_dtype": "混合精度类型",
    "eval_interval": "验证间隔",
    "eval_max_det": "COCO 评估检测数",
    "faster_coco_eval": "加速 COCO 评估",
    "save_plots": "原生绘图",
    "plot_samples": "绘图样本数",
    "log_interval": "日志间隔",
    "allow_download_scripts": "数据下载脚本",
    "compile": "编译模型",
    "cuda_graph": "CUDA Graph",
    "time": "按时间终止",
    "channels_last": "通道布局",
    "quantize": "量化训练",
    "distill_model": "蒸馏教师模型",
    "dis": "蒸馏损失权重",
    "mixup_scale": "MixUp 缩放区间",
    "lora": "LoRA",
    "cls_pw": "类别正样本加权",
    "class_weights": "类别加权",
    "freeze": "冻结层或模块（需验证具体命中）",
}

SEGMENTATION_FIXED = {
    "task": "segment",
    "overlap_mask": False,
    "mask_ratio": 1,
    "copy_paste": 0.0,
    "copy_paste_mode": "flip",
    "degrees": 0.0,
    "translate": 0.0,
    "scale": 0.0,
    "shear": 0.0,
    "perspective": 0.0,
    "flipud": 0.0,
    "multi_scale": 0.0,
    "rect": False,
    "mosaic": 0.0,
    "mixup": 0.0,
    "cutmix": 0.0,
    "close_mosaic": 0,
}


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def definition(
    framework, base_model=None, *, legacy=False, revision=None, version=None
):
    if framework not in ADAPTERS:
        raise ValueError("未知训练框架")
    if (
        framework == "libreyolo"
        and base_model is not None
        and revision is None
        and not legacy
    ):
        model_spec(framework, base_model)
    revision = (
        2
        if legacy
        else revision
        or (
            (4 if version == LEGACY_ULTRALYTICS_VERSION else 5)
            if framework == "ultralytics"
            else 3
        )
    )
    if revision not in ({2, 3, 4, 5} if framework == "ultralytics" else {2, 3}):
        raise ValueError("Unknown training parameter schema revision")
    legacy = revision == 2
    inventory = json.loads(
        files(__package__)
        .joinpath(
            "native_inventory_current.json"
            if framework == "ultralytics" and revision >= 5
            else "native_inventory.json"
        )
        .read_text()
    )[framework]
    if version is not None and version != inventory["version"]:
        raise ValueError("参数定义与冻结框架版本不匹配")
    family = "yolo11" if framework == "ultralytics" else "yolo9"
    segmentation = (
        framework == "ultralytics" and (base_model or "").lower() == "yolo11n-seg"
    )
    if framework == "ultralytics" and revision >= 5:
        spec = model_spec(framework, base_model or "yolo11n")
        family, segmentation = spec.family, spec.task == SEGMENTATION
    elif base_model is not None and base_model.lower() not in (
        {"yolo11n", "yolo11s", "yolo11m", "yolo11l", "yolo11x", "yolo11n-seg"}
        if family == "yolo11"
        else {"libreyolo9t", "libreyolo9s", "libreyolo9m", "libreyolo9c", "libreyolo9e"}
    ):
        raise ValueError("该基础模型家族尚未通过参数契约验证")
    reviewed = {**COMMON, **(ULTRA if framework == "ultralytics" else LIBRE)}
    if framework == "ultralytics" and revision >= 4:
        reviewed["pretrained"] = ("使用预训练权重", "basic", BOOL, True)
    result = {
        "schema_id": f"{framework}-{inventory['version']}-{family}-{'segment' if segmentation else 'detect'}-{revision}",
        "framework": framework,
        "framework_version": inventory["version"],
        "adapter_revision": 2,
        "task": (
            legacy_training_task(SEGMENTATION if segmentation else DETECTION)
            if legacy
            else (SEGMENTATION if segmentation else DETECTION)
        ),
        "model_family": family,
        "source": inventory["source"],
        "source_sha256": inventory["source_sha256"],
        "groups": GROUPS,
        "fields": [],
    }
    for key, native in inventory["fields"].items():
        field = {
            "key": key,
            **native,
            "native_default": native["default"],
            "aliases": [],
        }
        if segmentation and key in SEGMENTATION_FIXED:
            field.update(
                label=LABELS.get(key, reviewed.get(key, (key,))[0]),
                group="platform",
                status="platform-managed",
                rules={},
                default=SEGMENTATION_FIXED[key],
                reason="单多边形分割资格固定的 mask 或变换语义，不能提交覆盖",
            )
        elif key in reviewed:
            label, group, rule, default = reviewed[key]
            field.update(
                label=label,
                group=group,
                status="editable",
                rules=deepcopy(rule),
                default=native["default"] if default is None else default,
                reason="已验证当前模型家族的检测训练路径；取值范围按平台资格收敛",
            )
            if key == "nbs" and framework == "libreyolo":
                field["rules"] = {"anyOf": [{"type": "null"}, field["rules"]]}
            if segmentation:
                field["reason"] = (
                    "已验证 YOLO11n 单多边形分割原生训练路径；取值范围按平台资格收敛"
                )
        elif key in MANAGED[framework]:
            field.update(
                label=LABELS.get(key, key),
                group="platform",
                status="platform-managed",
                rules={},
                default=FIXED[framework].get(key, native["default"]),
                reason="由平台固定数据、设备、评测或产物协议管理，不能提交覆盖",
            )
        elif key in UNQUALIFIED[framework]:
            field.update(
                label=LABELS.get(key, key),
                group="performance",
                status="unqualified",
                rules={},
                reason="原生功能需要额外执行、恢复或产物验证；当前不可编辑",
            )
        elif key in OTHER[framework]:
            field.update(
                label=LABELS.get(key, key),
                group="other",
                status="inapplicable",
                rules={},
                reason="其他任务/模式专用，或当前检测路径不读取该值",
            )
        else:
            raise ValueError(f"Unreviewed native field: {framework}.{key}")
        if framework == "libreyolo" and key == "mixup_prob":
            field["aliases"] = ["mixup"]
        result["fields"].append(field)
    if framework == "ultralytics" and revision >= 4:
        result["fields"].append(
            {
                "key": "evaluate_train",
                "label": "逐轮评估训练集",
                "group": "evaluation",
                "status": "editable",
                "rules": dict(BOOL),
                "default": False,
                "native_default": None,
                "native_type": "bool",
                "defined_in": "platform",
                "aliases": [],
                "reason": "平台每轮额外调用原生评估器计算训练集 Precision、Recall 和 mAP，关闭增强；增加耗时，不参与最佳权重选择",
            }
        )
        next(field for field in result["fields"] if field["key"] == "pretrained")[
            "reason"
        ] = "开启时使用预训练基础权重；关闭时仅使用模型架构随机初始化，与项目权重初始化互斥"
    if framework == "ultralytics" and revision >= 5:
        for field in result["fields"]:
            if field["status"] == "editable":
                field["reason"] = (
                    f"受控 {family} {'实例分割' if segmentation else '目标检测'}训练参数；取值按平台契约收敛"
                    if field["key"] not in {"pretrained", "evaluate_train"}
                    else field["reason"]
                )
            if family == "yolo26" and field["key"] == "dfl":
                field.update(
                    status="inapplicable",
                    rules={},
                    reason="该模型的 reg_max=1，不使用 DFL 损失增益",
                )
            if family == "yolo26" and field["key"] == "nms":
                field["default"] = False
                field["reason"] = "固定使用原生端到端检测分支，评估与推理保持相同语义"
            if family == "yolo26" and field["key"] == "iou":
                field["reason"] = "端到端分支不执行普通 NMS；保留原生评估匹配配置"
            if family == "yolo26" and segmentation and field["key"] == "batch":
                field["default"] = 1
    result["schema_digest"] = digest(result)
    return result


def definitions(*, legacy=False):
    if legacy:
        return [definition(framework, legacy=True) for framework in ADAPTERS] + [
            definition("ultralytics", "yolo11n-seg", legacy=True)
        ]
    return [
        definition("ultralytics", f"{family}n{suffix}")
        for family in ("yolov8", "yolo11", "yolo26")
        for suffix in ("", "-seg")
    ] + [definition("libreyolo")]


def schema_identity(schema):
    return {
        key: schema[key]
        for key in (
            "schema_id",
            "schema_digest",
            "framework",
            "framework_version",
            "adapter_revision",
            "task",
            "model_family",
        )
    }


def supported_identities(framework_versions=None):
    result = []
    historical = [
        definition("ultralytics", base, revision=revision)
        for base in ("yolo11n", "yolo11n-seg")
        for revision in (4, 3)
    ]
    for schema in [*definitions(), *historical, *definitions(legacy=True)]:
        if (
            framework_versions is not None
            and framework_versions.get(schema["framework"])
            != schema["framework_version"]
        ):
            continue
        identity = schema_identity(schema)
        result.append(identity)
        if normalize_task(schema["task"]) == SEGMENTATION:
            continue
        result.append(
            {
                key: (1 if key == "adapter_revision" else value)
                for key, value in identity.items()
                if key not in {"schema_id", "schema_digest"}
            }
        )
    return result


def definition_for_identity(framework, base_model, identity):
    """Select an exact frozen schema; aliases never change its hashed content."""
    for schema in compatible_definitions(framework, base_model):
        if identity == schema_identity(schema):
            return schema
    raise ValueError("未知或不匹配的冻结参数定义")


def compatible_definitions(framework, base_model):
    """Keep historical hashed parameter schemas available for frozen experiments."""
    result = []
    for revision in (5, 4, 3, 2) if framework == "ultralytics" else (3, 2):
        try:
            result.append(definition(framework, base_model, revision=revision))
        except ValueError:
            continue
    return result


def instance_rle_capability(
    framework_version=LEGACY_ULTRALYTICS_VERSION, base_model=None
):
    """Explicit first-revision consumer bounds, separate from old model schemas."""
    result = {
        "bridge": "ultralytics-instance-rle-1",
        "framework": "ultralytics",
        "version": framework_version,
        "models": [
            spec.id
            for spec in MODEL_SPECS
            if spec.task == SEGMENTATION and supports_version(spec, framework_version)
        ],
        "limits": dict(INSTANCE_RLE_LIMITS),
        "rasterizer": "aap-pixel-center-even-odd-1",
        "transform": "aap-letterbox-nearest-1",
        "loss_mask_resize": "truth-full-input-plane;prototype-bilinear-upsample-align-corners-false",
        "validation_mask_resize": "torch.bilinear-align-corners-false-threshold-0.5-to-prototype-plane",
    }
    if framework_version not in {LEGACY_ULTRALYTICS_VERSION, ULTRALYTICS_VERSION}:
        raise ValueError("该框架版本没有已登记的实例掩码消费者")
    if base_model is not None:
        spec = model_spec("ultralytics", base_model)
        if spec.id not in result["models"]:
            raise ValueError("该型号没有匹配的实例掩码消费者")
        result["models"] = [spec.id]
        if spec.end2end:
            result["semantic_target"] = "smallest-covering-instance-first-tie-1"
            result["semantic_plane_elements"] = 64_000_000
    return result
