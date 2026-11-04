from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import pytest

from yoao_assets_source.ingest_contract import (
    INGEST_SCHEMA,
    SCAN_REQUEST_DIGEST,
    IngestBatchPlan,
    IngestCommand,
    IngestContext,
    IngestDescriptor,
    IngestFailure,
    IngestProgress,
)
from app.services.assets_source_ingest import DatasetSourceIngestData

pytestmark = pytest.mark.asyncio

ACTOR = UUID("10000000-0000-4000-8000-000000000001")
DATASET = UUID("20000000-0000-4000-8000-000000000002")
OPERATION = UUID("30000000-0000-4000-8000-000000000003")


def _command() -> IngestCommand:
    return IngestCommand(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
        authority={
            "credential_type": "api_key",
            "credential_id": uuid4(),
            "generation": None,
            "expires_at": None,
            "api_key_revision": "a" * 64,
            "api_key_scopes": ["datasets:write"],
            "dataset_lifecycle_revision": 1,
            "directory_protected": False,
        },
    )


def _success(data) -> dict:
    return {
        "schema": INGEST_SCHEMA,
        "ok": True,
        "data": data.model_dump(mode="json"),
    }


def _descriptor() -> IngestDescriptor:
    return IngestDescriptor(
        kind="scan",
        request_digest=SCAN_REQUEST_DIGEST,
        total_in_zip=None,
        planned_entries=None,
    )


async def test_begin_rejects_a_native_reply_for_another_actor():
    descriptor = _descriptor()
    context = IngestContext(
        operation_id=OPERATION,
        actor_id=uuid4(),
        dataset_id=DATASET,
        descriptor=descriptor,
        state="preparing",
        dataset_name="dataset-a",
        is_temporal=False,
        source_prefix="dataset-a/",
        next_index=0,
        result=None,
    )
    db = SimpleNamespace(scalar=AsyncMock(return_value=_success(context)))

    with pytest.raises(IngestFailure) as raised:
        await DatasetSourceIngestData(db).begin(_command(), descriptor)

    assert raised.value.code == "invalid_response"


async def test_prepare_rejects_a_plan_for_another_batch():
    command = _command()
    batch_id = uuid4()
    progress = IngestProgress(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
        staged=0,
        settled=0,
        added=0,
        deduped=0,
        skipped=0,
        error_count=0,
        linked_tasks=0,
    )
    plan = IngestBatchPlan(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
        batch_id=uuid4(),
        entries=[],
        progress=progress,
    )
    db = SimpleNamespace(scalar=AsyncMock(return_value=_success(plan)))

    with pytest.raises(IngestFailure) as raised:
        await DatasetSourceIngestData(db).prepare_batch(command, batch_id, [])

    assert raised.value.code == "invalid_response"
