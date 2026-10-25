import json
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from aap_labeling.annotation_write_contract import (
    ANNOTATION_WRITE_PROTOCOL,
    ReadCreateReceipt,
)
from app.services.labeling_annotation_data import annotation_write_command


class _AsyncpgError(Exception):
    sqlstate = "P0001"
    message = "aap_annotation_write_error"
    detail = json.dumps(
        {
            "status_code": 409,
            "detail": {"reason": "track_frame_absent", "message": "frame is absent"},
        }
    )


class _AdaptedError(Exception):
    def __init__(self, orig):
        self.orig = orig


class _Database:
    async def scalar(self, *_args, **_kwargs):
        raise DBAPIError("SELECT", {}, _AdaptedError(_AsyncpgError()), False)


@pytest.mark.asyncio
async def test_wrapped_asyncpg_business_error_keeps_scene_track_conflict_detail():
    request = ReadCreateReceipt(
        schema=ANNOTATION_WRITE_PROTOCOL,
        operation="read_create_receipt",
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        idempotency_key="key",
        request_digest="a" * 64,
    )

    with pytest.raises(HTTPException) as captured:
        await annotation_write_command(_Database(), request)

    assert captured.value.status_code == 409
    assert captured.value.detail == {
        "reason": "track_frame_absent",
        "message": "frame is absent",
    }
