"""rapidocr-backend 能力目录（SSOT）。

单一来源同时驱动 `/setup` 自报（main.py）与 `/predict` 路由解析（predictor.py），
避免两处漂移。详见 docs/plans/2026-06-29-v0.20.0-rapidocr-backend.md「能力分解」。

三个对外 model（映射平台现成任务族）：
- ``ocr-det`` 文本检测（detection，原子）：full_image → polygon 文本框，无属性。
- ``ocr-rec`` 文本识别（ocr，原子）：crop → text(+orientation)，内部跑 cls 方向校正。
- ``ocr-e2e`` 端到端 OCR（ocr，composite）：full_image → polygon + text + orientation。

变体轴：version(v5/v6) × size × lang。size 在 v5 是 mobile/server、v6 是 tiny/small/medium；
lang 只 universal(中英)/en，且 en 仅 v5-mobile、v6 仅 universal（非笛卡尔积，用 variant_combinations 表达）。
cls（方向）语言/版本无关，仅按 size 选 mobile/server 档，被 rec 与 e2e 内部共享。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

MODELS_DIR = os.environ.get("RAPIDOCR_MODEL_DIR", "/app/models")

DET_MODEL_ID = "ocr-det"
REC_MODEL_ID = "ocr-rec"
E2E_MODEL_ID = "ocr-e2e"

# ---- 组件权重文件（相对 MODELS_DIR），与 download_models.py 落盘布局一致 ----
_DET: dict[tuple[str, str], str] = {
    ("v5", "mobile"): "PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx",
    ("v5", "server"): "PP-OCRv5/det/ch_PP-OCRv5_det_server.onnx",
    ("v6", "tiny"): "PP-OCRv6/det/PP-OCRv6_det_tiny.onnx",
    ("v6", "small"): "PP-OCRv6/det/PP-OCRv6_det_small.onnx",
    ("v6", "medium"): "PP-OCRv6/det/PP-OCRv6_det_medium.onnx",
}
_CLS: dict[str, str] = {
    "mobile": "PP-OCRv5/cls/ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx",
    "server": "PP-OCRv5/cls/ch_PP-LCNet_x1_0_textline_ori_cls_server.onnx",
}
_REC: dict[tuple[str, str, str], str] = {
    ("v5", "mobile", "universal"): "PP-OCRv5/rec/ch_PP-OCRv5_rec_mobile.onnx",
    ("v5", "server", "universal"): "PP-OCRv5/rec/ch_PP-OCRv5_rec_server.onnx",
    ("v5", "mobile", "en"): "PP-OCRv5/rec/en_PP-OCRv5_rec_mobile.onnx",
    ("v6", "tiny", "universal"): "PP-OCRv6/rec/PP-OCRv6_rec_tiny.onnx",
    ("v6", "small", "universal"): "PP-OCRv6/rec/PP-OCRv6_rec_small.onnx",
    ("v6", "medium", "universal"): "PP-OCRv6/rec/PP-OCRv6_rec_medium.onnx",
}

# 合法 (version, size) for det / (version, size, lang) for rec·e2e（非笛卡尔积）。
_DET_COMBOS = list(_DET.keys())
_REC_COMBOS = list(_REC.keys())

# size → cls 档：tiny/small/mobile 配 mobile cls；medium/server 配 server cls。
_CLS_FOR_SIZE = {
    "mobile": "mobile",
    "tiny": "mobile",
    "small": "mobile",
    "server": "server",
    "medium": "server",
}

_OCR_VERSION = {"v5": "PP-OCRv5", "v6": "PP-OCRv6"}
# det lang_type：v5 用 ch 检测器、v6 用 multi 检测器（检测与目标语言无关）。
_DET_LANG = {"v5": "ch", "v6": "multi"}
# rec lang_type：universal 在 v5 是 ch（含中英）、v6 是 multi；en 走 en。
_REC_LANG = {
    ("v5", "universal"): "ch",
    ("v6", "universal"): "multi",
    ("v5", "en"): "en",
}


@dataclass(frozen=True)
class ResolvedEngine:
    """一次 /predict 解析出的引擎配置 + 运行开关。

    pool_key 由三组件路径 + device 决定：det/e2e 同 (version,size) 复用同一引擎，
    只是 use_* 不同。"""

    det_path: str
    cls_path: str
    rec_path: str
    det_meta: tuple[str, str, str]  # (ocr_version, model_type, lang_type)
    rec_meta: tuple[str, str, str]
    use_det: bool
    use_cls: bool
    use_rec: bool
    lang: str  # universal / en（写入 attributes.language；det 无关时为 ""）

    @property
    def pool_key(self) -> str:
        return f"{self.det_path}|{self.cls_path}|{self.rec_path}"


def _abs(rel: str) -> str:
    return os.path.join(MODELS_DIR, rel)


def resolve(model_id: str, variants: dict[str, str] | None) -> ResolvedEngine:
    """把 (model_id, model_variants) 解析为引擎配置。缺省 variant 用各能力默认档。"""
    v = variants or {}
    version = v.get("version", "v5")
    size = v.get("size", "mobile")
    lang = v.get("lang", "universal")

    if model_id == DET_MODEL_ID:
        if (version, size) not in _DET:
            raise ValueError(f"未知 det variant: version={version} size={size}")
        # det 原子只用 det，但 RapidOCR 构造需三件套 → cls/rec 取同档默认（不参与运行）。
        rec_lang = "universal"
        return _build(
            version, size, rec_lang, use_det=True, use_cls=False, use_rec=False, lang=""
        )

    if model_id in (REC_MODEL_ID, E2E_MODEL_ID):
        if (version, size, lang) not in _REC:
            raise ValueError(
                f"未知 rec/e2e variant: version={version} size={size} lang={lang}"
            )
        if model_id == REC_MODEL_ID:
            # rec 原子吃 crop：跳过 det（构造仍需 det 路径，不运行），跑 cls+rec。
            return _build(
                version,
                size,
                lang,
                use_det=False,
                use_cls=True,
                use_rec=True,
                lang=lang,
            )
        # e2e：det→cls→rec 全开。
        return _build(
            version, size, lang, use_det=True, use_cls=True, use_rec=True, lang=lang
        )

    raise ValueError(f"未知 model_id: {model_id}")


def _build(
    version: str,
    size: str,
    rec_lang: str,
    *,
    use_det: bool,
    use_cls: bool,
    use_rec: bool,
    lang: str,
) -> ResolvedEngine:
    det_rel = _DET[(version, size)]
    cls_rel = _CLS[_CLS_FOR_SIZE[size]]
    rec_rel = _REC[(version, size, rec_lang)]
    ocr_ver = _OCR_VERSION[version]
    return ResolvedEngine(
        det_path=_abs(det_rel),
        cls_path=_abs(cls_rel),
        rec_path=_abs(rec_rel),
        det_meta=(ocr_ver, size, _DET_LANG[version]),
        rec_meta=(ocr_ver, size, _REC_LANG[(version, rec_lang)]),
        use_det=use_det,
        use_cls=use_cls,
        use_rec=use_rec,
        lang=lang,
    )


# ---------------- /setup 能力自报 ----------------


# variant option 形态：{value, label}（协议 InstanceVariantOption）；轴形态：{key, title, variants}。
def _version_axis(combos: list[tuple]) -> dict:
    versions = sorted({c[0] for c in combos})
    return {
        "key": "version",
        "title": "PP-OCR 版本",
        "variants": [{"value": v, "label": v.upper()} for v in versions],
    }


def _size_axis(combos: list[tuple]) -> dict:
    # 按出现顺序去重，保留 mobile/server/tiny/small/medium 的语义顺序。
    order = ["mobile", "server", "tiny", "small", "medium"]
    sizes = sorted({c[1] for c in combos}, key=order.index)
    return {
        "key": "size",
        "title": "尺寸 / 精度档",
        "variants": [{"value": s, "label": s} for s in sizes],
    }


def _lang_axis() -> dict:
    return {
        "key": "lang",
        "title": "语言",
        "variants": [
            {"value": "universal", "label": "通用(中英)"},
            {"value": "en", "label": "英文"},
        ],
    }


_ATTR_TEXT = {"key": "text", "label": "识别文本", "type": "text"}
# select options 必须是 {value,label} 对象(协议 output_attribute_schema §3.x,后端
# AttributeFieldOption 亦然)。value 与 /predict 实际写入的 attributes 值严格对齐:
# orientation = cls 标签 "0"/"180";language = ResolvedEngine.lang "universal"/"en"。
# 曾误写成纯字符串数组 ["0","180"],项目设置「从 ML Backend 预填」取 o.value 得 undefined,
# 下拉选项对不上、且新版 validateAttributeFields 对 undefined.trim() 崩溃。
_ATTR_ORIENT = {
    "key": "orientation",
    "label": "方向",
    "type": "select",
    "options": [{"value": "0", "label": "0°"}, {"value": "180", "label": "180°"}],
}
_ATTR_LANG = {
    "key": "language",
    "label": "语言",
    "type": "select",
    "options": [
        {"value": "universal", "label": "通用(中英)"},
        {"value": "en", "label": "英文"},
    ],
}

# ---- 运行时可调阈值（透传 RapidOCR __call__/update_params；缺省=引擎默认）。----
# 平台据此渲染阈值滑块(model.params.properties)，/predict 从 context.params 读取并应用。
# RUNTIME_PARAM_DEFAULTS 是这三个旋钮的单一真值：既是 schema 的 default，也是 predictor
# 缺参时显式回落的值。必须显式回落（而非传 None）——RapidOCR.update_params 对 None 是跳过、
# 不重置，而 det/rec/e2e 同 variant 共享同一池化引擎，缺参传 None 会让上一次请求的阈值粘在
# 引擎上、污染后续请求（含跨原子类型、跨项目）。
RUNTIME_PARAM_DEFAULTS = {"text_score": 0.5, "box_thresh": 0.5, "unclip_ratio": 1.6}

# 注：text_score 只在「同时有 det+rec」的 e2e 路径生效（build_final_output 的 rec-only
# 分支提前 return、不过 filter_by_text_score），故 rec 原子不暴露任何可调阈值。
_PARAM_TEXT_SCORE = {
    "type": "number",
    "title": "文本置信度阈值",
    "minimum": 0.0,
    "maximum": 1.0,
    "default": RUNTIME_PARAM_DEFAULTS["text_score"],
    "description": "识别置信度低于此值的文本被丢弃（调低=保留更多但更杂）。",
}
_PARAM_BOX_THRESH = {
    "type": "number",
    "title": "检测框阈值",
    "minimum": 0.0,
    "maximum": 1.0,
    "default": RUNTIME_PARAM_DEFAULTS["box_thresh"],
    "description": "文本检测框得分低于此值被过滤（调低=检出更多更碎的框）。",
}
_PARAM_UNCLIP_RATIO = {
    "type": "number",
    "title": "检测框扩张比",
    "minimum": 1.0,
    "maximum": 3.0,
    "default": RUNTIME_PARAM_DEFAULTS["unclip_ratio"],
    "description": "检测框向外扩张比例（调大=框更松、更易包全文字）。",
}


def _det_params() -> dict:
    return {
        "type": "object",
        "properties": {
            "box_thresh": _PARAM_BOX_THRESH,
            "unclip_ratio": _PARAM_UNCLIP_RATIO,
        },
    }


def _e2e_params() -> dict:
    return {
        "type": "object",
        "properties": {
            "box_thresh": _PARAM_BOX_THRESH,
            "unclip_ratio": _PARAM_UNCLIP_RATIO,
            "text_score": _PARAM_TEXT_SCORE,
        },
    }


def _det_entry() -> dict:
    return {
        "id": DET_MODEL_ID,
        "display_name": "RapidOCR · 文本检测（原子）",
        "task": "detect",
        "model_family": "rapidocr",
        "infra": "onnx",
        "composition": "atom",
        "is_interactive": False,
        "supported_prompts": ["none"],
        "supported_inputs": ["full_image"],
        "supported_geometric_outputs": ["polygon"],
        "params": _det_params(),
        "supported_variants": [_version_axis(_DET_COMBOS), _size_axis(_DET_COMBOS)],
        "variant_combinations": [list(c) for c in _DET_COMBOS],
        "default_variants": {"version": "v5", "size": "mobile"},
        "resource_profile": {"device": _device(), "batchable": True},
    }


def _rec_entry() -> dict:
    return {
        "id": REC_MODEL_ID,
        "display_name": "RapidOCR · 文本识别（原子）",
        "task": "ocr",
        "model_family": "rapidocr",
        "infra": "onnx",
        "composition": "atom",  # 内部含 cls 方向校正
        "is_interactive": False,
        "supported_prompts": ["none"],
        "supported_inputs": ["crop"],
        "supported_geometric_outputs": ["polygon"],
        "output_attribute_types": ["text", "orientation", "language"],
        "output_attribute_schema": [_ATTR_TEXT, _ATTR_ORIENT, _ATTR_LANG],
        # rec 原子无可调阈值：text_score 在 rec-only 路径是 no-op（见上方注释）。
        "supported_variants": [
            _version_axis(_REC_COMBOS),
            _size_axis(_REC_COMBOS),
            _lang_axis(),
        ],
        "variant_combinations": [list(c) for c in _REC_COMBOS],
        "default_variants": {"version": "v5", "size": "mobile", "lang": "universal"},
        "resource_profile": {"device": _device(), "batchable": True},
    }


def _e2e_entry() -> dict:
    return {
        "id": E2E_MODEL_ID,
        "display_name": "RapidOCR · 端到端 OCR",
        "task": "ocr",
        "model_family": "rapidocr",
        "infra": "onnx",
        "composition": "composite",  # 内部 det→cls→rec
        "is_interactive": False,
        "supported_prompts": ["none"],
        "supported_inputs": ["full_image"],
        "supported_geometric_outputs": ["polygon"],
        "output_attribute_types": ["text", "orientation", "language"],
        "output_attribute_schema": [_ATTR_TEXT, _ATTR_ORIENT, _ATTR_LANG],
        "params": _e2e_params(),
        "supported_variants": [
            _version_axis(_REC_COMBOS),
            _size_axis(_REC_COMBOS),
            _lang_axis(),
        ],
        "variant_combinations": [list(c) for c in _REC_COMBOS],
        "default_variants": {"version": "v5", "size": "mobile", "lang": "universal"},
        "resource_profile": {"device": _device(), "batchable": True},
    }


def _device() -> str:
    return os.environ.get("RAPIDOCR_DEVICE", "gpu")


def model_entries() -> list[dict]:
    return [_det_entry(), _rec_entry(), _e2e_entry()]
