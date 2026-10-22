"""Shared request schemas for preannotation pipeline configuration."""

from __future__ import annotations

import uuid
from typing import Literal

from aap_task_contract import Task as VisualTaskName, normalize_task_fields
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PipelineStage(BaseModel):
    """多阶段预标注的一项声明。

    parent_stage=None 标识源阶段；下游阶段消费父阶段几何，可写入属性、
    独立几何或中间结果。阶段序号不必从零开始，任一依赖链最多三层。
    """

    # 脏键在校验期即 422, 不再原样落进 preannotate_pipeline JSONB (见 issue 0008.1);
    # 前端 PipelineStagePayload 键集与此严格一致, 不会误伤。
    model_config = ConfigDict(extra="forbid")

    stage: int
    ml_backend_id: uuid.UUID
    # 初始输入节点的数据源描述: {"kind":"dataset","data_type":...,"execution_unit":...}。
    # 声明源类型与执行单位；下游 stage 不携带此字段。
    source: dict | None = None
    model_id: str | None = None
    task: VisualTaskName | None = None
    prompt_type: Literal["text"] | None = None

    @model_validator(mode="before")
    @classmethod
    def canonical_task_fields(cls, value):
        return normalize_task_fields(value)

    model_variants: dict[str, str] | None = None
    params: dict | None = None
    class_filter: list[int] | None = None
    # 依赖的父阶段 index; None=源阶段 (吃整图)。
    parent_stage: int | None = None
    # 按类别路由: 本阶段只对这些 class_name 的父框启动 (空/缺=全部父框)。
    # 不相交类别集=不同类走不同下游模型; 重叠=同类喂多模型 (并行扇出)。声明式过滤, 非分支节点。
    parent_class_filter: list[str] | None = None
    # ROI 构造; 认 {"mode":"crop","pad":0.05}, pad∈[0,0.5]。
    roi: dict | None = None
    # 结果写回; 认 {"target":"attributes","keys":[...]}。keys 用于并行兄弟键冲突检测。
    write: dict | None = None
    # 阶段级失败策略: keep_parent (默认, 下游失败保留上游框、属性留空) | drop_box (丢父框)。
    on_failure: Literal["keep_parent", "drop_box"] = "keep_parent"
    # 卡片显示名 + 写回属性键前缀。设了 label, 写回键加 f"{label}_" 前缀 (子物体
    # 命名空间, 如 hat_color / shoe_color); 缺省写原始键 (双阶段零退化)。
    label: str | None = None
    # 显式输入模式覆盖 {"mode": "full_image"|"crop"|"geometry"}; 缺省由 worker 按
    # write.target 推断 (attributes→crop, geometry/intermediate→geometry-prompt)。
    input: dict | None = None


class PreannotateRequest(BaseModel):
    ml_backend_id: uuid.UUID | None = None
    task_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=200)
    # 文本批量预标可选参数
    prompt: str | None = None
    output_mode: Literal["box", "mask", "both"] = "mask"
    batch_id: uuid.UUID | None = None
    # 执行场景 (issue #121): bulk=数据管理批量预标 (缺省, 保留 pending + active 批次 +
    # 无有效编辑锁的批量前置条件); workbench=工作台「当前题 AI」单题交互执行, 按当前用户
    # 对该题的编辑语义校验 (允许 in_progress 任务与 draft 批次, 保留管理员锁 / 他人锁 / 终态保护)。
    execution_scope: Literal["bulk", "workbench"] = "bulk"
    # 按后端参数面板 (epic 阶段 2): 选中 backend 的 /setup.params 值,
    # 由前端按 backend 分桶解析后显式带上, worker 合并进 /predict context (覆盖项目级阈值兜底).
    params: dict | None = None
    # 幂等模式: skip_predicted=跳过已预标 task (默认), overwrite=先清旧预测再预标,
    # append=保留旧行为 (无脑追加, 仅特殊场景). 避免重复预标叠加重复标注.
    predict_mode: Literal["skip_predicted", "overwrite", "append"] = "skip_predicted"
    # 能力声明协议 v2 多模型目录: 选中 backend 暴露的某个 model 条目 id,
    # worker 透传进 /predict context["model_id"], backend 据此路由到对应模型。
    model_id: str | None = None
    # 任务类型便捷别名 ("ocr"/"doc_layout"/"text"): worker 写 context["type"],
    # 让纯文本以外的 task (OCR / 版面分析) 也能走批量预标。缺省走老的纯 prompt / image 行为。
    task: VisualTaskName | None = None
    prompt_type: Literal["text"] | None = None

    @model_validator(mode="before")
    @classmethod
    def canonical_task_fields(cls, value):
        return normalize_task_fields(value)

    # 协议 v2 结构化路径 (YOLO 等多 task 几何 backend): 选中 variant 组合 (dict[axis,value])。
    # 非空时 worker 构造 v2 context (model_variants dict + nested params + type=几何 task),
    # 而非 gsam2 文本路径的扁平形态。修通 YOLO 批量预标 (此前 worker 发 type="text" 被 YOLO 422)。
    model_variants: dict[str, str] | None = None
    # 类别白名单 (模型原生类别 index 子集): 非空时 backend 只检出这些类。
    # 平台不做类→项目标签映射 (NG6), 仅透传给 yolo /predict context.classes 做推理层过滤。
    class_filter: list[int] | None = None
    # 多阶段预标注 (路径 B): 有序阶段列表。非空时走阶段化编排 (detect→ROI→classify),
    # 缺省时由上面的平铺字段合成单阶段, 与现状逐字等价 (向后兼容)。
    # 支持单层并行扇出 (源 + N 个共享 parent_stage 的下游)。
    pipeline_stages: list[PipelineStage] | None = None
    # 并行兄弟写同一属性键时的策略: reject (默认, 校验期 422) | last_wins (末位覆盖)。
    on_key_conflict: Literal["reject", "last_wins"] = "reject"

    @model_validator(mode="after")
    def _validate_pipeline_stages(self) -> "PreannotateRequest":
        stages = self.pipeline_stages
        if not stages:
            if self.ml_backend_id is None:
                raise ValueError("ml_backend_id 必填")
            return self
        indices = {s.stage for s in stages}
        if len(indices) != len(stages):
            raise ValueError("pipeline_stages 的 stage 序号不可重复")
        # 源阶段: 恰一个 parent_stage=None。顶层 ml_backend_id 是兼容字段, 多阶段时
        # 自动从源阶段派生, 避免项目主 backend / payload 顶层成为第二真值。
        roots = [s for s in stages if s.parent_stage is None]
        if len(roots) != 1:
            raise ValueError("pipeline_stages 须恰有一个源阶段 (parent_stage=None)")
        root = roots[0]
        self.ml_backend_id = root.ml_backend_id
        self.model_id = root.model_id
        self.task = root.task
        self.prompt_type = root.prompt_type
        self.model_variants = root.model_variants
        self.params = root.params
        self.class_filter = root.class_filter
        # 受限树形校验 (max depth 3): 替换原单层扇出约束。
        # parent_stage 须指向已定义且更早的阶段 (序号严格小于, 自然无环); 父须产可消费几何
        # (write.target ∈ {geometry, intermediate}); 任一链路深度 ≤ 3。
        # parent_stage=0 的旧双阶段 payload 在此等价通过 (root depth=1, 子 depth=2)。
        known_depth: dict[int, int] = {root.stage: 1}
        known_target: dict[int, str] = {
            root.stage: (root.write or {}).get("target", "geometry")
        }
        for s in sorted(stages, key=lambda x: x.stage):
            if s.parent_stage is None:
                continue
            if s.parent_stage >= s.stage:
                raise ValueError(
                    f"stage {s.stage} 的 parent_stage={s.parent_stage} 必须小于子阶段序号"
                )
            if s.parent_stage not in known_depth:
                raise ValueError(
                    f"stage {s.stage} 的 parent_stage={s.parent_stage} 未在前面定义; "
                    "受限树形要求父阶段序号严格小于子阶段"
                )
            parent_depth = known_depth[s.parent_stage]
            if parent_depth >= 3:
                raise ValueError(
                    f"stage {s.stage} 超过最大深度 3 (父深度={parent_depth})"
                )
            parent_target = known_target[s.parent_stage]
            if parent_target not in {"geometry", "intermediate"}:
                raise ValueError(
                    f"stage {s.stage} 的父阶段 {s.parent_stage} write.target={parent_target!r}, "
                    "不产几何, 无法作为父阶段"
                )
            # drop_box 的「丢父框」语义仅在父阶段为源阶段 (root) 时下标才与 root_boxes 对齐;
            # 深层 (非-root-父) 阶段的父几何是中间产物, worker 误用其下标会删掉无关的 root 框
            # (见 issue 0001)。深层 drop_box 语义未定义, 此处直接拒绝, 防止静默数据丢失。
            if s.on_failure == "drop_box" and s.parent_stage != root.stage:
                raise ValueError(
                    f"stage {s.stage} 的 on_failure='drop_box' 仅支持父阶段为源阶段 "
                    f"(stage {root.stage}); 深层阶段请用 on_failure='keep_parent'"
                )
            target = (s.write or {}).get("target", "attributes")
            # geometry: 下游产独立 polygon 追加为 new shape。intermediate: 只产几何给下游消费,
            # 不落库为候选。attributes: 纯分类下游写回父框 attributes。
            if target not in {"attributes", "geometry", "intermediate"}:
                raise ValueError(
                    "write.target 须为 'attributes' / 'geometry' / 'intermediate', "
                    f"收到 {target!r}"
                )
            # 未来祖先选择扩展点; 本版仅接受 'root' (缺省也按 root 处理)。
            ts = (s.write or {}).get("target_stage")
            if ts not in (None, "root"):
                raise ValueError(
                    f"stage {s.stage} 的 write.target_stage={ts!r} 暂不支持, 本版仅接受 'root'"
                )
            if s.roi is not None:
                mode = s.roi.get("mode", "crop")
                # crop: 平台裁父框 ROI 喂下游分类。geometry: 全图 + 父框列表喂 box-seg。
                if mode not in {"crop", "geometry"}:
                    raise ValueError(
                        f"roi.mode 须为 'crop' 或 'geometry', 收到 {mode!r}"
                    )
                pad = s.roi.get("pad")
                if pad is not None and not (0.0 <= float(pad) <= 0.5):
                    raise ValueError("roi.pad 须在 [0, 0.5] 区间")
            if s.input is not None:
                imode = s.input.get("mode")
                # 下游阶段投递只有 crop (裁父框 ROI) / geometry (整图+父框列表) 两态; worker
                # _resolve_input_mode 也只认这两个。full_image 不是真实投递模式 (会被 worker
                # 静默忽略并回落 write.target 启发式), 校验期直接拒绝以保契约一致 (见 issue 0006)。
                if imode is not None and imode not in {"crop", "geometry"}:
                    raise ValueError(
                        f"input.mode 须为 'crop' / 'geometry', 收到 {imode!r}"
                    )
            known_depth[s.stage] = parent_depth + 1
            known_target[s.stage] = target
        # 属性键冲突: 按"加完 label 前缀的最终键"维度 (写回 root)。无 label 时用原始键 (= 旧双阶段
        # 行为, 零退化); 设了 label 才加 f"{label}_" 前缀 (子物体命名空间, 与 worker 写回一致)。
        # geometry/intermediate 不写 attributes, 不参与冲突检测。
        if self.on_key_conflict == "reject":
            final_keys: dict[str, int] = {}
            for s in stages:
                if s.parent_stage is None or not s.write:
                    continue
                if s.write.get("target", "attributes") != "attributes":
                    continue
                prefix = f"{s.label}_" if s.label else ""
                for k in s.write.get("keys") or []:
                    final = f"{prefix}{k}"
                    if final in final_keys:
                        raise ValueError(
                            f"attribute key 冲突: stage {s.stage} 与 stage {final_keys[final]} "
                            f"都写 {final!r} 到 root; 设 on_key_conflict=last_wins 以允许末位覆盖"
                        )
                    final_keys[final] = s.stage
        return self
