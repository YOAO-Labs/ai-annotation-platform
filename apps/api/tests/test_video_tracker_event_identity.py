"""Tracker event identities preserve nullable source annotation semantics."""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services.video_tracking.runner import _event


@pytest.mark.parametrize("annotation_id", [None, uuid.UUID(int=1)])
@pytest.mark.parametrize("event_type", ["job_started", "job_progress"])
def test_tracker_event_uses_null_for_absent_source(annotation_id, event_type):
    job = SimpleNamespace(
        id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        dataset_item_id=uuid.uuid4(),
        annotation_id=annotation_id,
    )
    payload = json.loads(json.dumps(_event(job, event_type, current=1, total=2)))
    assert payload["annotation_id"] == (str(annotation_id) if annotation_id else None)
    assert payload["job_id"] == str(job.id)
    assert payload["type"] == event_type
