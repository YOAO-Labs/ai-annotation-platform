from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from aap_labeling.annotation_transfer_contract import TaskMatchSnapshot
from app.services import task_matcher


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "missing", "mismatch"])
async def test_resolve_task_hydrates_the_matched_id_from_public_core_read(
    monkeypatch, change
):
    task_id, project_id, dataset_item_id = (uuid.uuid4() for _ in range(3))
    snapshot = TaskMatchSnapshot(
        id=task_id,
        project_id=project_id,
        dataset_item_id=dataset_item_id,
        display_id="T-1",
        file_name="frame.jpg",
        file_path="dataset/frame.jpg",
        file_type="image",
    )
    task = SimpleNamespace(
        **snapshot.model_dump(),
        version=7,
        media_source_generation=3,
        status="pending",
    )
    if change == "mismatch":
        task.file_path = "dataset/moved.jpg"
    db = object()
    calls = []

    async def transfer(_db, request):
        calls.append(("match", request))
        return SimpleNamespace(task=snapshot)

    async def get_core_row(_db, row, row_id):
        calls.append(("hydrate", row, row_id))
        return None if change == "missing" else task

    monkeypatch.setattr(task_matcher, "_transfer", transfer)
    monkeypatch.setattr(task_matcher, "get_core_row", get_core_row)

    result = await task_matcher.resolve_task(
        db, project_id, {"display_id": snapshot.display_id}
    )

    assert calls[0][0] == "match"
    assert calls[1] == ("hydrate", task_matcher.task_row, task_id)
    if change in {"missing", "mismatch"}:
        assert result is None
    else:
        assert result is task
        assert result.version == 7
        assert result.media_source_generation == 3
