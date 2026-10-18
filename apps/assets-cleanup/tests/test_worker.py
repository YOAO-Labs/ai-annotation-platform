import asyncio
from datetime import datetime, timezone
import threading
from uuid import UUID, uuid4

import pytest

from aap_assets_cleanup.contract import (
    DeletionCursor,
    ImageGarbageClaim,
    PendingAssetStorageDeletion,
)
from aap_assets_cleanup.object_store import CleanupStopped
from aap_assets_cleanup.worker import drain_image_garbage, drain_pending


def _row(created_at: datetime, *, bucket: str = "media-cache"):
    asset_id = uuid4()
    return PendingAssetStorageDeletion.model_validate(
        {
            "operation_id": uuid4(),
            "asset_id": asset_id,
            "bucket": bucket,
            "prefix": f"image-pyramids/{asset_id}/",
            "created_at": created_at,
        }
    )


def _key(row):
    return row.created_at, row.operation_id.int, row.asset_id.int


class _Data:
    def __init__(self, rows, events=None, *, ack_failures=(), ignore_bucket=False):
        self.rows = tuple(sorted(rows, key=_key))
        self.events = events if events is not None else []
        self.completed: set[tuple[UUID, UUID]] = set()
        self.ack_failures = set(ack_failures)
        self.ignore_bucket = ignore_bucket
        self.pending_cursors = []
        self.ack_calls = []

    async def pending(self, *, bucket, cursor, limit):
        self.pending_cursors.append(cursor)
        self.events.append(("pending", cursor))
        after = _key(cursor) if cursor is not None else None
        page = tuple(
            row
            for row in self.rows
            if (self.ignore_bucket or row.bucket == bucket)
            and (row.operation_id, row.asset_id) not in self.completed
            and (after is None or _key(row) > after)
        )[:limit]
        self.events.append(("read_committed", cursor))
        return page

    async def complete(self, reference):
        identity = reference.operation_id, reference.asset_id
        self.events.append(("ack", identity))
        self.ack_calls.append(identity)
        if identity in self.ack_failures:
            self.ack_failures.remove(identity)
            raise OSError("ack unavailable")
        self.completed.add(identity)


class _Storage:
    def __init__(self, events, *, failures=(), stop_after=None):
        self.events = events
        self.failures = set(failures)
        self.stop_after = stop_after
        self.calls = []

    def delete_prefix(self, prefix, stop):
        self.events.append(("delete", prefix))
        self.calls.append(prefix)
        if prefix in self.failures:
            self.failures.remove(prefix)
            raise OSError("storage unavailable")
        if self.stop_after == prefix:
            stop.set()
            raise CleanupStopped("stopped")


class _ImageData:
    def __init__(self, claims, events=None, *, ack_failures=()):
        self.claims = list(claims)
        self.events = events if events is not None else []
        self.ack_failures = set(ack_failures)
        self.ack_calls = []
        self.claim_calls = []

    async def claim_image_garbage(self, *, worker_id, lease_seconds, batch_size):
        self.claim_calls.append((worker_id, lease_seconds, batch_size))
        self.events.append(("claim", worker_id))
        return self.claims.pop(0) if self.claims else ()

    async def complete_image_garbage(self, claim):
        identity = claim.garbage_id, claim.lease_token
        self.events.append(("ack_image", identity))
        self.ack_calls.append(identity)
        if identity in self.ack_failures:
            self.ack_failures.remove(identity)
            raise OSError("ack unavailable")


class _ImageStorage(_Storage):
    def __init__(self, events, objects=(), *, late_put=None, **kwargs):
        super().__init__(events, **kwargs)
        self.objects = set(objects)
        self.late_put = late_put

    def delete_prefix(self, prefix, stop):
        super().delete_prefix(prefix, stop)
        self.objects = {key for key in self.objects if not key.startswith(prefix)}
        if self.late_put is not None:
            key, self.late_put = self.late_put, None
            self.objects.add(key)


@pytest.mark.asyncio
async def test_keyset_advances_past_failed_row_and_drains_later_pages():
    timestamp = datetime(2026, 10, 18, tzinfo=timezone.utc)
    first, second, third, fourth = sorted((_row(timestamp) for _ in range(4)), key=_key)
    events = []
    data = _Data((first, second, third, fourth), events)
    storage = _Storage(events, failures=(first.prefix,))

    result = await drain_pending(
        data,
        storage,
        bucket="media-cache",
        page_size=2,
        stop=asyncio.Event(),
        storage_stop=threading.Event(),
    )

    assert result == {"completed": 3, "failed": 1, "pages": 2}
    assert len(data.pending_cursors) == 3
    assert data.pending_cursors[0] is None
    assert data.pending_cursors[1] == DeletionCursor.after(second)
    assert data.pending_cursors[2] == DeletionCursor.after(fourth)
    assert {asset for _, asset in data.completed} == {
        second.asset_id,
        third.asset_id,
        fourth.asset_id,
    }
    assert events.index(("read_committed", None)) < events.index(
        ("delete", first.prefix)
    )


@pytest.mark.asyncio
async def test_failed_ack_is_left_pending_and_repeated_delete_recovers():
    row = _row(datetime(2026, 10, 18, tzinfo=timezone.utc))
    identity = row.operation_id, row.asset_id
    events = []
    data = _Data((row,), events, ack_failures=(identity,))
    storage = _Storage(events)
    args = {
        "data": data,
        "storage": storage,
        "bucket": "media-cache",
        "page_size": 10,
        "storage_stop": threading.Event(),
    }

    first = await drain_pending(stop=asyncio.Event(), **args)
    assert first == {"completed": 0, "failed": 1, "pages": 1}
    assert identity not in data.completed

    second = await drain_pending(stop=asyncio.Event(), **args)
    assert second == {"completed": 1, "failed": 0, "pages": 1}
    assert identity in data.completed
    assert storage.calls == [row.prefix, row.prefix]


@pytest.mark.asyncio
async def test_bucket_mismatch_never_reaches_object_storage():
    row = _row(datetime(2026, 10, 18, tzinfo=timezone.utc), bucket="other-bucket")
    events = []
    data = _Data((row,), events, ignore_bucket=True)
    storage = _Storage(events)

    result = await drain_pending(
        data,
        storage,
        bucket="media-cache",
        page_size=10,
        stop=asyncio.Event(),
        storage_stop=threading.Event(),
    )

    assert result == {"completed": 0, "failed": 1, "pages": 1}
    assert storage.calls == []
    assert not data.completed


@pytest.mark.asyncio
async def test_shutdown_during_prefix_delete_never_acknowledges_partial_work():
    row = _row(datetime(2026, 10, 18, tzinfo=timezone.utc))
    events = []
    data = _Data((row,), events)
    storage_stop = threading.Event()
    storage = _Storage(events, stop_after=row.prefix)
    loop_stop = asyncio.Event()
    result = await drain_pending(
        data,
        storage,
        bucket="media-cache",
        page_size=10,
        stop=loop_stop,
        storage_stop=storage_stop,
    )

    assert result == {"completed": 0, "failed": 0, "pages": 1}
    assert data.ack_calls == []


def _image_claim(prefix, *, garbage_id=None, lease_token=None):
    return ImageGarbageClaim(
        garbage_id=garbage_id or uuid4(),
        prefix=prefix,
        lease_token=lease_token or uuid4(),
    )


@pytest.mark.asyncio
async def test_image_garbage_deletes_only_claimed_attempt_prefixes():
    job_id, lease_id = uuid4(), uuid4()
    prefix = f"image-assets/{job_id}/g1/a{lease_id}/"
    claim = _image_claim(prefix)
    worker_id = uuid4()
    events = []
    data = _ImageData([(claim,)], events)
    keep_key = f"image-assets/{uuid4()}/keep"
    storage = _ImageStorage(
        events,
        objects={prefix + "thumbnail.webp", keep_key},
    )

    result = await drain_image_garbage(
        data,
        storage,
        worker_id=worker_id,
        batch_size=7,
        lease_seconds=300,
        stop=asyncio.Event(),
        storage_stop=threading.Event(),
    )

    assert result == {"completed": 1, "failed": 0, "batches": 1}
    assert data.claim_calls == [(worker_id, 300, 7), (worker_id, 300, 7)]
    assert storage.calls == [prefix]
    assert storage.objects == {keep_key}
    assert data.ack_calls == [(claim.garbage_id, claim.lease_token)]


@pytest.mark.asyncio
async def test_image_garbage_retries_late_put_after_lost_ack():
    job_id, first_lease, second_lease = uuid4(), uuid4(), uuid4()
    prefix = f"image-pyramids/{job_id}/g4/a{first_lease}/"
    first = _image_claim(prefix, garbage_id=uuid4(), lease_token=first_lease)
    second = _image_claim(prefix, garbage_id=first.garbage_id, lease_token=second_lease)
    events = []
    identity = first.garbage_id, first.lease_token
    data = _ImageData([(first,), (second,), ()], events, ack_failures=(identity,))
    storage = _ImageStorage(
        events,
        objects={prefix + "manifest.json"},
        late_put=(prefix + "late-tile.webp"),
    )

    result = await drain_image_garbage(
        data,
        storage,
        worker_id=uuid4(),
        batch_size=5,
        lease_seconds=3600,
        stop=asyncio.Event(),
        storage_stop=threading.Event(),
    )

    assert result == {"completed": 1, "failed": 1, "batches": 2}
    assert storage.calls == [prefix, prefix]
    assert storage.objects == set()
    assert data.ack_calls == [
        (first.garbage_id, first.lease_token),
        (second.garbage_id, second.lease_token),
    ]


@pytest.mark.asyncio
async def test_image_garbage_shutdown_during_delete_does_not_ack_lease():
    prefix = f"image-assets/{uuid4()}/g1/a{uuid4()}/"
    claim = _image_claim(prefix)
    events = []
    data = _ImageData([(claim,)], events)
    storage_stop = threading.Event()
    storage = _ImageStorage(events, stop_after=prefix)
    stop = asyncio.Event()

    result = await drain_image_garbage(
        data,
        storage,
        worker_id=uuid4(),
        batch_size=1,
        lease_seconds=300,
        stop=stop,
        storage_stop=storage_stop,
    )

    assert result == {"completed": 0, "failed": 0, "batches": 1}
    assert data.ack_calls == []
