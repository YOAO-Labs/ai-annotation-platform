"""Bounded page, failure, cancellation and acknowledgement edges of the lane."""

import asyncio
from datetime import datetime, timedelta, timezone
import threading
from uuid import UUID, uuid4

import pytest

from yoao_assets_images.contract import (
    ClaimedNativePyramidRetirement,
    NativePyramidRetirementAck,
    NativePyramidRetirementClaim,
    NativePyramidRetirementMember,
    NativePyramidRetirementPage,
    NativePyramidRetirementStore,
    NativePyramidRetirementTarget,
)
from yoao_assets_cleanup.data import DataContractError
from yoao_assets_cleanup.native_gc import (
    NativeStorageError,
    NativeTargetRefused,
    drain_native_pyramid_retirement,
)

STORE = NativePyramidRetirementStore(
    endpoint_url="http://s3:9000",
    bucket="media-cache",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _target(output_id) -> NativePyramidRetirementTarget:
    return NativePyramidRetirementTarget.model_validate(
        {
            "schema": 2,
            "purpose": "media-cache",
            "location_id": str(uuid4()),
            "config_revision": 1,
            "binding_generation": 2,
            "endpoint_url": STORE.endpoint_url,
            "bucket": STORE.bucket,
            "key_prefix": "",
            "prefix": f"image-pyramids/objects/{output_id}/",
            "storage_identity": "b" * 64,
        }
    )


def _claim(*, after: UUID | None = None, lease_seconds: int = 300):
    output_id = uuid4()
    return NativePyramidRetirementClaim(
        output_id=output_id,
        lease_token=uuid4(),
        lease_expires_at=_now() + timedelta(seconds=lease_seconds),
        storage_ref=_target(output_id),
        after_operation_id=after,
    )


def _members(count: int, *, start: int = 1000) -> list[NativePyramidRetirementMember]:
    members = [
        NativePyramidRetirementMember(
            operation_id=UUID(int=start + index),
            relative_key=f"tiles/0/0/{index}.webp",
            sha256="a" * 64,
            size_bytes=1,
            etag='"etag"',
            version_id="v1",
        )
        for index in range(count)
    ]
    return sorted(members, key=lambda member: member.operation_id.int)


def _page(
    claim,
    members,
    *,
    after: UUID | None = None,
    exhausted: bool = False,
    page_id: UUID | None = None,
    output_id: UUID | None = None,
    lease_token: UUID | None = None,
    lease_seconds: int = 300,
) -> NativePyramidRetirementPage:
    if members and after is None:
        after = members[-1].operation_id
    return NativePyramidRetirementPage(
        output_id=claim.output_id if output_id is None else output_id,
        lease_token=claim.lease_token if lease_token is None else lease_token,
        page_id=page_id or uuid4(),
        lease_expires_at=_now() + timedelta(seconds=lease_seconds),
        after_operation_id=after,
        items=list(members),
        exhausted=exhausted,
    )


class _Data:
    def __init__(self, claims, pages, acks):
        self._claims = list(claims)
        self._pages = list(pages)
        self._acks = list(acks)
        self.claims = []
        self.prepares = []
        self.completes = []

    async def claim_native_pyramid_retirement(self, request):
        self.claims.append(request)
        if not self._claims:
            return ClaimedNativePyramidRetirement(claim=None)
        return ClaimedNativePyramidRetirement(claim=self._claims.pop(0))

    async def prepare_native_pyramid_retirement_page(self, request):
        self.prepares.append(request)
        item = self._pages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def complete_native_pyramid_retirement_page(self, request):
        self.completes.append(request)
        item = self._acks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _Storage:
    def __init__(
        self,
        *,
        failures=None,
        verify_error=None,
        prefix_error=None,
    ):
        self.failures = dict(failures or {})
        self.verify_error = verify_error
        self.prefix_error = prefix_error
        self.verified = []
        self.deleted = []
        self.prefixes = []

    def verify_target(self, output_id, target):
        self.verified.append((output_id, target))
        if self.verify_error is not None:
            raise self.verify_error

    def delete_member(self, prefix, member, stop):
        error = self.failures.pop(member.operation_id, None)
        if error is not None:
            raise error
        self.deleted.append((prefix, member.operation_id))

    def verify_prefix_absent(self, prefix, stop):
        self.prefixes.append(prefix)
        if self.prefix_error is not None:
            raise self.prefix_error

    def close(self):
        return None


async def _drain(data, storage, *, page_size=10, stop=None, storage_stop=None):
    return await drain_native_pyramid_retirement(
        data,
        storage,
        worker_id=uuid4(),
        store=STORE,
        page_size=page_size,
        stop=asyncio.Event() if stop is None else stop,
        storage_stop=(
            threading.Event() if storage_stop is None else storage_stop
        ),
    )


@pytest.mark.asyncio
async def test_member_pages_and_terminal_prefix_close_in_cursor_order():
    claim = _claim()
    prefix = claim.storage_ref.prefix
    members = _members(2)
    after = members[-1].operation_id
    page = _page(claim, members)
    terminal = _page(claim, [], after=after, exhausted=True)
    data = _Data(
        [claim],
        [page, terminal],
        [
            NativePyramidRetirementAck(claim.output_id, after, False),
            NativePyramidRetirementAck(claim.output_id, after, True),
        ],
    )
    storage = _Storage()

    result = await _drain(data, storage)

    assert result == {"completed": 1, "failed": 0, "pages": 2}
    assert [request.outcome for request in data.completes] == [
        "members_absent",
        "prefix_absent",
    ]
    assert data.completes[0].operation_ids == [
        member.operation_id for member in members
    ]
    assert data.completes[1].operation_ids == []
    assert storage.deleted == [
        (prefix, members[0].operation_id),
        (prefix, members[1].operation_id),
    ]
    assert storage.prefixes == [prefix]
    assert len(data.claims) == 2
    assert data.claims[0].store == STORE


@pytest.mark.asyncio
async def test_ambiguous_page_prepare_is_never_acknowledged():
    claim = _claim()
    data = _Data(
        [claim],
        [OSError("prepare response lost"), OSError("replay unavailable")],
        [],
    )
    storage = _Storage()

    result = await _drain(data, storage)

    assert result == {"completed": 0, "failed": 1, "pages": 0}
    assert storage.deleted == []
    assert data.completes == []
    assert len(data.claims) == 1
    assert len(data.prepares) == 2


@pytest.mark.asyncio
async def test_lost_prepare_reply_replays_the_same_page_and_finishes():
    claim = _claim()
    members = _members(1)
    after = members[-1].operation_id
    page = _page(claim, members)
    terminal = _page(claim, [], after=after, exhausted=True)
    data = _Data(
        [claim],
        [OSError("prepare response lost"), page, terminal],
        [
            NativePyramidRetirementAck(claim.output_id, after, False),
            NativePyramidRetirementAck(claim.output_id, after, True),
        ],
    )
    storage = _Storage()

    result = await _drain(data, storage)

    assert result == {"completed": 1, "failed": 0, "pages": 2}
    assert storage.deleted == [(claim.storage_ref.prefix, members[0].operation_id)]
    assert [request.outcome for request in data.completes] == [
        "members_absent",
        "prefix_absent",
    ]
    assert len(data.prepares) == 3


@pytest.mark.asyncio
async def test_definite_member_failure_releases_only_its_closed_page():
    claim = _claim()
    members = _members(3)
    page = _page(claim, members)
    data = _Data(
        [claim],
        [page],
        [NativePyramidRetirementAck(claim.output_id, None, False)],
    )
    failure = NativeStorageError("native_member_identity_mismatch")
    storage = _Storage(failures={members[1].operation_id: failure})

    result = await _drain(data, storage)

    assert result == {"completed": 0, "failed": 1, "pages": 1}
    assert storage.deleted == [(claim.storage_ref.prefix, members[0].operation_id)]
    assert [request.outcome for request in data.completes] == [
        "closed_without_ack"
    ]
    assert data.completes[0].operation_ids == []
    assert data.completes[0].page_id == page.page_id
    assert storage.prefixes == []


@pytest.mark.asyncio
async def test_unknown_delete_outcome_never_acknowledges_the_page():
    claim = _claim()
    members = _members(1)
    data = _Data([claim], [_page(claim, members)], [])
    failure = NativeStorageError(
        "native_member_delete_unsettled", unsettled=True
    )
    storage = _Storage(failures={members[0].operation_id: failure})

    result = await _drain(data, storage)

    assert result == {"completed": 0, "failed": 1, "pages": 1}
    assert data.completes == []
    assert storage.deleted == []


@pytest.mark.asyncio
async def test_cancellation_drains_inflight_io_before_leaving_the_page():
    claim = _claim()
    members = _members(1)
    data = _Data([claim], [_page(claim, members)], [])

    class BlockingStorage(_Storage):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def delete_member(self, prefix, member, stop):
            self.started.set()
            self.release.wait(5)
            self.finished.set()

    storage = BlockingStorage()
    task = asyncio.create_task(_drain(data, storage))
    for _ in range(200):
        if storage.started.is_set():
            break
        await asyncio.sleep(0.01)
    assert storage.started.is_set()

    task.cancel()
    await asyncio.sleep(0)
    storage.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert storage.finished.is_set() is True
    assert data.completes == []


@pytest.mark.asyncio
async def test_ambiguous_page_ack_is_not_retried_and_never_replays_deletes():
    claim = _claim()
    members = _members(1)
    data = _Data(
        [claim],
        [_page(claim, members)],
        [
            OSError("ack lost"),
            NativePyramidRetirementAck(claim.output_id, None, False),
        ],
    )
    storage = _Storage()

    result = await _drain(data, storage)

    assert result == {"completed": 0, "failed": 1, "pages": 1}
    assert len(storage.deleted) == 1
    assert [request.outcome for request in data.completes] == [
        "members_absent",
        "closed_without_ack",
    ]
    assert len(data.claims) == 1


@pytest.mark.asyncio
async def test_stale_member_page_ack_does_not_advance_or_repeat_work():
    claim = _claim()
    members = _members(1)
    data = _Data(
        [claim],
        [_page(claim, members)],
        [NativePyramidRetirementAck(claim.output_id, uuid4(), False)],
    )
    storage = _Storage()

    with pytest.raises(DataContractError):
        await _drain(data, storage)

    assert len(storage.deleted) == 1
    assert len(data.completes) == 1
    assert storage.prefixes == []
    assert len(data.claims) == 1


@pytest.mark.asyncio
async def test_foreign_saved_target_is_refused_before_prepare_or_launch():
    claim = _claim()
    data = _Data([claim], [], [])
    storage = _Storage(
        verify_error=NativeTargetRefused("native_target_endpoint_mismatch")
    )

    with pytest.raises(NativeTargetRefused):
        await _drain(data, storage)

    assert data.prepares == []
    assert data.completes == []
    assert storage.deleted == []


@pytest.mark.asyncio
async def test_page_substitution_is_refused_before_member_io():
    claim = _claim()
    other = _claim()
    members = _members(1)
    data = _Data(
        [claim],
        [_page(claim, members, output_id=other.output_id)],
        [],
    )
    storage = _Storage()

    with pytest.raises(DataContractError):
        await _drain(data, storage)

    assert storage.deleted == []
    assert data.completes == []


@pytest.mark.asyncio
async def test_non_advancing_page_is_refused_before_member_io():
    claim = _claim(after=UUID(int=2000))
    members = _members(1, start=1000)
    data = _Data(
        [claim],
        [_page(claim, members, after=UUID(int=1500))],
        [],
    )
    storage = _Storage()

    with pytest.raises(DataContractError):
        await _drain(data, storage)

    assert storage.deleted == []
    assert data.completes == []


@pytest.mark.asyncio
async def test_rewound_terminal_page_is_refused_before_prefix_check():
    claim = _claim(after=UUID(int=2000))
    terminal = _page(claim, [], after=UUID(int=1500), exhausted=True)
    data = _Data([claim], [terminal], [])
    storage = _Storage()

    with pytest.raises(DataContractError):
        await _drain(data, storage)

    assert storage.prefixes == []
    assert data.completes == []


@pytest.mark.asyncio
async def test_expired_page_lease_stops_before_new_member_work():
    claim = _claim()
    members = _members(1)
    page = _page(claim, members, lease_seconds=-1)
    data = _Data(
        [claim],
        [page],
        [NativePyramidRetirementAck(claim.output_id, None, False)],
    )
    storage = _Storage()

    result = await _drain(data, storage)

    assert result == {"completed": 0, "failed": 0, "pages": 1}
    assert storage.deleted == []
    assert [request.outcome for request in data.completes] == [
        "closed_without_ack"
    ]


@pytest.mark.asyncio
async def test_shutdown_after_prepare_releases_the_page_without_absence():
    claim = _claim()
    members = _members(1)
    stop = asyncio.Event()

    class StoppingData(_Data):
        async def prepare_native_pyramid_retirement_page(self, request):
            page = await super().prepare_native_pyramid_retirement_page(
                request
            )
            stop.set()
            return page

    data = StoppingData(
        [claim],
        [_page(claim, members)],
        [NativePyramidRetirementAck(claim.output_id, None, False)],
    )
    storage = _Storage()

    result = await _drain(data, storage, stop=stop)

    assert result == {"completed": 0, "failed": 0, "pages": 1}
    assert storage.deleted == []
    assert [request.outcome for request in data.completes] == [
        "closed_without_ack"
    ]


@pytest.mark.asyncio
async def test_terminal_prefix_material_refuses_without_claiming_absence():
    claim = _claim()
    terminal = _page(claim, [], exhausted=True)
    data = _Data(
        [claim],
        [terminal],
        [NativePyramidRetirementAck(claim.output_id, None, False)],
    )
    storage = _Storage(
        prefix_error=NativeStorageError("native_prefix_not_empty")
    )

    result = await _drain(data, storage)

    assert result == {"completed": 0, "failed": 1, "pages": 1}
    assert [request.outcome for request in data.completes] == [
        "closed_without_ack"
    ]
    assert storage.prefixes == [claim.storage_ref.prefix]


@pytest.mark.asyncio
async def test_lost_terminal_ack_releases_the_page_without_rechecking_absence():
    claim = _claim()
    terminal = _page(claim, [], exhausted=True)
    data = _Data(
        [claim],
        [terminal],
        [
            OSError("terminal ack lost"),
            NativePyramidRetirementAck(claim.output_id, None, False),
        ],
    )
    storage = _Storage()

    result = await _drain(data, storage)

    assert result == {"completed": 0, "failed": 1, "pages": 1}
    assert [request.outcome for request in data.completes] == [
        "prefix_absent",
        "closed_without_ack",
    ]
    assert storage.prefixes == [claim.storage_ref.prefix]
    assert len(data.claims) == 1


@pytest.mark.asyncio
async def test_closure_ack_failure_is_not_reposted():
    claim = _claim()
    members = _members(1)
    data = _Data(
        [claim],
        [_page(claim, members)],
        [OSError("closure ack lost")],
    )
    failure = NativeStorageError("native_member_identity_mismatch")
    storage = _Storage(failures={members[0].operation_id: failure})

    result = await _drain(data, storage)

    assert result == {"completed": 0, "failed": 1, "pages": 1}
    assert [request.outcome for request in data.completes] == [
        "closed_without_ack"
    ]


@pytest.mark.asyncio
async def test_stale_closure_ack_is_not_reposted_or_trusted():
    claim = _claim()
    members = _members(1)
    data = _Data(
        [claim],
        [_page(claim, members)],
        [NativePyramidRetirementAck(claim.output_id, uuid4(), False)],
    )
    failure = NativeStorageError("native_member_identity_mismatch")
    storage = _Storage(failures={members[0].operation_id: failure})

    result = await _drain(data, storage)

    assert result == {"completed": 0, "failed": 1, "pages": 1}
    assert [request.outcome for request in data.completes] == [
        "closed_without_ack"
    ]
    assert len(data.claims) == 1


@pytest.mark.asyncio
async def test_no_claim_does_not_prepare_or_touch_storage():
    data = _Data([], [], [])
    storage = _Storage()

    result = await _drain(data, storage)

    assert result == {"completed": 0, "failed": 0, "pages": 0}
    assert data.prepares == []
    assert storage.verified == []
