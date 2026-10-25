from __future__ import annotations

from types import SimpleNamespace
import uuid

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.db.models.task_batch import TaskBatch
from app.services.labeling_task_batch_counter_data import (
    _clear_pending_batch_counters,
    _clear_finished_batch_counter_scope,
    _write_pending_batch_counters,
    register_source_less_batch_counter_projection,
)


def test_source_less_batch_counter_bridge_is_opt_in_and_rollback_clears_state():
    first = Session()
    second = Session()
    project_id = uuid.uuid4()
    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project_id,
        display_id="B-BRIDGE",
        name="bridge",
    )

    register_source_less_batch_counter_projection(
        SimpleNamespace(sync_session=first), project_id, batch
    )

    assert event.contains(first, "before_flush", _write_pending_batch_counters)
    assert not event.contains(second, "before_flush", _write_pending_batch_counters)
    assert (
        first.info["aap_labeling_source_less_batch_counter_pending"][project_id][
            batch.id
        ]
        is batch
    )

    _clear_pending_batch_counters(first, None)
    assert "aap_labeling_source_less_batch_counter_pending" not in first.info

    register_source_less_batch_counter_projection(
        SimpleNamespace(sync_session=first), project_id, batch
    )
    _clear_finished_batch_counter_scope(first, SimpleNamespace(parent=None))
    assert "aap_labeling_source_less_batch_counter_pending" not in first.info

    register_source_less_batch_counter_projection(
        SimpleNamespace(sync_session=first), project_id, batch
    )
    assert (
        first.info["aap_labeling_source_less_batch_counter_pending"][project_id][
            batch.id
        ]
        is batch
    )

    first.close()
    second.close()
