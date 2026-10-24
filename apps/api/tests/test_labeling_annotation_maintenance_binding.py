from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import make_transient_to_detached

from aap_labeling.annotation_maintenance_contract import DraftData, MaintenanceDraft
from app.db.models.task_lock import AnnotationDraft
from app.services.labeling_annotation_maintenance import (
    LabelingAnnotationMaintenanceService,
)


@pytest.mark.asyncio
async def test_save_draft_preserves_python_json_equality_and_pending_other_fields():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snapshot = MaintenanceDraft(
        id=uuid4(),
        task_id=uuid4(),
        annotation_id=None,
        user_id=uuid4(),
        result={"nested": [True, {"value": False}]},
        was_postponed=False,
        created_at=at,
        updated_at=at,
    )
    async with AsyncSession(autoflush=False, expire_on_commit=False) as db:
        draft = AnnotationDraft(**snapshot.model_dump())
        make_transient_to_detached(draft)
        draft = await db.merge(draft, load=False)
        draft.was_postponed = True
        db.flush = AsyncMock()
        service = LabelingAnnotationMaintenanceService(db)
        requests = []

        async def command(request):
            requests.append(request)
            return DraftData(operation=request.operation, draft=snapshot)

        service._command = command
        result = {"nested": [1, {"value": 0}]}
        saved = await service.save_draft(snapshot.task_id, snapshot.user_id, result)
        assert saved is draft
        assert requests[-1].write_result is False
        assert draft.result is result
        assert draft.updated_at == at
        assert draft.was_postponed is True
        db.flush.assert_awaited_once()
