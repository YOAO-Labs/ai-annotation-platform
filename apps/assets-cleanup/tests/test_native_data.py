"""Result-boundary refusals for the three Native cleanup capabilities."""

import json
from uuid import UUID, uuid4

import pytest

from yoao_assets_images.contract import (
    ClaimNativePyramidRetirementRequest,
    CompleteNativePyramidRetirementPageRequest,
    PrepareNativePyramidRetirementPageRequest,
)
from yoao_assets_cleanup.data import (
    CLAIM_NATIVE_RETIREMENT_SQL,
    COMPLETE_NATIVE_RETIREMENT_PAGE_SQL,
    PREPARE_NATIVE_RETIREMENT_PAGE_SQL,
    DataContractError,
    StorageDeletionData,
)

LEASE_EXPIRES = "2026-11-16T00:00:00Z"


class _Transaction:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        self.events.append("begin")

    async def __aexit__(self, exc_type, exc, traceback):
        self.events.append("rollback" if exc_type else "commit")


class _Connection:
    def __init__(self, payloads, events=None):
        self.payloads = dict(payloads)
        self.events = events if events is not None else []
        self.calls = []

    def transaction(self):
        return _Transaction(self.events)

    async def fetchval(self, query, *parameters):
        self.calls.append((query, parameters))
        return self.payloads[query]


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


def _target(output_id, *, endpoint=None, bucket=None, prefix=None):
    return {
        "schema": 2,
        "purpose": "media-cache",
        "location_id": str(uuid4()),
        "config_revision": 1,
        "binding_generation": 3,
        "endpoint_url": "http://s3:9000" if endpoint is None else endpoint,
        "bucket": "media-cache" if bucket is None else bucket,
        "key_prefix": "",
        "prefix": (
            prefix if prefix is not None else f"image-pyramids/objects/{output_id}/"
        ),
        "storage_identity": "c" * 64,
    }


def _claim_payload(claim):
    return {
        "schema": "aap-assets-images/v1",
        "ok": True,
        "data": {"claim": claim},
    }


def _member(operation_id, *, relative_key="manifest.json"):
    return {
        "operation_id": str(operation_id),
        "relative_key": relative_key,
        "sha256": "d" * 64,
        "size_bytes": 12,
        "etag": '"etag"',
        "version_id": "v1",
    }


def _page_payload(
    *,
    output_id,
    lease_token,
    page_id,
    after=None,
    items=(),
    exhausted=False,
):
    if items and after is None:
        after = items[-1]["operation_id"]
    return {
        "schema": "aap-assets-images/v1",
        "ok": True,
        "data": {
            "output_id": str(output_id),
            "lease_token": str(lease_token),
            "page_id": str(page_id),
            "lease_expires_at": LEASE_EXPIRES,
            "after_operation_id": None if after is None else str(after),
            "items": list(items),
            "exhausted": exhausted,
        },
    }


def _ack_payload(*, output_id, after=None, completed=False):
    return {
        "schema": "aap-assets-images/v1",
        "ok": True,
        "data": {
            "output_id": str(output_id),
            "after_operation_id": None if after is None else str(after),
            "completed": completed,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        lambda output_id: _target(output_id, prefix="image-pyramids/other/"),
    ],
)
async def test_claim_rejects_a_target_outside_its_saved_scope(target):
    output_id = uuid4()
    claim = {
        "output_id": str(output_id),
        "lease_token": str(uuid4()),
        "lease_expires_at": LEASE_EXPIRES,
        "storage_ref": target(output_id),
        "after_operation_id": None,
    }
    connection = _Connection({CLAIM_NATIVE_RETIREMENT_SQL: _claim_payload(claim)})
    request = ClaimNativePyramidRetirementRequest(
        worker_id=uuid4(),
    )

    with pytest.raises(DataContractError) as failure:
        await StorageDeletionData(_Pool(connection)).claim_native_pyramid_retirement(
            request
        )

    assert failure.value.code == "invalid_response"


@pytest.mark.asyncio
async def test_claim_error_envelope_keeps_its_code_and_does_not_mutate():
    connection = _Connection(
        {
            CLAIM_NATIVE_RETIREMENT_SQL: {
                "schema": "aap-assets-images/v1",
                "ok": False,
                "error": {"code": "lease_lost"},
            }
        }
    )
    request = ClaimNativePyramidRetirementRequest(
        worker_id=uuid4(),
    )

    with pytest.raises(DataContractError) as failure:
        await StorageDeletionData(_Pool(connection)).claim_native_pyramid_retirement(
            request
        )

    assert failure.value.code == "lease_lost"


@pytest.mark.asyncio
async def test_prepare_requires_the_requested_page_scope_and_size():
    output_id, lease_token, page_id = uuid4(), uuid4(), uuid4()
    operation_id = uuid4()
    connection = _Connection(
        {
            PREPARE_NATIVE_RETIREMENT_PAGE_SQL: _page_payload(
                output_id=output_id,
                lease_token=lease_token,
                page_id=page_id,
                items=[_member(operation_id)],
            )
        }
    )
    request = PrepareNativePyramidRetirementPageRequest(
        output_id=output_id,
        lease_token=lease_token,
        page_size=5,
    )

    page = await StorageDeletionData(
        _Pool(connection)
    ).prepare_native_pyramid_retirement_page(request)

    assert page.page_id == page_id
    assert page.items[0].operation_id == operation_id
    assert connection.calls[0] == (
        PREPARE_NATIVE_RETIREMENT_PAGE_SQL,
        (output_id, lease_token, 5),
    )


@pytest.mark.asyncio
async def test_prepare_rejects_a_page_above_the_requested_size_or_lease():
    output_id, lease_token, page_id = uuid4(), uuid4(), uuid4()
    items = [
        _member(UUID(int=1000), relative_key="manifest.json"),
        _member(UUID(int=1001), relative_key="overview.webp"),
    ]
    connection = _Connection(
        {
            PREPARE_NATIVE_RETIREMENT_PAGE_SQL: _page_payload(
                output_id=output_id,
                lease_token=lease_token,
                page_id=page_id,
                items=items,
            )
        }
    )
    request = PrepareNativePyramidRetirementPageRequest(
        output_id=output_id,
        lease_token=lease_token,
        page_size=1,
    )

    with pytest.raises(DataContractError) as failure:
        await StorageDeletionData(
            _Pool(connection)
        ).prepare_native_pyramid_retirement_page(request)

    assert failure.value.code == "invalid_response"

    wrong_lease = _Connection(
        {
            PREPARE_NATIVE_RETIREMENT_PAGE_SQL: _page_payload(
                output_id=output_id,
                lease_token=uuid4(),
                page_id=page_id,
                items=[_member(UUID(int=1000))],
            )
        }
    )
    with pytest.raises(DataContractError):
        await StorageDeletionData(
            _Pool(wrong_lease)
        ).prepare_native_pyramid_retirement_page(request)


@pytest.mark.asyncio
async def test_members_absent_completion_sends_exact_ids_in_order():
    output_id, lease_token, page_id = uuid4(), uuid4(), uuid4()
    first, second = sorted((uuid4(), uuid4()), key=lambda value: value.int)
    connection = _Connection(
        {
            COMPLETE_NATIVE_RETIREMENT_PAGE_SQL: _ack_payload(
                output_id=output_id,
                after=second,
                completed=False,
            )
        }
    )
    request = CompleteNativePyramidRetirementPageRequest(
        output_id=output_id,
        lease_token=lease_token,
        page_id=page_id,
        outcome="members_absent",
        operation_ids=[first, second],
    )

    ack = await StorageDeletionData(
        _Pool(connection)
    ).complete_native_pyramid_retirement_page(request)

    assert ack.after_operation_id == second
    query, parameters = connection.calls[0]
    assert query == COMPLETE_NATIVE_RETIREMENT_PAGE_SQL
    assert parameters[:4] == (
        output_id,
        lease_token,
        page_id,
        "members_absent",
    )
    assert json.loads(parameters[4]) == [str(first), str(second)]


@pytest.mark.asyncio
async def test_prefix_absent_completion_requires_an_empty_terminal_ack():
    output_id, lease_token, page_id = uuid4(), uuid4(), uuid4()
    connection = _Connection(
        {
            COMPLETE_NATIVE_RETIREMENT_PAGE_SQL: _ack_payload(
                output_id=output_id,
                completed=True,
            )
        }
    )
    request = CompleteNativePyramidRetirementPageRequest(
        output_id=output_id,
        lease_token=lease_token,
        page_id=page_id,
        outcome="prefix_absent",
        operation_ids=[],
    )

    ack = await StorageDeletionData(
        _Pool(connection)
    ).complete_native_pyramid_retirement_page(request)

    assert ack.completed is True
    assert json.loads(connection.calls[0][1][4]) == []


@pytest.mark.asyncio
async def test_closed_without_ack_keeps_the_cursor_and_settles_no_members():
    output_id, lease_token, page_id = uuid4(), uuid4(), uuid4()
    previous = uuid4()
    connection = _Connection(
        {
            COMPLETE_NATIVE_RETIREMENT_PAGE_SQL: _ack_payload(
                output_id=output_id,
                after=previous,
                completed=False,
            )
        }
    )
    request = CompleteNativePyramidRetirementPageRequest(
        output_id=output_id,
        lease_token=lease_token,
        page_id=page_id,
        outcome="closed_without_ack",
        operation_ids=[],
    )

    ack = await StorageDeletionData(
        _Pool(connection)
    ).complete_native_pyramid_retirement_page(request)

    assert ack.after_operation_id == previous
    assert ack.completed is False
    assert connection.calls[0][1][3] == "closed_without_ack"
    assert json.loads(connection.calls[0][1][4]) == []


@pytest.mark.asyncio
async def test_stale_or_foreign_completion_ack_is_rejected():
    output_id, lease_token, page_id = uuid4(), uuid4(), uuid4()
    operation_id = uuid4()
    request = CompleteNativePyramidRetirementPageRequest(
        output_id=output_id,
        lease_token=lease_token,
        page_id=page_id,
        outcome="members_absent",
        operation_ids=[operation_id],
    )
    foreign = _Connection(
        {
            COMPLETE_NATIVE_RETIREMENT_PAGE_SQL: _ack_payload(
                output_id=uuid4(),
                after=operation_id,
            )
        }
    )
    with pytest.raises(DataContractError):
        await StorageDeletionData(
            _Pool(foreign)
        ).complete_native_pyramid_retirement_page(request)

    not_advanced = _Connection(
        {
            COMPLETE_NATIVE_RETIREMENT_PAGE_SQL: _ack_payload(
                output_id=output_id,
                after=None,
            )
        }
    )
    with pytest.raises(DataContractError):
        await StorageDeletionData(
            _Pool(not_advanced)
        ).complete_native_pyramid_retirement_page(request)
