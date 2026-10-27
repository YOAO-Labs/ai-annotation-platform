"""Caller contracts for sealed originals: real transaction boundary, fake storage."""

import hashlib
import io
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from fastapi import HTTPException
import pytest

from app.db.models.dataset import Dataset
from app.services import dataset as datasets
from app.services import dataset_assets
from app.services import dataset_references
from app.services import content_snapshot
from app.workers.dataset_import import _ImportByteBudget


@pytest.fixture
def creation(monkeypatch):
    import app.services

    module = ModuleType("app.services.managed_original_creation")
    allocation = {
        "id": uuid.uuid4(),
        "logical_path": "folder/image.png",
        "bucket": "datasets-test",
        "object_key": "allocated/final.txt",
    }
    payload = b"sealed bytes"
    receipt = SimpleNamespace(
        creation_id=allocation["id"],
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        md5=hashlib.md5(payload).hexdigest(),
        etag="actual-etag",
        version_id=None,
        head_bytes=payload,
    )
    module.allocate = AsyncMock(return_value=allocation)
    module.create_stream = AsyncMock(return_value=receipt)
    module.attach = AsyncMock()
    module.original_locator = Mock(return_value="saved-binding")
    module.original_ref = Mock(return_value={"schema": 2})
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(
        app.services, "managed_original_creation", module, raising=False
    )
    return module, allocation, receipt


@pytest.fixture
async def ingest(db_session, super_admin, monkeypatch):
    dataset_id = uuid.uuid4()
    db_session.add(
        Dataset(
            id=dataset_id,
            display_id="D-IMP-" + dataset_id.hex[:8],
            name="importer-caller-" + dataset_id.hex[:8],
            created_by=super_admin[0].id,
        )
    )
    await db_session.flush()
    service = datasets.DatasetService(db_session)
    item = SimpleNamespace(id=uuid.uuid4(), metadata_={})
    service._unique_file_name = AsyncMock(return_value="folder/file.txt")
    service._find_by_file_path = AsyncMock(return_value=None)
    service.add_item = AsyncMock(return_value=item)
    service.create_tasks_for_items = AsyncMock(return_value=2)
    monkeypatch.setattr(datasets, "validate_dataset_write", AsyncMock())
    monkeypatch.setattr(datasets, "find_verified_content", AsyncMock(return_value=None))
    monkeypatch.setattr(datasets.storage_service, "delete_object", Mock())
    return service, dataset_id, item


async def test_connector_attaches_only_verified_creation_in_caller_transaction(
    creation, ingest
):
    originals, allocation, receipt = creation
    service, dataset_id, item = ingest
    actor_id = uuid.uuid4()
    authority = {"connection_revision": 4}
    stream = io.BytesIO(b"sealed bytes")
    outcome = await service.ingest_one(
        dataset_id,
        "source/file.txt",
        stream,
        size=1,
        max_bytes=50,
        creation_requested_by=actor_id,
        creation_authority=authority,
    )
    assert outcome.status == "added" and outcome.file_size == receipt.size_bytes
    assert outcome.linked_tasks == 2
    originals.allocate.assert_awaited_once_with(
        service.db,
        dataset_id=dataset_id,
        logical_path="folder/file.txt",
        origin="connector",
        max_bytes=50,
        content_type="text/plain",
        requested_by=actor_id,
        authority=authority,
    )
    originals.create_stream.assert_awaited_once_with(
        service.db, allocation, stream, content_type="text/plain"
    )
    fields = service.add_item.await_args.kwargs
    assert fields["file_path"] == allocation["object_key"]
    assert fields["sha256"] == receipt.sha256
    assert fields["object_etag"] == receipt.etag
    originals.attach.assert_awaited_once_with(service.db, item, receipt.creation_id)
    datasets.storage_service.delete_object.assert_not_called()


async def test_connector_dedupe_retains_unlinked_creation(
    creation, ingest, monkeypatch
):
    originals, _, receipt = creation
    service, dataset_id, _ = ingest
    duplicate = SimpleNamespace(id=uuid.uuid4(), file_type="other")
    monkeypatch.setattr(
        datasets, "find_verified_content", AsyncMock(return_value=duplicate)
    )
    outcome = await service.ingest_one(
        dataset_id, "file.txt", io.BytesIO(b"sealed bytes"), max_bytes=12
    )
    assert outcome.status == "skipped" and outcome.item_id == duplicate.id
    assert outcome.file_size == receipt.size_bytes
    service.add_item.assert_not_awaited()
    originals.attach.assert_not_awaited()
    datasets.storage_service.delete_object.assert_not_called()


@pytest.mark.parametrize("failure", ["authority", "business", "attach"])
async def test_connector_failures_never_delete_sealed_final(
    creation, ingest, monkeypatch, failure
):
    originals, _, _ = creation
    service, dataset_id, _ = ingest
    if failure == "authority":
        datasets.validate_dataset_write.side_effect = [None, HTTPException(403)]
    elif failure == "business":
        service.add_item.side_effect = RuntimeError("business rollback")
    else:
        originals.attach.side_effect = RuntimeError("link rollback")
    with pytest.raises((HTTPException, RuntimeError)):
        await service.ingest_one(
            dataset_id, "file.txt", io.BytesIO(b"sealed bytes"), max_bytes=12
        )
    originals.create_stream.assert_awaited_once()
    datasets.storage_service.delete_object.assert_not_called()


async def test_scan_of_existing_object_never_invents_creation(creation, ingest):
    originals, _, _ = creation
    service, dataset_id, _ = ingest
    outcome = await service.ingest_one(
        dataset_id, "old/file.txt", storage_key="existing/file.txt", size=123
    )
    assert outcome.status == "added" and outcome.file_size == 123
    originals.allocate.assert_not_awaited()
    originals.create_stream.assert_not_awaited()
    originals.attach.assert_not_awaited()
    assert service.add_item.await_args.kwargs["sha256"] is None


@pytest.mark.parametrize("mismatch", [None, "sha256", "size_bytes"])
async def test_directory_copy_rechecks_sealed_creation_without_preview_ownership(
    creation, monkeypatch, mismatch
):
    originals, allocation, receipt = creation
    db = SimpleNamespace(flush=AsyncMock())
    dataset = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4(), metadata_={})
    service = SimpleNamespace(
        _existing_file_names=AsyncMock(return_value=set()),
        add_item=AsyncMock(return_value=item),
        create_tasks_for_items=AsyncMock(),
    )
    monkeypatch.setattr(datasets, "DatasetService", lambda _db: service)
    monkeypatch.setattr(
        dataset_assets, "find_verified_content", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(datasets.storage_service, "delete_object", Mock())
    source = SimpleNamespace(id=uuid.uuid4())
    entry = SimpleNamespace(relative_path="folder/image.png", observed={})
    snapshot = SimpleNamespace(
        file=io.BytesIO(b"sealed bytes"),
        sha256=receipt.sha256,
        size=receipt.size_bytes,
        media_type="image/png",
        width=16,
        height=8,
    )
    if mismatch == "sha256":
        receipt.sha256 = "0" * 64
    elif mismatch == "size_bytes":
        receipt.size_bytes += 1
    uploaded_previews = []
    call = dataset_references.apply_copy(
        db,
        source,
        entry,
        dataset,
        snapshot,
        {"allocation": allocation, "created": receipt},
        uploaded_previews,
        force_new=True,
        requested_by=source.id,
        authority={"source_id": str(source.id)},
    )
    if mismatch:
        with pytest.raises(
            content_snapshot.ContentError, match="reference_content_changed"
        ):
            await call
        service.add_item.assert_not_awaited()
        originals.attach.assert_not_awaited()
    else:
        result = await call
        assert result == {
            "item_id": item.id,
            "created": True,
            "sha256": snapshot.sha256,
        }
        originals.attach.assert_awaited_once_with(db, item, receipt.creation_id)
        fields = service.add_item.await_args
        assert fields.args[2] == allocation["object_key"]
        assert fields.kwargs["content_hash"] == receipt.md5
        assert (
            fields.kwargs["storage_binding"] is originals.original_locator.return_value
        )
        assert fields.kwargs["storage_ref"] is originals.original_ref.return_value
        originals.original_locator.assert_called_once_with(allocation)
        originals.original_ref.assert_called_once_with(allocation)
    originals.allocate.assert_not_awaited()
    assert uploaded_previews == []
    datasets.storage_service.delete_object.assert_not_called()


def test_actual_import_budget_accumulates_across_entries_and_failures():
    budget = _ImportByteBudget(5)
    first = budget.stream(io.BytesIO(b"abc"))
    assert first.read(100) == b"abc"
    assert budget.remaining == 2
    # A caller failure after this read must not refund bytes to the next entry.
    second = budget.stream(io.BytesIO(b"de"))
    assert second.read(100) == b"de"
    assert second.read(100) == b""
    assert budget.remaining == 0
    assert budget.stream(io.BytesIO()).read(100) == b""
    with pytest.raises(content_snapshot.ContentError) as error:
        budget.stream(io.BytesIO(b"f" * 100)).read(100)
    assert error.value.code == "creation_byte_budget_exceeded"
    assert error.value.status == 413 and budget.exceeded
    assert budget.consumed == 6


def test_budget_reads_only_one_extra_byte_to_detect_underreported_source_size():
    budget = _ImportByteBudget(4)
    raw = io.BytesIO(b"x" * 1000)
    with pytest.raises(content_snapshot.ContentError):
        budget.stream(raw).read(1024 * 1024)
    assert raw.tell() == 5 and budget.consumed == 5


@pytest.mark.parametrize("limit", [True, False, -1, "1", None])
def test_actual_import_budget_rejects_invalid_limits(limit):
    with pytest.raises(ValueError):
        _ImportByteBudget(limit)


def test_staging_namespace_is_reserved_for_dataset_names():
    with pytest.raises(HTTPException) as error:
        datasets.ensure_dataset_storage_name_is_writable(
            "__aap_upload_staging__/payload"
        )
    assert error.value.status_code == 422
