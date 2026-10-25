"""Public refresh keeps ORM expiry semantics without synchronous database IO."""

import uuid

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import make_transient_to_detached

from app.application import annotation_cross_frame
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.services.labeling_core_read_relations import task_row
from app.services import labeling_core_read_relations


@pytest.mark.asyncio
@pytest.mark.parametrize("annotation", [False, True])
@pytest.mark.parametrize("missing", [False, True])
async def test_public_refresh_uses_identity_and_discards_pending_target_changes(
    monkeypatch, annotation, missing
):
    model = Annotation if annotation else Task
    identifier = uuid.uuid4()
    original = model(id=identifier, version=1)
    make_transient_to_detached(original)
    async with AsyncSession() as db:
        row = await db.merge(original, load=False)
        row.version = 2
        calls = []

        async def read_core(_db, _model, row_id, *, populate_existing):
            assert row_id == identifier and populate_existing is True
            assert "version" in inspect(row).expired_attributes
            assert not inspect(row).attrs.version.history.has_changes()
            calls.append(row_id)
            return None if missing else row

        module = annotation_cross_frame if annotation else labeling_core_read_relations
        monkeypatch.setattr(module, "get_core_row", read_core)
        operation = (
            module._refresh_annotation(db, row)
            if annotation
            else module.refresh_core_row(db, task_row, row)
        )
        if missing:
            with pytest.raises(InvalidRequestError, match="Could not refresh"):
                await operation
        else:
            await operation
        assert calls == [identifier]
