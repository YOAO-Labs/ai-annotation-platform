"""Video input identity, original authority and real physical drain contracts."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
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

from app.services import video_derivative_originals as service
from app.services import original_sources, storage_access
from app.services.content_snapshot import ContentError


def original():
    item, dataset, task, project, asset, location = (uuid.uuid4() for _ in range(6))
    return original_sources.OriginalSource(
        "dataset_asset",
        asset,
        hashlib.sha256(b"video").hexdigest(),
        5,
        "video",
        item,
        original_sources.TaskRelation(
            task,
            project,
            None,
            "saved/input.mp4",
            "video",
            None,
            item,
            None,
            None,
            item,
            (),
        ),
        "saved-historical-originals",
        "saved/input.mp4",
        "http://127.0.0.1:9000",
        location,
        1,
        7,
        ("verified-asset",),
        original_sources.ItemFacts(
            item, dataset, "saved/input.mp4", "video", None, None
        ),
        None,
        None,
    )


@asynccontextmanager
async def reads(*_):
    yield


@pytest.fixture
def authority(monkeypatch):
    state = SimpleNamespace(source=original(), actor=SimpleNamespace(id=uuid.uuid4()))
    state.key_id = uuid.uuid4()
    # Existing video reads have no additional explicit read scope.
    state.key = ("private-hash", (), datetime.now(timezone.utc) + timedelta(hours=1))
    state.proof = SimpleNamespace(actor_id=state.actor.id, key_id=state.key_id)
    state.capture = AsyncMock(return_value=state.proof)

    async def actor(*_, **__):
        return state.actor, state.key

    async def pin(_db, kind, identifier, **_):
        assert kind == "task" and identifier == state.source.relation.task_id
        return state.source

    state.visible = AsyncMock()
    monkeypatch.setattr(service, "_actor_and_key", actor)
    monkeypatch.setattr(service, "pin", pin)
    monkeypatch.setattr(service.access, "capture_credentials", state.capture)
    monkeypatch.setattr(service.access, "visible_task", state.visible)
    monkeypatch.setattr(service.access, "check_clock", lambda *_: None)
    monkeypatch.setattr(service.original_sources, "_reads", reads)
    return state


async def authorize(state):
    return await service.authorize(
        object(),
        state.source.relation.task_id,
        state.actor,
        Request({"type": "http", "headers": []}),
    )


def test_receipt_keeps_locator_and_identity_but_excludes_derived_dimensions():
    source = original()
    expected = service.receipt(source)
    assert service.validate_receipt(expected) == expected
    assert service.receipt(replace(source, relation=None)) == expected
    assert (
        service.receipt(
            replace(
                source, item_facts=replace(source.item_facts, width=1920, height=1080)
            )
        )
        == expected
    )
    encoded = json.dumps(expected)
    assert all(
        private not in encoded
        for private in (source.bucket, source.object_key, source.endpoint_url)
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"bucket": "another-saved-bucket"},
        {"object_key": "another.mp4"},
        {"content_id": uuid.uuid4()},
        {"sha256": "a" * 64},
        {"size_bytes": 6},
        {"location_revision": 2},
        {"maintenance_revision": 8},
        {"ownership_facts": ("changed-owner",)},
    ],
)
def test_same_media_locator_and_owner_changes_remain_fenced(changes):
    source = original()
    assert service.receipt(replace(source, **changes)) != service.receipt(source)


def test_item_relationship_remains_fenced_when_dimensions_change():
    source = original()
    changed = replace(
        source, item_facts=replace(source.item_facts, dataset_id=uuid.uuid4())
    )
    assert service.receipt(changed) != service.receipt(source)


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_kind": "task_original"},
        {"media_type": "image"},
        {"item_id": None},
        {"item_facts": None},
        {"size_bytes": True},
        {"size_bytes": service.MAX_SOURCE_BYTES + 1},
        {"sha256": "A" * 64},
    ],
)
def test_unsupported_or_unverified_inputs_do_not_gain_a_receipt(changes):
    with pytest.raises(ContentError):
        service.receipt(replace(original(), **changes))


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("owner_kind", []),
        ("content_id", "not-a-uuid"),
        ("size_bytes", False),
        ("sha256", None),
        ("binding", "B" * 64),
    ],
)
def test_malformed_saved_receipts_are_rejected(field, value):
    expected = service.receipt(original())
    expected[field] = value
    with pytest.raises(ContentError):
        service.validate_receipt(expected)


@pytest.mark.asyncio
async def test_http_keeps_no_extra_scope_and_freezes_specific_task(authority):
    captured = await authorize(authority)
    saved = service.authorization(captured)
    assert saved["policy"] == "task" and saved["owner_id"] == str(
        authority.source.relation.task_id
    )
    assert "private-hash" not in json.dumps(saved)
    delegated = await service.delegated(object(), saved)
    assert delegated.key == authority.key
    assert (
        await service.check(object(), delegated)
    ).content_id == authority.source.content_id
    assert authority.visible.await_count >= 3


@pytest.mark.asyncio
async def test_same_id_key_rotation_is_not_accepted(authority):
    captured = await authorize(authority)
    saved = service.authorization(captured)
    authority.key = ("rotated-hash", authority.key[1], authority.key[2])
    with pytest.raises(HTTPException) as raised:
        await service.delegated(object(), saved)
    assert raised.value.status_code == 403
    with pytest.raises(HTTPException):
        await service.check(object(), captured)


@pytest.mark.asyncio
async def test_other_visible_task_cannot_substitute_for_initiating_relation(authority):
    captured = await authorize(authority)
    authority.source = replace(
        authority.source,
        relation=replace(authority.source.relation, batch_id=uuid.uuid4()),
    )
    with pytest.raises(ContentError, match="content_binding_changed"):
        await service.check(object(), captured)


@pytest.mark.asyncio
async def test_no_request_is_never_internal_system_work(authority):
    with pytest.raises(ContentError):
        await service.authorize(
            object(), authority.source.relation.task_id, authority.actor, None
        )
    authority.capture.assert_not_awaited()
    with pytest.raises(ContentError):
        await service.delegated(object(), None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", []),
        ("policy", []),
        ("owner_kind", []),
        ("owner_id", "bad"),
        ("task_binding", None),
        ("scope_binding", "a" * 64),
        ("lifecycle_revision", 1),
    ],
)
async def test_delegation_shape_cannot_expand_authority(authority, field, value):
    saved = service.authorization(await authorize(authority))
    saved[field] = value
    with pytest.raises(ContentError):
        await service.delegated(object(), saved)


def test_system_delegation_is_explicit_and_bound_to_owner():
    source = replace(original(), relation=None)
    value = service.system_authorization("dataset_item", source.item_id, source)
    assert value["mode"] == value["policy"] == "system"
    with pytest.raises(ContentError):
        service.system_authorization("dataset_item", uuid.uuid4(), source)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [asyncio.CancelledError, SoftTimeLimitExceeded])
async def test_physical_cancel_or_soft_limit_does_not_abandon_native_thread(
    monkeypatch, interrupt
):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def native():
        entered.set()
        assert release.wait(3)
        closed.set()

    real_shield = asyncio.shield
    first = True

    async def interrupted(pending):
        nonlocal first
        if first:
            first = False
            while not entered.is_set():
                await asyncio.sleep(0)
            raise interrupt()
        return await real_shield(pending)

    monkeypatch.setattr(service.asyncio, "shield", interrupted)
    task = asyncio.create_task(service.physical(native))
    try:
        while not entered.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
        release.set()
        with pytest.raises(interrupt):
            await task
        assert closed.is_set()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_unknown_late_physical_close_marks_interruption(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def native():
        entered.set()
        assert release.wait(3)
        raise storage_access.mark_unsettled(OSError("unknown close"))

    task = asyncio.create_task(service.physical(native))
    try:
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert raised.value.storage_operation_unsettled
    finally:
        release.set()


@pytest.mark.parametrize("second", [SoftTimeLimitExceeded, KeyboardInterrupt])
async def test_repeated_non_cancel_interruption_still_drains_native_work(
    monkeypatch, second
):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def native():
        entered.set()
        assert release.wait(3)
        closed.set()

    real_shield = asyncio.shield
    count = 0

    async def interrupted(pending):
        nonlocal count
        count += 1
        if count <= 2:
            while not entered.is_set():
                await asyncio.sleep(0)
            raise (asyncio.CancelledError() if count == 1 else second())
        return await real_shield(pending)

    monkeypatch.setattr(service.asyncio, "shield", interrupted)
    task = asyncio.create_task(service.physical(native))
    try:
        while count < 3:
            await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
    finally:
        release.set()
