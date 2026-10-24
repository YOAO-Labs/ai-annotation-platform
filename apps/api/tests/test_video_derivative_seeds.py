"""E2E video fixtures retain production publication and cleanup ownership."""

from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

from fastapi import HTTPException
import pytest

from app.api.v1 import _test_seed as seed
from app.api.v1 import _test_seed_webcodecs as fixtures
from app.services import storage_access, video_derivative_originals as originals
from app.services import video_derivative_objects as objects, video_derivatives
from app.services import video_derivative_storage as physical
from app.workers import media_derivatives as worker


async def test_pending_fixture_saves_one_real_claim_before_any_physical_build(
    monkeypatch,
):
    item, source, authority = SimpleNamespace(id=uuid.uuid4()), object(), object()
    output = SimpleNamespace(id=uuid.uuid4())
    delegation, token, events = {"mode": "system"}, uuid.uuid4(), []
    db = SimpleNamespace(commit=AsyncMock(side_effect=lambda: events.append("commit")))
    monkeypatch.setattr(seed, "_require_fixture_video_item", AsyncMock())
    monkeypatch.setattr(originals, "pin", AsyncMock(return_value=source))
    monkeypatch.setattr(
        originals, "system_authorization", MagicMock(return_value=delegation)
    )
    monkeypatch.setattr(originals, "delegated", AsyncMock(return_value=authority))
    monkeypatch.setattr(
        video_derivatives, "prepare", AsyncMock(return_value=(output, True))
    )
    monkeypatch.setattr(objects, "claim", AsyncMock(return_value=token))
    publish = AsyncMock()
    monkeypatch.setattr(seed, "_publish_fixture_video_outputs", publish)
    spec = {"chunk_id": 0}
    assert await seed._stage_fixture_video_outputs(
        db, item, "chunk", [spec], pending=True
    ) == [output.id]
    originals.system_authorization.assert_called_once_with(
        "dataset_item", item.id, source
    )
    video_derivatives.prepare.assert_awaited_once_with(
        db, item.id, authority, "chunk", spec=spec
    )
    objects.claim.assert_awaited_once_with(
        db, output, ttl_seconds=originals.JOB_SECONDS
    )
    assert events == ["commit"]
    publish.assert_not_awaited()


async def test_transition_publishes_the_existing_claim_without_resetting_its_token(
    monkeypatch,
):
    token, output_id, item_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    item = SimpleNamespace(id=item_id)
    row = SimpleNamespace(output_id=output_id, status="pending")
    output = SimpleNamespace(
        id=output_id, dataset_item_id=item_id, state="building", lease_token=token
    )
    db = SimpleNamespace(
        get=AsyncMock(side_effect=[item, output]),
        scalar=AsyncMock(return_value=row),
        refresh=AsyncMock(side_effect=lambda _row: setattr(row, "status", "ready")),
    )
    monkeypatch.setattr(seed, "_require_fixture_video_item", AsyncMock())
    publish = AsyncMock()
    monkeypatch.setattr(seed, "_publish_fixture_video_outputs", publish)
    result = await seed.seed_video_webcodecs_transition(
        seed.SeedVideoWebCodecsTransitionRequest(dataset_item_id=str(item_id)), db
    )
    publish.assert_awaited_once_with(db, [output_id])
    assert output.lease_token == token
    assert result.status == "ready"


@pytest.mark.parametrize("valid", [False, True])
async def test_fixture_scope_rejects_ordinary_items_before_generation(
    monkeypatch, valid
):
    dataset_id = uuid.uuid4()
    item = SimpleNamespace(
        file_type="video",
        dataset_id=dataset_id,
        file_path="e2e/video/source.mp4",
        metadata_={"_e2e_video_fixture": "baseline"},
    )
    scope = SimpleNamespace(dataset_ids=(dataset_id,) if valid else ())
    monkeypatch.setattr(seed, "_resolve_fixture_scope", AsyncMock(return_value=scope))
    if valid:
        await seed._require_fixture_video_item(object(), item)
    else:
        with pytest.raises(HTTPException) as failure:
            await seed._require_fixture_video_item(object(), item)
        assert failure.value.status_code == 422


async def test_fixture_publication_waits_for_source_and_output_close(monkeypatch):
    import sqlalchemy.ext.asyncio as sqlalchemy_async

    output = SimpleNamespace(
        id=uuid.uuid4(),
        dataset_item_id=uuid.uuid4(),
        state="building",
        lease_token=uuid.uuid4(),
        metadata_output_id=None,
        original_ref={"source": "exact"},
        auth_ref={"mode": "system"},
    )
    source, authority, stage, client = object(), object(), object(), object()
    events = []
    db = SimpleNamespace(
        get=AsyncMock(side_effect=[output, object()]),
        commit=AsyncMock(side_effect=lambda: events.append("stage-commit")),
    )
    fresh = SimpleNamespace(
        commit=AsyncMock(side_effect=lambda: events.append("publish-commit"))
    )

    @asynccontextmanager
    async def session():
        yield fresh

    monkeypatch.setattr(
        sqlalchemy_async, "async_sessionmaker", lambda *_a, **_k: session
    )
    monkeypatch.setattr(storage_access, "engine", lambda _db: object())
    monkeypatch.setattr(seed, "_require_fixture_video_item", AsyncMock())
    monkeypatch.setattr(originals, "delegated", AsyncMock(return_value=authority))
    monkeypatch.setattr(originals, "check", AsyncMock(return_value=source))
    monkeypatch.setattr(objects, "snapshot", lambda value: value)
    monkeypatch.setattr(video_derivatives, "check", AsyncMock())

    @asynccontextmanager
    async def media(actual_db, actual_authority, actual_source):
        assert (actual_db, actual_authority, actual_source) == (db, authority, source)
        events.append("source-open")
        yield stage
        events.append("source-close")

    @asynccontextmanager
    async def access(actual_db, actual_output, guard):
        assert (actual_db, actual_output) == (db, output)
        await guard(7)
        events.append("output-open")
        yield client
        events.append("output-close")
        await guard(7)

    result, inventory = {"kind": "metadata"}, {"schema": 1}
    build = MagicMock(
        side_effect=lambda *_a: events.append("build") or (result, inventory)
    )
    monkeypatch.setattr(worker, "build", build)
    monkeypatch.setattr(originals, "media", media)
    monkeypatch.setattr(objects, "access", access)
    monkeypatch.setattr(
        originals,
        "physical",
        AsyncMock(side_effect=lambda call, *args, **_k: call(*args)),
    )
    publish = AsyncMock(side_effect=lambda *_a: events.append("publish"))
    monkeypatch.setattr(video_derivatives, "publish", publish)
    await seed._publish_fixture_video_outputs(db, [output.id])
    build.assert_called_once_with(client, output, stage, None)
    publish.assert_awaited_once_with(
        fresh, output, authority, output.lease_token, result, inventory, 7
    )
    assert events == [
        "stage-commit",
        "source-open",
        "output-open",
        "build",
        "output-close",
        "source-close",
        "publish",
        "publish-commit",
    ]


@pytest.mark.parametrize("fixture", ["unsupported-config", "malformed-samples"])
def test_bad_samples_are_response_faults_without_mutating_the_valid_baseline(fixture):
    baseline = {
        "codec_string": "avc1.42c00a",
        "samples": [
            {"offset_in_chunk": 0, "size_bytes": 4},
            {"offset_in_chunk": 4, "size_bytes": 4},
        ],
    }
    original = deepcopy(baseline)
    fault = fixtures.apply_metadata_mutation(fixture, baseline)
    assert baseline == original
    assert fault != original
    assert fixtures.frame_expectations(fixture)["response_fault"] == {
        "target": "chunk-samples",
        "mutation": fixture,
    }
    assert fixtures.frame_expectations("h264-baseline-gop12")["response_fault"] is None


async def test_cleanup_retains_claim_and_rows_when_an_operation_is_unsettled(
    monkeypatch,
):
    output = SimpleNamespace(id=uuid.uuid4(), state="building")
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())
    monkeypatch.setattr(
        seed,
        "_fixture_video_outputs",
        AsyncMock(return_value=((uuid.uuid4(),), [output])),
    )
    monkeypatch.setattr(seed, "_fixture_video_source_operations", AsyncMock())
    monkeypatch.setattr(seed, "_fixture_video_foreign_refs", AsyncMock())
    monkeypatch.setattr(objects, "snapshot", lambda value: value)
    monkeypatch.setattr(objects, "_in_use", AsyncMock(return_value=True))
    with pytest.raises(HTTPException) as failure:
        await seed._prepare_fixture_video_cleanup(db, object())
    assert failure.value.detail == {"code": "e2e_seed_video_operation_unsettled"}
    assert output.state == "building"
    db.commit.assert_not_awaited()


async def test_cleanup_rejects_any_foreign_reference_before_destructive_work():
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[uuid.uuid4(), None, None, None]),
        execute=AsyncMock(),
        commit=AsyncMock(),
    )
    with pytest.raises(HTTPException) as failure:
        await seed._fixture_video_foreign_refs(db, [uuid.uuid4()], [uuid.uuid4()])
    assert failure.value.detail == {"code": "e2e_seed_video_foreign_reference"}
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("close_fails", [False, True])
async def test_cleanup_deletes_only_saved_owner_and_keeps_rows_on_close_failure(
    monkeypatch, close_fails
):
    import sqlalchemy.ext.asyncio as sqlalchemy_async

    scope = object()
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        state="retiring",
        storage_ref={"location_id": str(uuid.uuid4())},
    )
    plan = seed._FixtureVideoCleanup(scope, (), (receipt,))
    client, db = object(), SimpleNamespace(delete=AsyncMock())
    fresh = SimpleNamespace(
        scalar=AsyncMock(return_value=receipt),
        scalars=AsyncMock(return_value=[SimpleNamespace(revision=7)]),
        commit=AsyncMock(),
    )

    @asynccontextmanager
    async def session():
        yield fresh

    @asynccontextmanager
    async def access(actual_db, actual_receipt, guard):
        assert (actual_db, actual_receipt) == (db, receipt)
        await guard(7)
        yield client
        if close_fails:
            raise RuntimeError("owned close uncertain")
        await guard(7)

    monkeypatch.setattr(
        sqlalchemy_async, "async_sessionmaker", lambda *_a, **_k: session
    )
    monkeypatch.setattr(storage_access, "engine", lambda _db: object())
    monkeypatch.setattr(seed, "_fixture_video_source_operations", AsyncMock())
    monkeypatch.setattr(objects, "snapshot", lambda value: value)
    monkeypatch.setattr(objects, "access", access)
    remove = MagicMock(return_value=2)
    monkeypatch.setattr(physical, "delete_output", remove)
    monkeypatch.setattr(
        originals,
        "physical",
        AsyncMock(side_effect=lambda call, *args, **_k: call(*args)),
    )
    resolve = AsyncMock(return_value=scope)
    if close_fails:
        with pytest.raises(RuntimeError, match="close uncertain"):
            await seed._delete_fixture_video_objects(db, plan, resolve)
    else:
        await seed._delete_fixture_video_objects(db, plan, resolve)
    remove.assert_called_once_with(client, receipt)
    db.delete.assert_not_awaited()
    assert receipt.state == "retiring"
