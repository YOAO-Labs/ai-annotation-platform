"""Pyramid delegation boundaries and actual read/native/close lifecycles."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from billiard.exceptions import SoftTimeLimitExceeded
from fastapi import HTTPException, Request
import pytest

from app.services import image_pyramid_originals as service
from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services.original_sources import OriginalSource, TaskRelation
from tests.test_managed_original_reader import ENDPOINT, RAW, Provider


def original(*, task=True, standalone=False):
    task_id, project_id, item_id, content_id = (uuid.uuid4() for _ in range(4))
    return OriginalSource(
        owner_kind="task_original" if standalone else "dataset_asset",
        content_id=content_id,
        sha256=hashlib.sha256(RAW).hexdigest(),
        size_bytes=len(RAW),
        media_type="image",
        item_id=None if standalone else item_id,
        relation=TaskRelation(
            task_id,
            project_id,
            None,
            "saved/图片 空格%25",
            "image",
            content_id if standalone else None,
            None if standalone else item_id,
            None,
            None,
            None if standalone else item_id,
            (),
        )
        if task
        else None,
        bucket="saved-historical-pyramid-originals",
        object_key="saved/图片 空格%25",
        endpoint_url=ENDPOINT,
        location_id=uuid.uuid4(),
        location_revision=1,
        maintenance_revision=9,
        ownership_facts=("saved-owner",),
        item_facts=None,
        replica_facts=None,
        location_facts=None,
    )


@asynccontextmanager
async def reads(_db, _lock):
    yield


@pytest.fixture
def metadata(monkeypatch):
    state = SimpleNamespace(source=original(), actor=SimpleNamespace(id=uuid.uuid4()))
    state.key_id = uuid.uuid4()
    state.key = (
        "private-password-hash",
        ("annotations:read",),
        datetime.now(timezone.utc) + timedelta(hours=1),
    )
    state.proof = SimpleNamespace(
        actor_id=state.actor.id,
        key_id=state.key_id,
        key_proof=state.key,
    )
    state.captured = AsyncMock(return_value=state.proof)

    async def actor_and_key(_db, actor_id, key_id, scope, proof=None):
        assert actor_id == state.actor.id and key_id == state.key_id
        assert scope == "annotations:read"
        if "annotations:read" not in state.key[1] and "*" not in state.key[1]:
            raise HTTPException(403, {"code": "api_key_authority_revoked"})
        return state.actor, state.key

    async def pin(_db, kind, identifier, *, lock=False):
        assert kind == "task" and identifier == state.source.relation.task_id
        return state.source

    state.actor_and_key = AsyncMock(side_effect=actor_and_key)
    state.visible = AsyncMock()
    state.pin = AsyncMock(side_effect=pin)
    monkeypatch.setattr(service.access, "capture_credentials", state.captured)
    monkeypatch.setattr(service.access, "actor_and_key", state.actor_and_key)
    monkeypatch.setattr(service.access, "visible_task", state.visible)
    monkeypatch.setattr(service.access, "check_clock", lambda *_: None)
    monkeypatch.setattr(service, "pin", state.pin)
    monkeypatch.setattr(service.original_sources, "_reads", reads)
    return state


async def authorized(state):
    return await service.authorize(
        object(),
        state.source.relation.task_id,
        state.actor,
        Request({"type": "http", "headers": []}),
    )


@pytest.mark.parametrize("standalone", [False, True])
def test_receipt_is_safe_exact_identity_for_both_canonical_owners(standalone):
    source = original(standalone=standalone)
    value = service.receipt(source)
    assert service.validate_receipt(value) == value
    encoded = json.dumps(value)
    for private in (source.bucket, source.object_key, source.endpoint_url):
        assert private not in encoded
    assert value["owner_kind"] == source.owner_kind
    assert value["sha256"] == source.sha256
    assert value["location_revision"] == 1
    assert service.receipt(replace(source, relation=None)) == value
    with pytest.raises(FrozenInstanceError):
        source.sha256 = "a" * 64


@pytest.mark.parametrize(
    "changes",
    [
        {"bucket": "different-saved-bucket"},
        {"object_key": "changed/key"},
        {"endpoint_url": "http://changed.test:9000"},
        {"ownership_facts": ("replaced-owner",)},
        {"location_revision": 2},
        {"maintenance_revision": 10},
        {"content_id": uuid.uuid4()},
    ],
)
def test_receipt_detects_same_content_locator_or_owner_rebinding(changes):
    source = original()
    assert service.receipt(replace(source, **changes)) != service.receipt(source)


@pytest.mark.parametrize(
    "name,value",
    [
        ("schema", True),
        ("schema", 2),
        ("content_id", "a" * 32),
        ("owner_kind", []),
        ("sha256", "A" * 64),
        ("binding", None),
        ("size_bytes", True),
        ("size_bytes", 0),
        ("location_revision", True),
        ("maintenance_revision", 0),
        ("location_id", "invalid"),
    ],
)
def test_malformed_receipt_fails_closed(name, value):
    with pytest.raises(ContentError, match="original_input_unrecorded"):
        service.validate_receipt(service.receipt(original()) | {name: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"media_type": "video"},
        {"size_bytes": 0},
        {"size_bytes": True},
        {"sha256": "invalid"},
        {"location_revision": None},
        {"location_id": None},
        {"bucket": None},
        {"endpoint_url": None},
    ],
)
def test_unsupported_original_never_becomes_a_receipt(changes):
    with pytest.raises(ContentError, match="original_input_unsupported"):
        service.receipt(replace(original(), **changes))


async def test_http_proof_scope_visibility_and_exact_original_are_captured(metadata):
    authority = await authorized(metadata)
    assert authority.task_id == metadata.source.relation.task_id
    assert authority.mode == "http"
    altered = authority.original
    altered["sha256"] = "a" * 64
    assert authority.original["sha256"] == metadata.source.sha256
    assert metadata.captured.await_count == metadata.visible.await_count == 1
    assert metadata.visible.await_args.args[1:] == (authority.task_id, metadata.actor)
    assert metadata.visible.await_args.kwargs == {"lock": True}
    ref = service.authorization(authority)
    assert ref["original"] == service.receipt(metadata.source)
    assert ref["task_id"] == str(authority.task_id)
    serialized = json.dumps(ref)
    assert "private-password-hash" not in serialized
    assert "proof" not in serialized and "jwt" not in serialized.lower()
    delegated = await service.delegated(object(), json.loads(serialized))
    assert delegated.proof is None
    assert delegated.key == metadata.key
    assert metadata.visible.await_count == 2
    with pytest.raises(FrozenInstanceError):
        delegated.owner_id = uuid.uuid4()


@pytest.mark.parametrize("presented", [None, SimpleNamespace(headers={})])
async def test_authorize_requires_the_real_http_request(metadata, presented):
    with pytest.raises(ContentError, match="pyramid_authority_unrecorded"):
        await service.authorize(
            object(), metadata.source.relation.task_id, metadata.actor, presented
        )
    metadata.captured.assert_not_awaited()


@pytest.mark.parametrize(
    "replacement",
    [
        ("rotated-hash", ("annotations:read",)),
        ("private-password-hash", ("annotations:read", "datasets:read")),
        ("private-password-hash", ("datasets:read",)),
    ],
)
async def test_delegated_same_id_key_rotation_or_scope_change_is_rejected(
    metadata, replacement
):
    ref = service.authorization(await authorized(metadata))
    metadata.key = (*replacement, metadata.key[2])
    with pytest.raises(HTTPException) as exc:
        await service.delegated(object(), ref)
    assert exc.value.status_code == 403


async def test_delegated_visibility_revocation_is_checked_fresh(metadata):
    ref = service.authorization(await authorized(metadata))
    metadata.visible.side_effect = HTTPException(404, "Task not found")
    with pytest.raises(HTTPException) as exc:
        await service.delegated(object(), ref)
    assert exc.value.status_code == 404


async def test_http_read_only_wildcard_key_needs_no_dataset_management(metadata):
    metadata.key = (metadata.key[0], ("*",), metadata.key[2])
    authority = await authorized(metadata)
    assert authority.key[1] == ("*",)
    assert all(
        call.args[3] == "annotations:read"
        for call in metadata.actor_and_key.await_args_list
    )


def test_clock_rejects_expired_api_key_without_waiting_for_more_metadata():
    source = original()
    authority = service.Authority(
        "http",
        "task",
        source.relation.task_id,
        json.dumps(service.receipt(source)),
        None,
        key=(
            "hash",
            ("annotations:read",),
            datetime.now(timezone.utc) - timedelta(seconds=1),
        ),
    )
    with pytest.raises(HTTPException) as exc:
        service.clock(authority)
    assert exc.value.status_code == 403


def test_clock_retains_live_http_jwt_expiration_check():
    from app.services.comment_attachment_authority import Authority, JwtProof

    source = original()
    proof = Authority(
        uuid.uuid4(),
        None,
        None,
        JwtProof(datetime.now(timezone.utc) - timedelta(seconds=1), 1, "jwt-id"),
    )
    authority = service.Authority(
        "http",
        "task",
        source.relation.task_id,
        json.dumps(service.receipt(source)),
        None,
        proof=proof,
    )
    with pytest.raises(HTTPException) as exc:
        service.clock(authority)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("change", ["content", "locator", "relation"])
async def test_delegated_rebind_does_not_follow_current_owner(metadata, change):
    ref = service.authorization(await authorized(metadata))
    if change == "content":
        metadata.source = replace(metadata.source, content_id=uuid.uuid4())
    elif change == "locator":
        metadata.source = replace(metadata.source, bucket="rebound-bucket")
    else:
        metadata.source = replace(
            metadata.source,
            relation=replace(metadata.source.relation, project_id=uuid.uuid4()),
        )
    with pytest.raises(ContentError, match="content_binding_changed"):
        await service.delegated(object(), ref)


@pytest.mark.parametrize("value", [None, {}, {"mode": []}, {"mode": "system"}])
async def test_missing_or_partial_delegation_never_means_system(value):
    with pytest.raises(ContentError, match="pyramid_authority_unrecorded"):
        await service.delegated(object(), value)


async def test_system_requires_exact_canonical_owner_and_original(monkeypatch):
    source = original(task=False)
    ref = service.system_authorization("dataset_item", source.item_id, source)
    monkeypatch.setattr(service.original_sources, "_reads", reads)
    current = AsyncMock(return_value=source)
    monkeypatch.setattr(service, "pin", current)
    actor = AsyncMock()
    monkeypatch.setattr(service.access, "actor_and_key", actor)
    authority = await service.delegated(object(), ref)
    assert authority.mode == "system" and authority.actor_id is None
    assert current.await_args.args[1:] == ("dataset_item", source.item_id)
    assert current.await_args.kwargs == {"lock": False}
    actor.assert_not_awaited()
    for kind, identifier in (("task", source.item_id), ("dataset_item", uuid.uuid4())):
        with pytest.raises(ContentError, match="pyramid_authority_unrecorded"):
            service.system_authorization(kind, identifier, source)
    current.return_value = replace(source, content_id=uuid.uuid4())
    with pytest.raises(ContentError, match="content_binding_changed"):
        await service.delegated(object(), ref)


async def test_pin_uses_canonical_primary_relation_and_keeps_directory_boundary(
    monkeypatch,
):
    source = original()
    source = replace(
        source,
        relation=replace(
            source.relation,
            direct_item_id=None,
            primary_item_id=source.item_id,
            primary_link_id=uuid.uuid4(),
        ),
    )
    monkeypatch.setattr(
        service.original_sources,
        "resolve_task_relation",
        AsyncMock(return_value=source.relation),
    )
    resolve = AsyncMock(return_value=source)
    monkeypatch.setattr(service.original_sources, "resolve_task", resolve)
    owned = AsyncMock(return_value=False)
    monkeypatch.setattr(service, "owns_media", owned)
    assert await service.pin(object(), "task", source.relation.task_id) == source
    assert owned.await_args.args[1] == source.item_id
    owned.return_value = True
    with pytest.raises(ContentError, match="directory_cache_not_supported"):
        await service.pin(object(), "task", source.relation.task_id)
    assert resolve.await_count == 1


def test_reference_receipt_remains_explicitly_unsupported():
    source = replace(original(), object_key="__aap_reference__/blocked")
    with pytest.raises(ContentError, match="reference_operation_not_supported"):
        service.receipt(source)


async def test_fresh_reads_own_and_close_session_without_committing_caller(monkeypatch):
    source, authority, bind = original(), object(), object()
    caller = SimpleNamespace(
        commit=AsyncMock(), rollback=AsyncMock(), pending=["write"]
    )
    events = []

    class Owned:
        def __init__(self, **options):
            assert options == {
                "bind": bind,
                "expire_on_commit": False,
                "autoflush": False,
            }

        async def __aenter__(self):
            events.append("start")
            return self

        async def __aexit__(self, *_):
            events.append("close")

    async def check(db, received, *, lock=False):
        assert isinstance(db, Owned) and received is authority and lock
        return source

    monkeypatch.setattr(service, "AsyncSession", Owned)
    monkeypatch.setattr(service.storage_access, "engine", lambda received: bind)
    monkeypatch.setattr(service, "check", check)
    monkeypatch.setattr(service, "clock", lambda *_: None)
    await service._fresh(caller, authority, source, None)
    assert events == ["start", "close"] and caller.pending == ["write"]
    caller.commit.assert_not_awaited()
    caller.rollback.assert_not_awaited()


class Permit:
    def __init__(self, events):
        self.events = events
        self.error = None

    async def __aenter__(self):
        self.events.append("permit_start")
        return self

    async def __aexit__(self, _kind, exc, _traceback):
        self.error = exc
        self.events.append("permit_end")


@pytest.fixture
def media_setup(monkeypatch):
    monkeypatch.setattr(
        service.original_sources, "read_options", AsyncMock(return_value={})
    )
    state = SimpleNamespace(source=original(), events=[], provider=Provider())
    state.authority = object()
    state.permit = Permit(state.events)
    state.open_sessions = 0

    @asynccontextmanager
    async def session(_db):
        state.open_sessions += 1
        try:
            yield object()
        finally:
            state.open_sessions -= 1

    async def check(_db, _authority, *, lock=False):
        assert lock
        state.events.append("check")
        return state.source

    async def acquire(_db, location_id, purpose, *, expected_revision):
        assert location_id == state.source.location_id
        assert expected_revision == state.source.maintenance_revision
        assert purpose == "image_pyramid_original"
        return state.permit

    def create_client(**options):
        assert state.open_sessions == 0
        assert options["timeout_seconds"] == 30
        state.events.append("client")
        return state.provider

    monkeypatch.setattr(service, "_session", session)
    monkeypatch.setattr(service, "check", check)
    monkeypatch.setattr(service, "clock", lambda *_: None)
    monkeypatch.setattr(service.storage_access, "acquire", acquire)
    monkeypatch.setattr(service.reader, "create_client", create_client)
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(2))
    return state


async def wait_thread(event):
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 2), 3)


def interrupt_first_shield(monkeypatch, entered):
    shield, first = asyncio.shield, True

    def interrupted(pending):
        nonlocal first
        if first:
            first = False

            async def interrupt():
                await wait_thread(entered)
                raise SoftTimeLimitExceeded()

            return interrupt()
        return shield(pending)

    monkeypatch.setattr(service.asyncio, "shield", interrupted)


async def test_media_uses_saved_locator_closes_body_and_stage_before_final_check(
    media_setup,
):
    state = media_setup
    guard = AsyncMock()
    async with service.media(
        object(), state.authority, state.source, guard=guard
    ) as stage:
        assert stage.file.read() == RAW
        assert state.open_sessions == 0
        assert all(body.closed for body in state.provider.bodies)
        assert service._slots._value == 1
        assert "permit_end" not in state.events
    assert stage.file.closed and service._slots._value == 2
    assert guard.await_count == 3
    assert state.events == [
        "check",
        "permit_start",
        "check",
        "client",
        "check",
        "permit_end",
    ]
    for action, params in state.provider.calls:
        if action in {"head", "get"}:
            assert params["Bucket"] == state.source.bucket
            assert params["Key"] == state.source.object_key


async def test_media_hash_mismatch_releases_only_after_body_and_client_close(
    media_setup,
):
    state = media_setup
    state.provider.actual = b"x" * len(RAW)
    with pytest.raises(ContentError, match="content_mismatch"):
        async with service.media(object(), state.authority, state.source):
            pytest.fail("Unverified bytes escaped")
    assert all(body.closed for body in state.provider.bodies)
    assert state.provider.closed == 1
    assert service._slots._value == 2
    assert state.events[-1] == "permit_end"


async def test_media_final_rebinding_rejects_success_after_stage_close(media_setup):
    state = media_setup
    source = state.source
    with pytest.raises(ContentError, match="content_binding_changed"):
        async with service.media(object(), state.authority, source) as stage:
            state.source = replace(source, bucket="changed-saved-bucket")
    assert stage.file.closed
    assert state.events[-1] == "permit_end"


async def test_native_cancellation_drains_before_stage_and_permit_close(media_setup):
    state = media_setup
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    captured = []
    marker = ContextVar("pyramid-native")
    marker.set("caller-context")

    def native(stage):
        entered.set()
        assert marker.get() == "caller-context"
        assert release.wait(3)
        assert not stage.file.closed and "permit_end" not in state.events
        exited.set()

    async def run():
        async with service.media(object(), state.authority, state.source) as stage:
            captured.append(stage)
            await service.physical(native, stage)

    pending = asyncio.create_task(run())
    try:
        await wait_thread(entered)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not captured[0].file.closed
        assert service._slots._value == 1
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert exited.is_set() and captured[0].file.closed
    assert service._slots._value == 2 and state.events[-1] == "permit_end"


async def test_body_close_cancellation_retains_source_admission_until_real_close(
    media_setup,
):
    state = media_setup
    entered, release = threading.Event(), threading.Event()

    def closing():
        entered.set()
        assert release.wait(3)

    state.provider.body_close_hook = closing

    async def run():
        async with service.media(object(), state.authority, state.source):
            pytest.fail("Capture canceled during physical close")

    pending = asyncio.create_task(run())
    try:
        await wait_thread(entered)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and "permit_end" not in state.events
        assert service._slots._value == 1
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert state.provider.closed == 1
    assert service._slots._value == 2
    assert state.events[-1] == "permit_end"


async def test_unknown_body_close_retains_capacity_and_marks_durable_permit(
    media_setup,
):
    state = media_setup
    state.provider.body_close_error = OSError("unknown handle closure")
    with pytest.raises(ContentError) as exc:
        async with service.media(object(), state.authority, state.source):
            pytest.fail("Unsettled close escaped")
    assert exc.value.storage_operation_unsettled
    assert state.permit.error.storage_operation_unsettled
    assert service._slots._value == 1


async def test_native_soft_limit_drains_before_source_and_permit_release(
    media_setup, monkeypatch
):
    state = media_setup
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    captured = []

    def native(stage):
        entered.set()
        assert release.wait(3)
        assert not stage.file.closed and "permit_end" not in state.events
        exited.set()

    async def run():
        async with service.media(object(), state.authority, state.source) as stage:
            captured.append(stage)
            # Inject the soft limit only at the native wait, after source capture.
            interrupt_first_shield(monkeypatch, entered)
            await service.physical(native, stage)

    pending = asyncio.create_task(run())
    try:
        await wait_thread(entered)
        await asyncio.sleep(0)
        assert not pending.done() and not captured[0].file.closed
    finally:
        release.set()
        with pytest.raises(SoftTimeLimitExceeded):
            await pending
    assert exited.is_set() and captured[0].file.closed
    assert service._slots._value == 2 and state.events[-1] == "permit_end"


async def test_read_soft_limit_drains_reader_before_permit_release(
    media_setup, monkeypatch
):
    state = media_setup
    entered, release = threading.Event(), threading.Event()

    def read(_body):
        entered.set()
        assert release.wait(3)

    state.provider.read_hook = read
    interrupt_first_shield(monkeypatch, entered)

    async def run():
        async with service.media(object(), state.authority, state.source):
            pytest.fail("Soft-limited reader yielded bytes")

    pending = asyncio.create_task(run())
    try:
        await wait_thread(entered)
        await asyncio.sleep(0)
        assert not pending.done() and "permit_end" not in state.events
        assert service._slots._value == 1
    finally:
        release.set()
        with pytest.raises(SoftTimeLimitExceeded):
            await pending
    assert all(body.closed for body in state.provider.bodies)
    assert state.provider.closed == 1 and service._slots._value == 2
    assert state.events[-1] == "permit_end"


async def test_physical_timeout_drains_and_preserves_late_unsettled_error():
    entered, release = threading.Event(), threading.Event()

    def native():
        entered.set()
        assert release.wait(3)
        raise storage_access.mark_unsettled(OSError("native handle unknown"))

    pending = asyncio.create_task(service.physical(native, timeout=0.01))
    try:
        await wait_thread(entered)
        await asyncio.sleep(0.03)
        assert not pending.done()
    finally:
        release.set()
        with pytest.raises(TimeoutError) as exc:
            await pending
    assert exc.value.storage_operation_unsettled
