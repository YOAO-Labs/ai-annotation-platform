from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_source.operator_contract import (
    OPERATOR_SCHEMA,
    AppendOperatorItemsRequest,
    CreateOperatorImageDatasetRequest,
    OperatorDataset,
    OperatorDatasetMutation,
    OperatorDimensions,
    OperatorFailure,
    OperatorItem,
    OperatorItemsMutation,
    OperatorItemsPage,
    OperatorSourceEntry,
    OperatorSourcePatch,
    ReadOperatorItemsRequest,
)
from app.services import assets_operator
from app.services.assets_operator import AssetsOperatorGateway


def _wire(value):
    return {
        "schema": OPERATOR_SCHEMA,
        "ok": True,
        "data": value.model_dump(mode="json"),
    }


def _entry(**values):
    return OperatorSourceEntry(
        id=values.pop("id", uuid4()),
        file_name="frame.jpg",
        file_path="custom/frame.jpg",
        file_type="image",
        **values,
    )


def _item(dataset_id, entry):
    return OperatorItem(
        **entry.model_dump(),
        dataset_id=dataset_id,
        scene_id=None,
        frame_index=None,
        source_snapshot="a" * 64,
    )


@pytest.mark.asyncio
async def test_operator_native_binding_flushes_pending_state_but_never_commits():
    entry = _entry()
    request = AppendOperatorItemsRequest(
        operation_id=uuid4(), dataset_id=uuid4(), entries=[entry]
    )
    expected = OperatorItemsMutation(
        operation_id=request.operation_id,
        dataset_id=request.dataset_id,
        action="append_items",
        items=[_item(request.dataset_id, entry)],
        file_count=1,
        dimensions_backfilled=0,
    )
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: _wire(expected))

    actual = await AssetsOperatorGateway(db).append_operator_items(request)

    assert actual == expected
    assert [call[0] for call in db.mock_calls[:2]] == ["flush", "execute"]
    statement, parameters = db.execute.await_args.args
    assert str(statement) == (
        "SELECT aap_assets_v1.append_operator_items(CAST(:operation_id AS uuid), "
        "CAST(:dataset_id AS uuid), CAST(:entries AS jsonb))"
    )
    assert parameters["operation_id"] == request.operation_id
    assert json.loads(parameters["entries"]) == [entry.model_dump(mode="json")]
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_rejects_invalid_result_before_caller_can_use_it():
    request = CreateOperatorImageDatasetRequest(
        operation_id=uuid4(), created_by=uuid4(), name="images", description="files"
    )
    now = datetime.now(timezone.utc)
    dataset = OperatorDataset(
        id=request.operation_id,
        display_id=f"DS-{request.operation_id.hex[:6].upper()}",
        name=request.name,
        description=request.description,
        data_type="image",
        is_temporal=False,
        file_count=0,
        created_by=uuid4(),
        metadata={},
        created_at=now,
        updated_at=now,
    )
    expected = OperatorDatasetMutation(
        operation_id=request.operation_id,
        dataset_id=request.operation_id,
        action="create_image_dataset",
        dataset=dataset,
        storage_root=None,
        needs_pcd_upgrade=None,
    )
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: _wire(expected))
    with pytest.raises(OperatorFailure) as rejected:
        await AssetsOperatorGateway(db).create_operator_image_dataset(request)
    assert rejected.value.code == "invalid_response"
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_append_chunks_by_count_and_keeps_global_order():
    entries = [_entry() for _ in range(503)]
    dataset_id = uuid4()
    gateway = AssetsOperatorGateway(AsyncMock(spec=AsyncSession))
    requests = []

    async def append(request):
        requests.append(request)
        return OperatorItemsMutation(
            operation_id=request.operation_id,
            dataset_id=dataset_id,
            action="append_items",
            items=[_item(dataset_id, entry) for entry in request.entries],
            file_count=sum(len(part.entries) for part in requests),
            dimensions_backfilled=0,
        )

    gateway.append_operator_items = append

    result = await gateway.append_all_items(dataset_id, entries)

    assert [len(part.entries) for part in requests] == [500, 3]
    assert [item.id for item in result] == [item.id for item in entries]
    assert len({part.operation_id for part in requests}) == 2


@pytest.mark.asyncio
async def test_operator_append_chunks_by_encoded_size(monkeypatch):
    monkeypatch.setattr(assets_operator, "OPERATOR_REQUEST_BYTES", 2048)
    entries = [_entry(metadata={"value": "x" * 600}) for _ in range(3)]
    dataset_id = uuid4()
    gateway = AssetsOperatorGateway(AsyncMock(spec=AsyncSession))
    requests = []

    async def append(request):
        requests.append(request)
        assert len(assets_operator._json(request).encode()) <= 2048
        return OperatorItemsMutation(
            operation_id=request.operation_id,
            dataset_id=dataset_id,
            action="append_items",
            items=[_item(dataset_id, entry) for entry in request.entries],
            file_count=len(requests),
            dimensions_backfilled=0,
        )

    gateway.append_operator_items = append
    result = await gateway.append_all_items(dataset_id, entries)
    assert [len(part.entries) for part in requests] == [1, 1, 1]
    assert len(result) == 3


@pytest.mark.asyncio
async def test_operator_empty_batches_do_not_issue_sql():
    db = AsyncMock(spec=AsyncSession)
    gateway = AssetsOperatorGateway(db)
    assert await gateway.append_all_items(uuid4(), []) == ()
    assert await gateway.patch_all_items(uuid4(), []) == 0
    db.execute.assert_not_awaited()
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_reads_complete_pages_beyond_a_single_response_budget(
    monkeypatch,
):
    monkeypatch.setattr(assets_operator, "OPERATOR_REPLY_BYTES", 2000)
    dataset_id = uuid4()
    entries = sorted(
        [_entry(metadata={"value": "x" * 1000}) for _ in range(3)],
        key=lambda entry: entry.id,
    )
    gateway = AssetsOperatorGateway(AsyncMock(spec=AsyncSession))
    requests = []

    async def page(request):
        index = len(requests)
        requests.append(request)
        return OperatorItemsPage(
            dataset_id=dataset_id,
            after_item_id=request.after_item_id,
            next_item_id=entries[index].id if index < 2 else None,
            snapshot_digest="a" * 64,
            total=3,
            complete=index == 2,
            items=[_item(dataset_id, entries[index])],
        )

    gateway.read_operator_items = page
    result = await gateway.read_all_items(dataset_id)
    assert [item.id for item in result] == [entry.id for entry in entries]
    assert sum(len(item.model_dump_json()) for item in result) > 2000
    assert requests[1].after_item_id == entries[0].id


@pytest.mark.asyncio
async def test_operator_read_discards_results_when_snapshot_changes():
    dataset_id = uuid4()
    entries = sorted([_entry(), _entry()], key=lambda entry: entry.id)
    gateway = AssetsOperatorGateway(AsyncMock(spec=AsyncSession))
    index = 0

    async def page(request):
        nonlocal index
        current = index
        index += 1
        return OperatorItemsPage(
            dataset_id=dataset_id,
            after_item_id=request.after_item_id,
            next_item_id=entries[current].id if current == 0 else None,
            snapshot_digest=("a" if current == 0 else "b") * 64,
            total=2,
            complete=current == 1,
            items=[_item(dataset_id, entries[current])],
        )

    gateway.read_operator_items = page
    with pytest.raises(OperatorFailure) as rejected:
        await gateway.read_all_items(dataset_id)
    assert rejected.value.code == "source_changed"


@pytest.mark.asyncio
async def test_operator_page_rejects_excess_record_bytes_inside_reply_limit(
    monkeypatch,
):
    monkeypatch.setattr(assets_operator, "OPERATOR_PAGE_BYTES", 1000)
    dataset_id = uuid4()
    entry = _entry(metadata={"value": "x" * 1100})
    page = OperatorItemsPage(
        dataset_id=dataset_id,
        after_item_id=None,
        next_item_id=None,
        snapshot_digest="a" * 64,
        total=1,
        complete=True,
        items=[_item(dataset_id, entry)],
    )
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: _wire(page))
    with pytest.raises(OperatorFailure) as rejected:
        await AssetsOperatorGateway(db).read_operator_items(
            ReadOperatorItemsRequest(dataset_id=dataset_id)
        )
    assert rejected.value.code == "invalid_response"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("had_dimensions", [False, True])
async def test_operator_patch_checks_captured_dimension_condition(had_dimensions):
    dataset_id = uuid4()
    entry = _entry(
        width=320 if had_dimensions else None,
        height=200 if had_dimensions else None,
        metadata={"keep": True},
    )
    before = _item(dataset_id, entry)
    patch = OperatorSourcePatch(
        id=entry.id,
        source_snapshot=before.source_snapshot,
        dimensions=OperatorDimensions(width=640, height=480),
    )
    gateway = AssetsOperatorGateway(AsyncMock(spec=AsyncSession))

    async def correct(request):
        item = (
            before
            if had_dimensions
            else before.model_copy(update={"width": 640, "height": 480})
        )
        return OperatorItemsMutation(
            operation_id=request.operation_id,
            dataset_id=dataset_id,
            action="patch_items",
            items=[item],
            file_count=1,
            dimensions_backfilled=int(not had_dimensions),
        )

    gateway.patch_operator_items = correct
    assert await gateway.patch_all_items(
        dataset_id, [patch], observed_items={before.id: before}
    ) == int(not had_dimensions)

    async def wrong(request):
        return OperatorItemsMutation(
            operation_id=request.operation_id,
            dataset_id=dataset_id,
            action="patch_items",
            items=[before.model_copy(update={"width": 123, "height": 456})],
            file_count=1,
            dimensions_backfilled=0,
        )

    gateway.patch_operator_items = wrong
    with pytest.raises(OperatorFailure) as rejected:
        await gateway.patch_all_items(
            dataset_id, [patch], observed_items={before.id: before}
        )
    assert rejected.value.code == "invalid_response"
