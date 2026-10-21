"""ML Backend 能力快照抽取 + 模态派生.

平台对 backend「能力 / 模态」的持久化感知统一在此派生:
- `extract_capabilities`: 从 backend `/setup` 响应抽出平台关心的能力快照。
- `derive_modalities`: 由能力快照派生平台视图概念「模态」(image / video / lidar)。

能力快照随 `services.ml_backend.check_health` 写入 `ml_backends.health_meta["capabilities"]`
(HealthMeta schema `extra="allow"`, 无 alembic 迁移)。

v0.14.9 · 能力声明协议 v2 (多模型目录 + infra):
- `/setup` 顶层新增 `infra` + `models[]`; 能力声明下沉到 model 粒度。
- 一个 backend 暴露 N 个 model 条目, 每个自带 task / infra / 几何 / variants。
- 向后兼容: 老 backend (无 `models[]`) 由顶层能力字段合成「隐式单 model」。
- 同时保留顶层「扁平并集」字段 (supported_prompts / ... / modalities),
  让现有消费方 (ToolDock / 绑定校验 / 列表展示) 零回归。
"""

from __future__ import annotations

from aap_inference_contract.capabilities import CapabilityLimits

from aap_task_contract import normalize_task

from aap_protocol_v2.lifecycle import canonical_managed_lifecycle_capabilities

# 受控词表与 task→默认几何统一由 capability_registry SSOT 派生,
# 协议元数据 (label / summary / suggested_backends) 同源, 供
# `GET /v1/ml-capabilities/protocol` 直接对外暴露。
from .capability_registry import (
    GEOMETRY_VALUES,
    INPUT_BBOX_PROMPT,
    INPUT_CROP,
    INPUT_FULL_IMAGE,
    INPUT_MASK_PROMPT,
    INPUT_POINT_PROMPT,
    INPUT_SCRIBBLE_PROMPT,
    INPUT_VALUES,
    INFRA_VALUES,
    PROMPT_VALUES,
    PROMPTS_REQUIRES_INPUT,
    TASK_DEFAULT_GEOMETRY as _TASK_DEFAULT_GEOMETRY,
    TASK_VALUES,
)
from .model_capability import ModelCapability


__all__ = [
    "INFRA_VALUES",
    "TASK_VALUES",
    "GEOMETRY_VALUES",
    "INPUT_VALUES",
    "ModelCapability",
    "extract_capabilities",
    "derive_modalities",
]

# 模态规范顺序 (image < video < lidar)。
_MODALITY_ORDER = {"image": 0, "video": 1, "lidar": 2}
_LIDAR_GEOMETRY = {"lidar_box_3d", "point_mask_3d"}

# 交互式 prompt 集合统一由 capability_registry SSOT 派生 (PROMPTS_REQUIRES_INPUT,
# 等价旧硬编码 8 元组)。语义不变: 含这类 prompt 的模型不默认走 crop 投递。


def _synthesize_supported_inputs(prompts: list[str]) -> list[str]:
    """v0.18.15 · 老 backend 缺 supported_inputs 时, 按 supported_prompts 合成兼容默认。

    受控词表: ``full_image | crop | bbox_prompt | point_prompt | mask_prompt |
    scribble_prompt | video``。
    - bbox/point prompt → 对应 ``bbox_prompt`` / ``point_prompt`` (box-seg 类走 geometry-prompt)。
    - 一律含 ``full_image`` (任何模型都能吃整图)。
    - 非交互模型 (纯检测/分类/OCR…) 额外含 ``crop`` (平台可裁父框 ROI 喂入), 让其能作几何/属性下游。
    - ``video`` 只能由 backend 显式声明, 老 backend 合成路径绝不补 video。
    """
    out: list[str] = []
    if "interactive_box" in prompts or "bbox" in prompts:
        out.append(INPUT_BBOX_PROMPT)
    if "point" in prompts:
        out.append(INPUT_POINT_PROMPT)
    if "mask" in prompts or "correction_frame" in prompts:
        out.append(INPUT_MASK_PROMPT)
    if "scribble" in prompts:
        out.append(INPUT_SCRIBBLE_PROMPT)
    out.append(INPUT_FULL_IMAGE)
    if not any(p in PROMPTS_REQUIRES_INPUT for p in prompts):
        out.append(INPUT_CROP)
    return list(dict.fromkeys(out))


def _normalize_infra(value: object) -> str:
    """空 → "unknown"; 受控值小写归一; 非受控但非空 → 原样小写 (前端容忍未知)。"""
    if not isinstance(value, str):
        return "unknown"
    v = value.strip().lower()
    return v or "unknown"


def _union(models: list[dict], key: str) -> list[str]:
    """按出现顺序去重合并各 model 的某个列表字段。"""
    out: list[str] = []
    for m in models:
        for v in m.get(key) or []:
            if v not in out:
                out.append(v)
    return out


def _model_modality(model: dict) -> str:
    """由单个 model 的 task / 几何派生模态。"""
    if model.get("task") == "tracker" or model.get("supported_trackers"):
        return "video"
    geo = model.get("supported_geometric_outputs") or []
    if any(g in _LIDAR_GEOMETRY for g in geo):
        return "lidar"
    return "image"


def _effective_max_window_frames(model: dict, limits: CapabilityLimits) -> int | None:
    raw = model.get("max_window_frames")
    if type(raw) is not int or raw <= 0:
        return None
    trackers = list(model.get("supported_trackers") or [])
    platform_limits = [
        int(limits.video_tracker_sam3_window_size_frames)
        if tracker.startswith("sam3")
        else int(limits.video_tracker_window_size_frames)
        for tracker in trackers
    ]
    return min([raw, *platform_limits]) if platform_limits else raw


def _normalize_model(model: dict, backend_infra: str, limits: CapabilityLimits) -> dict:
    """规范化一个 `/setup.models[]` 条目, infra 缺省继承 backend 默认。"""
    task = normalize_task(model.get("task") or "unknown", allow_unknown=True)
    geo = list(model.get("supported_geometric_outputs") or [])
    if not geo and task in _TASK_DEFAULT_GEOMETRY:
        geo = list(_TASK_DEFAULT_GEOMETRY[task])
    model_id = str(model.get("id") or model.get("name") or "default")
    supported_inputs = list(
        model.get("supported_inputs") or []
    ) or _synthesize_supported_inputs(list(model.get("supported_prompts") or []))
    default_input_type = model.get("default_input_type")
    if (
        not isinstance(default_input_type, str)
        or default_input_type not in supported_inputs
    ):
        default_input_type = supported_inputs[0] if supported_inputs else None
    out: dict = {
        "id": model_id,
        "display_name": model.get("display_name") or model.get("name") or model_id,
        "task": task,
        "model_family": model.get("model_family"),
        "infra": _normalize_infra(model.get("infra"))
        if model.get("infra")
        else backend_infra,
        "is_interactive": bool(model.get("is_interactive")),
        "supported_prompts": list(model.get("supported_prompts") or []),
        # 一等输入契约 (与 supported_prompts 解耦): 模型能吃哪些投递形态
        # (full_image | crop | bbox_prompt | point_prompt | video)。缺省按 prompts 合成兼容默认。
        "supported_inputs": supported_inputs,
        "default_input_type": default_input_type,
        "supported_geometric_outputs": geo,
        "output_attribute_types": list(model.get("output_attribute_types") or []),
        # 协议③ · backend 自报的属性 schema (含 select options), 供平台一键导入项目 attribute_schema.
        "output_attribute_schema": list(model.get("output_attribute_schema") or []),
        "supported_text_outputs": list(model.get("supported_text_outputs") or []),
        "supported_trackers": list(model.get("supported_trackers") or []),
        # text-driven tracker (sam3_video) 子集: 这些 tracker 的 propagate
        # 请求需 text/exemplars 而非仅 seed bbox。前端据此在选中该 tracker 时显 text 框。
        "text_driven_trackers": list(model.get("text_driven_trackers") or []),
        # 对前端暴露的是平台与 backend 两层限制的交集，与创建作业的
        # fail-closed 校验保持同一个有效窗口。
        "max_window_frames": _effective_max_window_frames(model, limits),
        "tracker_context_mode": model.get("tracker_context_mode")
        if model.get("tracker_context_mode") in {"none", "session"}
        else "none",
        "max_context_frames": model.get("max_context_frames")
        if type(model.get("max_context_frames")) is int
        and model["max_context_frames"] > 0
        else None,
        "supported_variants": model.get("supported_variants") or [],
        "variant_combinations": list(model.get("variant_combinations") or []),
        "variants_shared_across_tasks": bool(
            model.get("variants_shared_across_tasks", False)
        ),
        # backend 自报的默认 variant 组合 (dict[axis_key, value]).
        # 前端 VariantSelector 在用户未选时取此作初值, 优先级低于项目级 default_variants.
        "default_variants": dict(model.get("default_variants") or {}),
        "default_thresholds": model.get("default_thresholds") or {},
        "resource_profile": model.get("resource_profile") or {},
        # 协议 v2.2 · 原子 vs 内部编排维度。缺省 atom（绝大多数 model 是单次推理；
        # 现存复合都在自管 backend 显式标）。编排下游 stage 据此过滤（只组合 atom）。
        "composition": model.get("composition") or "atom",
        "params": model.get("params") or {},
        # 闭集检测器的原生类别表 (yolo model.names, [{index,name}]); 供前端类别白名单.
        # 仅在该 task 模型已加载过 (warmup/predict) 时 backend /setup 才带, 否则为空。
        "classes": list(model.get("classes") or []),
        # exemplar 模型的能力声明 (multi_box / negative_box / text_combination /
        # threshold_refilter); 前端工作台据此渲染 exemplar 控件 (YOLOE 无负框 → 隐藏负极性)。
        # 缺字段 = None, 前端按「全支持」向后兼容。
        "exemplar_capabilities": model.get("exemplar_capabilities") or None,
    }
    release, weights_digest = (
        model.get("weights_release_id"),
        model.get("weights_sha256"),
    )
    if (
        type(release) is str
        and 1 <= len(release) <= 255
        and release.strip() == release
        and type(weights_digest) is str
        and len(weights_digest) == 64
        and all(character in "0123456789abcdef" for character in weights_digest)
    ):
        # Legacy version labels do not prove an exact weight artifact.
        out.update(weights_release_id=release, weights_sha256=weights_digest)
    out["modality"] = _model_modality(out)
    return out


def _synthesize_single_model(setup: dict, backend_infra: str) -> dict:
    """老 backend (无 `models[]`) 的向后兼容路径: 顶层能力字段合成「隐式单 model」。

    task 由现有信号推断:
    - `supported_trackers` 非空 ⇒ tracker
    - `supported_prompts` 含交互路由的 prompt (PROMPTS_INTERACTIVE_ROUTE) ⇒ interactive_seg (SAM 类)
    - 否则 ⇒ detection

    claude[bot] P2 · 用 SSOT `PROMPTS_INTERACTIVE_ROUTE` (point/interactive_box/exemplar)
    替代原硬编码 tuple, 修 text-only legacy backend (如 GroundingDINO) 被误判为 interactive_seg
    的 bug —— text 走批量线、非交互路由, 不应让 task=detection 的纯文本 backend 看起来像 SAM。
    """
    from .capability_registry import PROMPTS_INTERACTIVE_ROUTE  # 局部 import 避免循环。

    prompts = list(setup.get("supported_prompts") or [])
    trackers = list(setup.get("supported_trackers") or [])
    if trackers:
        task = "tracker"
    elif any(p in PROMPTS_INTERACTIVE_ROUTE for p in prompts):
        task = "interactive_seg"
    else:
        task = "detect"
    model_id = str(setup.get("name") or "default")
    supported_inputs = _synthesize_supported_inputs(prompts)
    default_input_type = setup.get("default_input_type")
    if (
        not isinstance(default_input_type, str)
        or default_input_type not in supported_inputs
    ):
        default_input_type = supported_inputs[0] if supported_inputs else None
    out: dict = {
        "id": model_id,
        "display_name": setup.get("name") or model_id,
        "task": task,
        "model_family": None,
        "infra": backend_infra,
        "is_interactive": bool(setup.get("is_interactive")),
        "supported_prompts": prompts,
        # 老 backend 无 supported_inputs, 按 prompts 合成兼容默认 (零退化)。
        "supported_inputs": supported_inputs,
        "default_input_type": default_input_type,
        "supported_geometric_outputs": list(
            setup.get("supported_geometric_outputs") or []
        ),
        "output_attribute_types": [],
        "supported_text_outputs": list(setup.get("supported_text_outputs") or []),
        "supported_trackers": trackers,
        # text-driven tracker 子集 (见 _normalize_model)。
        "text_driven_trackers": list(setup.get("text_driven_trackers") or []),
        "supported_variants": setup.get("supported_variants") or [],
        # 老 backend 均为单次推理原子（协议 v2.2）。
        "composition": "atom",
        "default_thresholds": {},
        "resource_profile": {},
        "params": setup.get("params") or {},
        # 老 backend (sam3) 的 exemplar 能力在顶层声明, 透传供前端 exemplar 控件渲染。
        "exemplar_capabilities": setup.get("exemplar_capabilities") or None,
    }
    out["modality"] = _model_modality(out)
    return out


def _collect_warnings(models: list[dict]) -> list[dict]:
    """校验规范化后各 model 的受控值, 越界即记 warning (只诊断、不改写)。

    v0.18.29 · 把「字段拼错 / 值越界致工具静默不亮」变成模型市场可见信号。只校验受控值
    (task / infra / supported_prompts / supported_inputs / supported_geometric_outputs); 派生缺省
    (task / infra == "unknown") 跳过 — 那是 backend 没声明、平台已兜底, 非越界。

    claude[bot] P2 · 加未知字段名检测: ModelCapability 用 ``extra=allow``, backend 拼错
    (如 ``output_attribute_typo``) 既逃 schema 校验又逃 warning, 与本批 SSOT 方向相反。
    白名单 = ModelCapability.model_fields ∪ _normalize_model 透传的扩展字段
    (``modality`` 为派生, ``exemplar_capabilities`` 为协议外透传). 多余键发 info 级警告。
    """
    known_model_keys = set(ModelCapability.model_fields.keys()) | {
        # ModelCapability 用 extra=allow 接以下透传 / 派生字段, 不算未知。
        "exemplar_capabilities",  # 透传给前端 exemplar 控件
        "weights_release_id",
        "weights_sha256",
    }
    out: list[dict] = []

    def warn(
        model_id: str, field: str, value: str, message: str, level: str = "warning"
    ) -> None:
        out.append(
            {
                "level": level,
                "model_id": model_id,
                "field": field,
                "value": value,
                "message": message,
            }
        )

    for m in models:
        mid = str(m.get("id") or "?")
        task = m.get("task")
        if task and task != "unknown" and task not in TASK_VALUES:
            warn(
                mid,
                "task",
                task,
                f"未知 task「{task}」不在受控词表; 平台无法据此派生默认几何/模态。",
            )
        infra = m.get("infra")
        if infra and infra != "unknown" and infra not in INFRA_VALUES:
            warn(
                mid,
                "infra",
                infra,
                f"未知 infra「{infra}」不在受控词表。",
                level="info",
            )
        for p in m.get("supported_prompts") or []:
            if p not in PROMPT_VALUES:
                warn(
                    mid,
                    "supported_prompts",
                    p,
                    f"未知 prompt「{p}」; 前端工具门控不识别, 该提示将静默失效。",
                )
        for i in m.get("supported_inputs") or []:
            if i not in INPUT_VALUES:
                warn(
                    mid,
                    "supported_inputs",
                    i,
                    f"未知 input「{i}」不在受控词表; 编排可达性判据不识别。",
                )
        default_input = m.get("default_input_type")
        if isinstance(default_input, str) and default_input not in INPUT_VALUES:
            warn(
                mid,
                "default_input_type",
                default_input,
                f"未知 default_input_type「{default_input}」不在受控词表。",
            )
        for g in m.get("supported_geometric_outputs") or []:
            if g not in GEOMETRY_VALUES:
                warn(
                    mid,
                    "supported_geometric_outputs",
                    g,
                    f"未知几何输出「{g}」不在受控词表。",
                )
        # claude[bot] P2 · ModelCapability extra=allow 不会拒未知键; 这里发 info 警告
        # 抓 backend 字段名拼写错误 (如 output_attribute_typo) 让其在模型市场可见。
        unknown_keys = sorted(set(m.keys()) - known_model_keys)
        for k in unknown_keys:
            warn(
                mid,
                k,
                "",
                f"未知字段「{k}」不在 ModelCapability schema; 可能是拼写错误, 平台会忽略。",
                level="info",
            )
    return out


def extract_capabilities(
    setup: dict | None, *, limits: CapabilityLimits = CapabilityLimits()
) -> dict | None:
    """从 `/setup` 响应抽能力快照; setup 为空返回 None。

    返回结构 (v0.14.9 协议 v2):
    - `infra`: backend 默认基础设施 (pytorch / onnx / paddle / ...)。
    - `models`: 规范化后的 model 条目数组 (老 backend 合成单条)。
    - 扁平并集字段 (`is_interactive` / `supported_prompts` / `supported_trackers` /
      `supported_text_outputs` / `supported_geometric_outputs`): 各 model 去重合并,
      供未迁移的消费方继续读, 保证向后兼容。
    - `modalities`: 派生视图 (image / video / lidar)。
    """
    if not setup:
        return None
    infra = _normalize_infra(setup.get("infra"))
    raw_models = setup.get("models")
    if isinstance(raw_models, list) and raw_models:
        models = [
            _normalize_model(m, infra, limits)
            for m in raw_models
            if isinstance(m, dict)
        ]
    else:
        models = [_synthesize_single_model(setup, infra)]

    managed_lifecycle: dict[str, object] | None = None
    managed_lifecycle_warning: dict | None = None
    if "managed_lifecycle" in setup:
        try:
            managed_lifecycle = canonical_managed_lifecycle_capabilities(
                setup["managed_lifecycle"]
            )
        except (TypeError, ValueError):
            managed_lifecycle_warning = {
                "level": "warning",
                "model_id": None,
                "field": "managed_lifecycle",
                "value": "invalid",
                "message": (
                    "managed_lifecycle 必须完整、严格匹配受管 GPU 生命周期协议。"
                ),
            }

    warnings = _collect_warnings(models)
    if managed_lifecycle_warning is not None:
        warnings.append(managed_lifecycle_warning)

    caps: dict = {
        # Stable compatibility/model identity fields participate in service-pool
        # interchangeability checks.  Runtime and display metadata remain excluded.
        "version": setup.get("version"),
        "protocol_version": setup.get("protocol_version") or "1",
        "task_contract_revision": 1
        if type(setup.get("task_contract_revision")) is int
        and setup["task_contract_revision"] == 1
        else 0,
        "compat_protocol_versions": list(setup.get("compat_protocol_versions") or []),
        "model_version": setup.get("model_version"),
        "weights_version": setup.get("weights_version"),
        # 透传 backend 自报的 name (如 "grounded-sam2-backend"), 让前端
        # 能力目录显示「源 backend 名」而非用户取的项目别名 (如 "gsam2.1")。
        "name": setup.get("name"),
        "infra": infra,
        # backend 声明本端是否支持 POST /warmup (协议 §4.4); 前端模型市场
        # "⚡ 预热" 按钮据此置灰. 老 backend 缺字段 = False.
        "warmup_endpoint": bool(setup.get("warmup_endpoint", False)),
        # ADR-0049 · 远端声明必须显式携带全部冻结字段；平台不能用 Pydantic
        # 默认值把 partial/legacy backend 静默升级成受管 backend。
        "managed_lifecycle": managed_lifecycle,
        "models": models,
        "is_interactive": any(m["is_interactive"] for m in models),
        "supported_prompts": _union(models, "supported_prompts"),
        "supported_inputs": _union(models, "supported_inputs"),
        "supported_trackers": _union(models, "supported_trackers"),
        "text_driven_trackers": _union(models, "text_driven_trackers"),
        "supported_text_outputs": _union(models, "supported_text_outputs"),
        "supported_geometric_outputs": _union(models, "supported_geometric_outputs"),
        # 受控词表校验诊断 (越界 task/infra/prompt/geometry); 空 = 全合法。
        "warnings": warnings,
    }
    caps["modalities"] = derive_modalities(caps)
    return caps


def derive_modalities(caps: dict | None) -> list[str]:
    """由能力快照派生支持的模态。

    扁平规则 (向后兼容, 且让「image+video 双修」的 backend 两个模态都保留):
    - `supported_prompts` 非空 ⇒ image (point/bbox/text/exemplar/none 都是图片侧)。
    - `supported_trackers` 非空 ⇒ video。
    per-model 补充: 多模型 backend 的 lidar / 纯 video model 等由 model 条目补齐。
    """
    if not caps:
        return []
    mods: list[str] = []
    if caps.get("supported_prompts"):
        mods.append("image")
    if caps.get("supported_trackers"):
        mods.append("video")
    for m in caps.get("models") or []:
        mod = _model_modality(m)
        if mod not in mods:
            mods.append(mod)
    return sorted(set(mods), key=lambda m: _MODALITY_ORDER.get(m, 99))


def resolve_model_task(
    backend, model_id: str | None, task: str | None = None
) -> str | None:
    """Resolve an explicit model's task while leaving opaque model IDs intact."""
    current = normalize_task(task) if task is not None else None
    meta = getattr(backend, "health_meta", None) or {}
    caps = meta.get("capabilities") or {}
    for model in caps.get("models") or []:
        if model_id is not None and str(model.get("id")) == str(model_id):
            expected = normalize_task(
                model.get("task") or "unknown", allow_unknown=True
            )
            if expected not in TASK_VALUES:
                return current
            if current is not None and current != expected:
                raise ValueError("task 与所选模型的任务不一致")
            return expected
    return current
