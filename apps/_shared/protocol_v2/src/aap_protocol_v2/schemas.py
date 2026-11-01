"""ML backend 协议 v2 跨 backend 共用的请求 / 响应 Pydantic 模型。

单一来源后, 协议层字段命名 / 类型扩展只改这一处。

每个 backend 自己的 `Context`（prompt 字段集差异巨大）不放在这里, 由各 backend 本地定义。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from aap_protocol_v2.mask_interaction import MaskInteractionDiagnostic


class TaskItem(BaseModel):
    """`/predict` 入参里的单个 task. `id` 透传回响应, `file_path` 是图像 URL 或绝对路径."""

    id: str | int
    file_path: str


class PoolStateSnapshot(BaseModel):
    """轻量 pool 快照, 可选附在 `/predict` 响应 (debug / 客户端缓存命中预测)."""

    current_size: int
    cap: int


class PredictionResult(BaseModel):
    """单 task 的预测结果壳. `result` 数组里的元素结构由具体 result.type 决定
    (rectanglelabels / polygonlabels / keypointlabels 等), 在协议文档 §3 详述.

    v0.14.14 新增 cache_hit / model_load_ms / pool_state 三个可选字段供前端做冷启动反馈。
    """

    task: str | int | None = None
    result: list[dict[str, Any]] = Field(default_factory=list)
    score: float | None = None
    model_version: str | None = None
    inference_time_ms: int | None = None
    # v0.14.14 · 运行时观测字段 (None = 该 backend 不支持上报)
    cache_hit: bool | None = None
    model_load_ms: int | None = None
    pool_state: PoolStateSnapshot | None = None
    # v0.18.18 · 交互单实例精修的 256×256 low-res logits 回灌 (base64, 见 mask_codec)。
    # 仅 multimask_output=False 的单 mask 路径返回 (规避多候选 index 歧义); 前端原样
    # 存储、下次点击经 context.mask_input 回传。None = 本轮不回灌。
    mask_input_next: str | None = None
    # 原生 Mask 候选的空结果 / 降级诊断；旧 backend 缺失时保持 None。
    diagnostic: MaskInteractionDiagnostic | None = None
    meta: dict[str, Any] | None = None


class BatchPredictResponse(BaseModel):
    """`/predict` 顶层响应. 即便单 task 也包成 results 数组, 统一交互式 + 批量路径."""

    results: list[PredictionResult]


# ---------- v0.14.14: /health.pool 统一 schema ----------


class LoadedKey(BaseModel):
    """`/health.pool.loaded_keys[]` 单条已加载权重描述.

    `key` 是 backend-defined 的 opaque 字符串 (yolo 用 `task/series/size`,
    gsam2 用 `sam=tiny/dino=T`, sam3 用 `sam3`), 前端只做字符串相等比较.
    """

    key: str
    loaded_at: datetime
    last_used_at: datetime
    hit_count: int = 0


class EvictRecord(BaseModel):
    """`/health.pool.last_evict` 最近一次淘汰记录."""

    key: str
    at: datetime
    reason: Literal["lru", "manual", "idle_timeout"]


class PoolStatus(BaseModel):
    """`/health.pool` 三 backend 统一结构 (v0.14.14 起)."""

    cap: int
    current_size: int
    loaded_keys: list[LoadedKey] = Field(default_factory=list)
    last_evict: EvictRecord | None = None


# ---------- v0.14.14: POST /warmup 端点 ----------


class WarmupResponse(BaseModel):
    """`/warmup` 响应. 加载权重到 pool 而不跑真实推理.

    `evicted` 在本次预热因 cap 上限而淘汰其他 key 时不为空,
    前端 toast 提示 "已加载 X, evict 了 Y".
    """

    ok: bool = True
    model_load_ms: int | None = None
    cache_hit: bool = False
    evicted: str | None = None
