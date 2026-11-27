from __future__ import annotations

import uuid
from unittest.mock import MagicMock

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
    real_flush = db.flush

    async def fail_after_delete(*args, **kwargs):
        if dataset in db.deleted:
            raise RuntimeError("foreign key rejection")
        return await real_flush(*args, **kwargs)

    monkeypatch.setattr(db, "flush", fail_after_delete)

    with pytest.raises(RuntimeError, match="foreign key rejection"):
        await DatasetService(db).delete(dataset.id)

    assert dataset in db.deleted
    delete_object.assert_not_called()
    delete_prefix.assert_not_called()
