"""Webhook 事件信封占位 schema（ADR-0025）。

仅定义信封形状与事件名 Literal，**不**接入任何 publisher / outbox / delivery。
未来 ROADMAP §2.1 Webhook epic 落地时直接复用，无需重写。
"""

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

EventName = Literal[
    "task.created",
    "task.reviewed",
    "task.approved",
    "task.rejected",
    "batch.state_changed",
    "prediction.completed",
    "prediction.failed",
    "bug_report.created",
]


T = TypeVar("T", bound=BaseModel)


class EventEnvelope(BaseModel, Generic[T]):
    """Webhook payload 顶层信封。

    所有 webhook 事件都遵循此结构，便于客户端按 envelope 字段做版本判断
    与 delivery_id 幂等去重。data 字段的 schema 由 event 决定。

    版本规则：
    - event_version="1.x" SemVer。minor 升级**只能**新增可空字段。
    - 跨 major 升级（极少）需双轨发送至少 90 天。
    """

    event_version: str = Field(
        default="1.0",
        description="信封 schema 版本（非 data 版本）",
    )
    event: EventName = Field(description="事件名，形如 domain.action")
    delivery_id: str = Field(
        description="消费侧幂等去重键；同一逻辑事件重试时不变（UUID 或 ULID）"
    )
    occurred_at: datetime = Field(description="事件发生时刻（非发送时刻）")
    data: T = Field(description="事件载荷，schema 由 event 决定")
