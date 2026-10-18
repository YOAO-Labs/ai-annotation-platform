from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from aap_assets_source.ingest_contract import (
    INGEST_SCHEMA,
    IngestBatchPlan,
    IngestBatchResult,
    IngestCommand,
    IngestContext,
    IngestDescriptor,
    IngestEntryCompletion,
    IngestFinish,
    IngestLayoutItem,
    IngestLayoutPage,
    IngestPlannedEntry,
    IngestProgress,
    IngestReceipt,
    IngestSourceEntry,
)
from aap_assets_source_runtime.database import NativeIngestData


class _Connection:
    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.calls = []

    async def fetchval(self, statement, *arguments):
        self.calls.append((statement, arguments))
        return self.replies.pop(0)


def _success(model) -> str:
    return json.dumps(
        {"schema": INGEST_SCHEMA, "ok": True, "data": model.model_dump(mode="json")},
        separators=(",", ":"),
    )


@pytest.mark.asyncio
async def test_ingest_port_calls_exact_five_native_functions_and_argument_order() -> (
    None
):
    command = IngestCommand(operation_id=uuid4(), actor_id=uuid4(), dataset_id=uuid4())
    batch_id = uuid4()
    digest = hashlib.sha256(b"archive").hexdigest()
    descriptor = IngestDescriptor(
        kind="zip", request_digest=digest, total_in_zip=1, planned_entries=1
    )
    proposal = IngestSourceEntry(
        source_name="nested/image.png",
        relpath="nested/image.png",
        source_key=None,
        file_type="image",
        content_type="image/png",
        file_size=4,
        content_hash=hashlib.md5(b"data", usedforsecurity=False).hexdigest(),
        width=None,
        height=None,
    )
    item_id, publication_id, entry_id = uuid4(), uuid4(), uuid4()
    target = (
        f"dataset-sources/{command.dataset_id}/{item_id}/{publication_id}/"
        "nested/image.png"
    )
    planned = IngestPlannedEntry(
        **proposal.model_dump(),
        entry_id=entry_id,
        index=0,
        status="pending",
        item_id=item_id,
        publication_id=publication_id,
        file_name="image.png",
        file_path=target,
    )
    staged = IngestProgress(
        **command.model_dump(),
        staged=1,
        settled=0,
        added=0,
        deduped=0,
        skipped=0,
        error_count=0,
        linked_tasks=0,
    )
    settled = staged.model_copy(update={"settled": 1, "added": 1, "linked_tasks": 1})
    context = IngestContext(
        **command.model_dump(),
        descriptor=descriptor,
        state="preparing",
        dataset_name="dataset",
        is_temporal=False,
        source_prefix="dataset/",
        next_index=0,
        result=None,
    )
    plan = IngestBatchPlan(
        **command.model_dump(), batch_id=batch_id, entries=[planned], progress=staged
    )
    batch_result = IngestBatchResult(
        **command.model_dump(), batch_id=batch_id, progress=settled
    )
    layout_digest = hashlib.sha256(b"layout").hexdigest()
    page = IngestLayoutPage(
        **command.model_dump(),
        snapshot_digest=layout_digest,
        scene_count=0,
        assigned_count=0,
        total_items=1,
        items=[
            IngestLayoutItem(
                id=item_id,
                file_name="image.png",
                file_path=target,
                file_type="image",
                scene_id=None,
                frame_index=None,
                logical_path="nested/image.png",
            )
        ],
        next_item_id=None,
    )
    finish = IngestFinish(
        layout_snapshot_digest=layout_digest,
        scene_plan=None,
        scene_inference_notes=[],
    )
    receipt = IngestReceipt(
        **command.model_dump(),
        kind="zip",
        request_digest=digest,
        added=1,
        deduped=0,
        skipped=0,
        errors=[],
        error_count=0,
        total_in_zip=1,
        linked_tasks=1,
        scene_inference_notes=[],
    )
    connection = _Connection(
        [_success(value) for value in (context, plan, batch_result, page, receipt)]
    )
    data = NativeIngestData(connection, 16_777_216)

    await data.begin(command, descriptor)
    await data.prepare_batch(command, batch_id, [proposal])
    await data.complete_batch(
        command,
        batch_id,
        [
            IngestEntryCompletion(
                entry_id=entry_id,
                status="ready",
                publication_id=publication_id,
                file_size=4,
                content_hash=proposal.content_hash,
                width=None,
                height=None,
                error_code=None,
            )
        ],
    )
    await data.read_layout(command, after_item_id=None, limit=500)
    await data.finish(command, finish)

    assert [call[0] for call in connection.calls] == [
        "SELECT aap_assets_v1.begin_dataset_ingest($1::uuid, $2::uuid, $3::uuid, $4::jsonb)",
        "SELECT aap_assets_v1.prepare_dataset_ingest_batch($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::jsonb)",
        "SELECT aap_assets_v1.complete_dataset_ingest_batch($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::jsonb)",
        "SELECT aap_assets_v1.read_dataset_ingest_layout($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::integer)",
        "SELECT aap_assets_v1.finish_dataset_ingest($1::uuid, $2::uuid, $3::uuid, $4::jsonb)",
    ]
    assert connection.calls[0][1][:3] == (
        command.operation_id,
        command.actor_id,
        command.dataset_id,
    )
    assert connection.calls[1][1][:4] == (
        command.operation_id,
        command.actor_id,
        command.dataset_id,
        batch_id,
    )
    assert connection.calls[1][1][4] == [proposal.model_dump(mode="json")]
    assert connection.calls[2][1][:4] == connection.calls[1][1][:4]
    assert connection.calls[3][1] == (
        command.operation_id,
        command.actor_id,
        command.dataset_id,
        None,
        500,
    )
    assert connection.calls[4][1][:3] == connection.calls[0][1][:3]
