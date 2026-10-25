from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from app.services import predictions_import


@pytest.mark.asyncio
async def test_task_image_size_uses_public_assets_spatial_facts(monkeypatch):
    item_id = uuid.uuid4()
    db = object()
    item = SimpleNamespace(width=640, height=480)
    calls = []

    async def read_items(actual_db, item_ids):
        calls.append((actual_db, item_ids))
        return {item_id: item}, {}

    monkeypatch.setattr(predictions_import, "read_spatial_item_facts", read_items)

    size = await predictions_import._task_image_size(
        db, SimpleNamespace(dataset_item_id=item_id)
    )

    assert size == (640, 480)
    assert calls == [(db, [item_id])]


@pytest.mark.asyncio
async def test_task_image_size_preserves_missing_dimension_behavior(monkeypatch):
    item_id = uuid.uuid4()

    async def read_items(_db, _item_ids):
        return {item_id: SimpleNamespace(width=640, height=None)}, {}

    monkeypatch.setattr(predictions_import, "read_spatial_item_facts", read_items)

    size = await predictions_import._task_image_size(
        object(), SimpleNamespace(dataset_item_id=item_id)
    )

    assert size is None
