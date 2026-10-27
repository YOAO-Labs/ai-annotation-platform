from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.dataset import Dataset
from app.services.dataset import DatasetService
from app.services.storage import storage_service


async def test_dataset_delete_defers_storage_cleanup_until_database_commit(
    db_session, super_admin, monkeypatch
):
    db = db_session
    dataset = Dataset(
        display_id=f"D-{uuid.uuid4().hex[:10]}",
        name="Retained until commit",
        created_by=super_admin[0].id,
    )
    db.add(dataset)
    await db.commit()
    delete_object = MagicMock()
    delete_prefix = MagicMock()
    monkeypatch.setattr(storage_service, "delete_object", delete_object)
    monkeypatch.setattr(storage_service, "delete_prefix", delete_prefix)
    monkeypatch.setattr(
        db, "flush", AsyncMock(side_effect=RuntimeError("foreign key rejection"))
    )

    with pytest.raises(RuntimeError, match="foreign key rejection"):
        await DatasetService(db).delete(dataset.id)

    assert dataset in db.deleted
    delete_object.assert_not_called()
    delete_prefix.assert_not_called()
