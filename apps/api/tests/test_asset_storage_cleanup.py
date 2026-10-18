from __future__ import annotations

import uuid
from collections.abc import Iterable

import pytest
from pydantic import ValidationError

from app.services.asset_storage_cleanup import drain_asset_storage_deletions
from app.services.asset_storage_cleanup_contract import AssetStorageDeletion


BUCKET = "media-cache-test"
OPERATION_ID = uuid.uuid4()
ASSET_ID = uuid.uuid4()
PREFIX = f"image-pyramids/{ASSET_ID}/"


def _row(**overrides) -> dict:
    value = {
        "operation_id": OPERATION_ID,
        "asset_id": ASSET_ID,
        "bucket": BUCKET,
        "prefix": PREFIX,
    }
    value.update(overrides)
    return value


class _Rows:
    def __init__(self, rows: Iterable[dict]):
        self._rows = tuple(rows)

    def mappings(self):
        return iter(self._rows)


class _FakeDb:
    def __init__(self, rows: Iterable[dict], acknowledgements: Iterable[object] = ()):
        self.pending_rows = [dict(row) for row in rows]
        self.acknowledgements = list(acknowledgements)
        self.events: list[tuple[str, object]] = []
        self.active = False

    def in_transaction(self) -> bool:
        return self.active

    async def execute(self, statement, params: dict):
        sql = str(statement)
        self.events.append(("pending", (sql, params)))
        self.active = True
        return _Rows(self.pending_rows)

    async def scalar(self, statement, params: dict):
        self.events.append(("complete", (str(statement), params)))
        self.active = True
        result = self.acknowledgements.pop(0) if self.acknowledgements else True
        if isinstance(result, Exception):
            raise result
        if result is True:
            self.pending_rows = [
                row
                for row in self.pending_rows
                if not (
                    row["operation_id"] == params["operation"]
                    and row["asset_id"] == params["asset"]
                )
            ]
        return result

    async def commit(self) -> None:
        self.events.append(("commit", None))
        self.active = False

    async def rollback(self) -> None:
        self.events.append(("rollback", None))
        self.active = False


class _FakeStorage:
    def __init__(self, db: _FakeDb, *, bucket: str = BUCKET, failures: int = 0):
        self.db = db
        self.media_cache_bucket = bucket
        self.failures = failures
        self.calls: list[tuple[str, str, bool]] = []

    def delete_prefix(self, prefix: str, *, bucket: str) -> None:
        self.calls.append((prefix, bucket, self.db.active))
        self.db.events.append(("delete", (prefix, bucket)))
        if self.failures:
            self.failures -= 1
            raise OSError("temporary object-store failure")


@pytest.mark.asyncio
async def test_cleanup_rejects_an_active_business_transaction_without_io():
    db = _FakeDb([_row()])
    db.active = True
    storage = _FakeStorage(db)

    with pytest.raises(RuntimeError, match="after the business transaction commits"):
        await drain_asset_storage_deletions(db, storage)

    assert db.events == []
    assert storage.calls == []
    assert db.pending_rows


@pytest.mark.asyncio
async def test_cleanup_commits_exact_pending_page_before_object_io():
    db = _FakeDb([_row()])
    storage = _FakeStorage(db)

    result = await drain_asset_storage_deletions(
        db, storage, operation_id=OPERATION_ID, limit=23
    )

    assert result == {"completed": 1, "failed": 0}
    assert storage.calls == [(PREFIX, BUCKET, False)]
    assert [event[0] for event in db.events] == [
        "pending",
        "commit",
        "delete",
        "complete",
        "commit",
    ]
    pending_sql, pending_params = db.events[0][1]
    assert "aap_assets_v1.pending_storage_deletions" in pending_sql
    assert "public.asset_storage_deletions" not in pending_sql
    assert pending_params == {"operation": OPERATION_ID, "limit": 23}
    complete_sql, complete_params = db.events[3][1]
    assert "aap_assets_v1.complete_storage_deletion" in complete_sql
    assert complete_params == {"operation": OPERATION_ID, "asset": ASSET_ID}
    assert db.pending_rows == []


@pytest.mark.asyncio
async def test_bucket_mismatch_leaves_outbox_pending_without_object_io():
    db = _FakeDb([_row()])
    storage = _FakeStorage(db, bucket="other-cache")

    result = await drain_asset_storage_deletions(db, storage)

    assert result == {"completed": 0, "failed": 1}
    assert storage.calls == []
    assert db.pending_rows == [_row()]
    assert [event[0] for event in db.events] == ["pending", "commit"]


@pytest.mark.asyncio
async def test_failed_object_delete_remains_pending_and_a_later_drain_recovers():
    db = _FakeDb([_row()])
    storage = _FakeStorage(db, failures=1)

    first = await drain_asset_storage_deletions(db, storage, operation_id=OPERATION_ID)

    assert first == {"completed": 0, "failed": 1}
    assert len(db.pending_rows) == 1
    assert db.events[-1][0] == "rollback"

    second = await drain_asset_storage_deletions(db, storage, operation_id=OPERATION_ID)

    assert second == {"completed": 1, "failed": 0}
    assert len(storage.calls) == 2
    assert db.pending_rows == []
    assert all(not transaction_active for _, _, transaction_active in storage.calls)


@pytest.mark.asyncio
async def test_failed_acknowledgement_stays_pending_until_idempotent_retry():
    db = _FakeDb([_row()], acknowledgements=[False, True])
    storage = _FakeStorage(db)

    first = await drain_asset_storage_deletions(db, storage, operation_id=OPERATION_ID)

    assert first == {"completed": 0, "failed": 1}
    assert len(db.pending_rows) == 1
    assert db.events[-1][0] == "rollback"

    second = await drain_asset_storage_deletions(db, storage, operation_id=OPERATION_ID)

    assert second == {"completed": 1, "failed": 0}
    assert len(storage.calls) == 2
    assert db.pending_rows == []


@pytest.mark.asyncio
async def test_invalid_prefix_reply_is_rejected_before_object_io():
    db = _FakeDb([_row(prefix="image-pyramids/unrelated/")])
    storage = _FakeStorage(db)

    with pytest.raises(ValidationError, match="outside its frozen asset prefix"):
        await drain_asset_storage_deletions(db, storage)

    assert storage.calls == []
    assert db.events[-1][0] == "rollback"
    assert db.pending_rows == [_row(prefix="image-pyramids/unrelated/")]


def test_storage_reference_is_frozen_and_rejects_extra_or_invalid_scope():
    reference = AssetStorageDeletion.model_validate(_row())
    assert reference.prefix == PREFIX

    with pytest.raises(ValidationError):
        AssetStorageDeletion.model_validate(_row(private_path="other/"))
    with pytest.raises(ValidationError):
        AssetStorageDeletion.model_validate(_row(bucket="127.0.0.1"))
    with pytest.raises(ValidationError):
        AssetStorageDeletion.model_validate(_row(prefix="image-pyramids/not-a-uuid/"))
