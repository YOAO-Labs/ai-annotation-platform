"""Shared request schemas for preannotation pipeline configuration."""

from __future__ import annotations

import uuid
from typing import Literal

from yoao_task_contract import Task as VisualTaskName, normalize_task_fields
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PipelineStage(BaseModel):
    """多阶段预标注的一项声明。

    parent_stage=None 标识源阶段；下游阶段消费父阶段几何，可写入属性、
    独立几何或中间结果。阶段序号不必从零开始，任一依赖链最多三层。
    """

    model_config = ConfigDict(extra="forbid")

    stage: int
    ml_backend_id: uuid.UUID
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
    parent_stage: int | None = None
    parent_class_filter: list[str] | None = None
    roi: dict | None = None
    write: dict | None = None
    on_failure: Literal["keep_parent", "drop_box"] = "keep_parent"
    label: str | None = None
    input: dict | None = None


class PreannotateRequest(BaseModel):
    ml_backend_id: uuid.UUID | None = None
    task_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = None
    output_mode: Literal["box", "mask", "both"] = "mask"
    batch_id: uuid.UUID | None = None
    execution_scope: Literal["bulk", "workbench"] = "bulk"
    params: dict | None = None
    predict_mode: Literal["skip_predicted", "overwrite", "append"] = "skip_predicted"
    model_id: str | None = None
    task: VisualTaskName | None = None
    prompt_type: Literal["text"] | None = None

    @model_validator(mode="before")
    @classmethod
    def canonical_task_fields(cls, value):
        return normalize_task_fields(value)

    model_variants: dict[str, str] | None = None
    class_filter: list[int] | None = None
    pipeline_stages: list[PipelineStage] | None = None
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
            if s.on_failure == "drop_box" and s.parent_stage != root.stage:
                raise ValueError(
                    f"stage {s.stage} 的 on_failure='drop_box' 仅支持父阶段为源阶段 "
                    f"(stage {root.stage}); 深层阶段请用 on_failure='keep_parent'"
                )
            target = (s.write or {}).get("target", "attributes")
            if target not in {"attributes", "geometry", "intermediate"}:
                raise ValueError(
                    "write.target 须为 'attributes' / 'geometry' / 'intermediate', "
                    f"收到 {target!r}"
                )
            ts = (s.write or {}).get("target_stage")
            if ts not in (None, "root"):
                raise ValueError(
                    f"stage {s.stage} 的 write.target_stage={ts!r} 暂不支持, 本版仅接受 'root'"
                )
            if s.roi is not None:
                mode = s.roi.get("mode", "crop")
                if mode not in {"crop", "geometry"}:
                    raise ValueError(
                        f"roi.mode 须为 'crop' 或 'geometry', 收到 {mode!r}"
                    )
                pad = s.roi.get("pad")
                if pad is not None and not (0.0 <= float(pad) <= 0.5):
                    raise ValueError("roi.pad 须在 [0, 0.5] 区间")
            if s.input is not None:
                imode = s.input.get("mode")
                if imode is not None and imode not in {"crop", "geometry"}:
                    raise ValueError(
                        f"input.mode 须为 'crop' / 'geometry', 收到 {imode!r}"
                    )
            known_depth[s.stage] = parent_depth + 1
            known_target[s.stage] = target
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
