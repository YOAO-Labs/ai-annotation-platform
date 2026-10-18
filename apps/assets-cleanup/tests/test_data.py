from datetime import datetime, timezone
from uuid import uuid4

import pytest

from aap_assets_cleanup.contract import AssetStorageDeletion, DeletionCursor
from aap_assets_cleanup.data import (
    CLAIM_IMAGE_GARBAGE_SQL,
    COMPLETE_SQL,
    COMPLETE_IMAGE_GARBAGE_SQL,
    DataContractError,
    StorageDeletionData,
    _PREFLIGHT_SQL,
)


class _Transaction:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        self.events.append("begin")

    async def __aexit__(self, exc_type, exc, traceback):
        self.events.append("rollback" if exc_type else "commit")


class _Connection:
    def __init__(
        self,
        events,
        rows=(),
        acknowledgement=True,
        scoped=True,
        image_claim=None,
        image_completion=None,
    ):
        self.events = events
        self.rows = list(rows)
        self.acknowledgement = acknowledgement
        self.scoped = scoped
        self.image_claim = image_claim
        self.image_completion = image_completion

    def transaction(self):
        return _Transaction(self.events)

    async def fetch(self, query, *parameters):
        self.events.append(("fetch", query, parameters))
        return self.rows

    async def fetchval(self, query, *parameters):
        self.events.append(("fetchval", query, parameters))
        if query == COMPLETE_SQL:
            return self.acknowledgement
        if query == CLAIM_IMAGE_GARBAGE_SQL:
            return self.image_claim
        if query == COMPLETE_IMAGE_GARBAGE_SQL:
            return self.image_completion
        assert "has_table_privilege" in query
        return self.scoped


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


def _row(*, created_at=None):
    asset_id = uuid4()
    return {
        "operation_id": uuid4(),
        "asset_id": asset_id,
        "bucket": "media-cache",
        "prefix": f"image-pyramids/{asset_id}/",
        "created_at": created_at or datetime(2026, 10, 18, tzinfo=timezone.utc),
    }


@pytest.mark.asyncio
async def test_pending_read_uses_v2_cursor_contract_and_commits_before_returning():
    events = []
    row = _row()
    connection = _Connection(events, rows=[row])
    data = StorageDeletionData(_Pool(connection))
    cursor = DeletionCursor(
        created_at=datetime(2026, 10, 17, tzinfo=timezone.utc),
        operation_id=uuid4(),
        asset_id=uuid4(),
    )

    result = await data.pending(bucket="media-cache", cursor=cursor, limit=23)

    assert [event if isinstance(event, str) else event[0] for event in events] == [
        "begin",
        "fetch",
        "commit",
    ]
    query, parameters = events[1][1:]
    assert "pending_storage_deletions_v2" in query
    assert "public.asset_storage_deletions" not in query
    assert parameters == (
        "media-cache",
        cursor.created_at,
        cursor.operation_id,
        cursor.asset_id,
        23,
    )
    assert result[0].asset_id == row["asset_id"]


@pytest.mark.asyncio
async def test_cleanup_login_preflight_fails_closed_when_role_is_broad():
    events = []
    data = StorageDeletionData(_Pool(_Connection(events, scoped=False)))

    with pytest.raises(DataContractError, match="not limited"):
        await data.verify_role_scope()


def test_role_preflight_rejects_direct_column_and_other_module_access():
    assert "pg_auth_members" in _PREFLIGHT_SQL
    assert "has_table_privilege" in _PREFLIGHT_SQL
    assert "has_any_column_privilege" in _PREFLIGHT_SQL
    assert "has_sequence_privilege" in _PREFLIGHT_SQL
    assert "has_function_privilege" in _PREFLIGHT_SQL
    assert "pending_storage_deletions_v2" in _PREFLIGHT_SQL
    assert "complete_storage_deletion" in _PREFLIGHT_SQL
    assert "claim_image_garbage" in _PREFLIGHT_SQL
    assert "complete_image_garbage" in _PREFLIGHT_SQL


@pytest.mark.asyncio
async def test_ack_runs_in_its_own_transaction_and_requires_matching_row():
    events = []
    reference = _row()
    reference.pop("created_at")
    data = StorageDeletionData(_Pool(_Connection(events, acknowledgement=True)))
    await data.complete(AssetStorageDeletion.model_validate(reference))

    assert [event if isinstance(event, str) else event[0] for event in events] == [
        "begin",
        "fetchval",
        "commit",
    ]
    assert events[1][1] == COMPLETE_SQL

    missing = StorageDeletionData(_Pool(_Connection([], acknowledgement=False)))
    with pytest.raises(DataContractError, match="did not acknowledge"):
        await missing.complete(AssetStorageDeletion.model_validate(reference))


@pytest.mark.asyncio
async def test_image_garbage_claim_uses_only_bounded_json_owner_call():
    from uuid import uuid4

    from aap_assets_cleanup.contract import ImageGarbageClaim

    garbage_id, lease_token, worker_id, asset_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    prefix = f"image-pyramids/{asset_id}/g2/a{lease_token}/"
    payload = {
        "schema": "aap-assets-images/v1",
        "ok": True,
        "data": {
            "items": [
                {
                    "garbage_id": str(garbage_id),
                    "prefix": prefix,
                    "lease_token": str(lease_token),
                }
            ]
        },
    }
    events = []
    data = StorageDeletionData(_Pool(_Connection(events, image_claim=payload)))

    claimed = await data.claim_image_garbage(
        worker_id=worker_id, lease_seconds=90, batch_size=17
    )

    assert claimed == (
        ImageGarbageClaim(
            garbage_id=garbage_id,
            prefix=prefix,
            lease_token=lease_token,
        ),
    )
    assert [event if isinstance(event, str) else event[0] for event in events] == [
        "begin",
        "fetchval",
        "commit",
    ]
    query, parameters = events[1][1:]
    assert query == CLAIM_IMAGE_GARBAGE_SQL
    assert parameters == (worker_id, 90, 17)
    assert "public.asset_image_garbage" not in query


@pytest.mark.asyncio
async def test_image_garbage_completion_is_bound_to_claim_and_requires_owner_ack():
    from uuid import uuid4

    from aap_assets_cleanup.contract import ImageGarbageClaim

    claim = ImageGarbageClaim(
        garbage_id=uuid4(),
        prefix=f"image-assets/{uuid4()}/g3/a{uuid4()}/",
        lease_token=uuid4(),
    )
    events = []
    payload = {
        "schema": "aap-assets-images/v1",
        "ok": True,
        "data": {"garbage_id": str(claim.garbage_id), "completed": True},
    }
    data = StorageDeletionData(_Pool(_Connection(events, image_completion=payload)))
    await data.complete_image_garbage(claim)

    assert [event if isinstance(event, str) else event[0] for event in events] == [
        "begin",
        "fetchval",
        "commit",
    ]
    assert events[1][1:] == (
        COMPLETE_IMAGE_GARBAGE_SQL,
        (claim.garbage_id, claim.lease_token),
    )

    lost_lease = StorageDeletionData(
        _Pool(
            _Connection(
                [],
                image_completion={
                    "schema": "aap-assets-images/v1",
                    "ok": True,
                    "data": {"garbage_id": str(claim.garbage_id), "completed": False},
                },
            )
        )
    )
    with pytest.raises(DataContractError, match="matching image cleanup lease"):
        await lost_lease.complete_image_garbage(claim)


@pytest.mark.asyncio
async def test_image_garbage_rejects_wrong_schema_or_extra_response_fields():
    from uuid import uuid4

    data = StorageDeletionData(
        _Pool(
            _Connection(
                [],
                image_claim={
                    "schema": "aap-assets-cleanup/v1",
                    "ok": True,
                    "data": {"items": []},
                },
            )
        )
    )
    with pytest.raises(DataContractError, match="invalid image cleanup reply"):
        await data.claim_image_garbage(
            worker_id=uuid4(), lease_seconds=10, batch_size=1
        )
