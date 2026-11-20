"""EventEnvelope schema 序列化锁（ADR-0025）。

锁住信封字段顺序与必备性；任何 breaking change 必须更新此测试 + 升 event_version major。
"""

from datetime import datetime, timezone

from pydantic import BaseModel

from app.schemas.event_envelope import EventEnvelope


class _RejectData(BaseModel):
    task_id: str
    reason_type: str
    reason: str | None = None


def test_event_envelope_serializes_with_all_fields():
    env = EventEnvelope[_RejectData](
        event="task.rejected",
        delivery_id="01JX0000000000000000000000",
        occurred_at=datetime(2026, 5, 19, 10, 0, 0, tzinfo=timezone.utc),
        data=_RejectData(task_id="t1", reason_type="missing", reason="框漏了 3 处"),
    )
    payload = env.model_dump(mode="json")
    assert payload["event_version"] == "1.0"
    assert payload["event"] == "task.rejected"
    assert payload["delivery_id"] == "01JX0000000000000000000000"
    assert payload["occurred_at"].startswith("2026-05-19T10:00:00")
    assert payload["data"] == {
        "task_id": "t1",
        "reason_type": "missing",
        "reason": "框漏了 3 处",
    }


def test_event_envelope_roundtrip_preserves_data():
    env = EventEnvelope[_RejectData].model_validate(
        {
            "event_version": "1.0",
            "event": "task.rejected",
            "delivery_id": "01JX1111111111111111111111",
            "occurred_at": "2026-05-19T10:00:00Z",
            "data": {"task_id": "t9", "reason_type": "wrong_label"},
        }
    )
    assert env.data.task_id == "t9"
    assert env.data.reason_type == "wrong_label"
    assert env.data.reason is None
