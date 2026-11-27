"""E2E video fixtures retain production publication and cleanup ownership."""

from contextlib import asynccontextmanager, nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import asyncio
import json
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.api.v1 import _test_seed as seed
from app.api.v1 import _test_seed_webcodecs as fixtures
from app.services import storage_access, video_derivative_originals as originals
from app.services import video_derivative_objects as objects, video_derivatives
from app.services import video_derivative_storage as physical
from app.services import video_frame_service as frame_service
from app.workers import media_derivatives as worker


async def test_pending_fixture_saves_one_real_claim_before_any_physical_build(
    monkeypatch,
):
    item = SimpleNamespace(id=uuid.uuid4(), dataset_id=uuid.uuid4())
    source, authority = object(), object()
    run_id, token, events = uuid.uuid4(), uuid.uuid4(), []
    output = SimpleNamespace(
        id=uuid.uuid4(),
        dataset_item_id=item.id,
        lease_token=token,
        original_ref={"source": "exact"},
        storage_ref={"location_id": str(uuid.uuid4())},
    )
    delegation = {"mode": "system"}
    db = SimpleNamespace(commit=AsyncMock(side_effect=lambda: events.append("commit")))
    runtime = SimpleNamespace(
        record_video_seed_claims=MagicMock(
            side_effect=lambda *_: events.append("record")
        )
    )
    monkeypatch.setattr(
        seed, "_require_fixture_video_item", AsyncMock(return_value=run_id)
    )
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
        db, item, "chunk", [spec], pending=True, runtime=runtime
    ) == [output.id]
    originals.system_authorization.assert_called_once_with(
        "dataset_item", item.id, source
    )
    video_derivatives.prepare.assert_awaited_once_with(
        db, item.id, authority, "chunk", spec=spec, force=True
    )
    objects.claim.assert_awaited_once_with(
        db, output, ttl_seconds=originals.JOB_SECONDS
    )
    runtime.record_video_seed_claims.assert_called_once_with(
        run_id,
        {
            str(output.id): {
                "dataset_id": str(item.dataset_id),
                "item_id": str(item.id),
                "token": str(token),
                "original_ref": output.original_ref,
                "storage_ref": output.storage_ref,
            }
        },
    )
    assert events == ["record", "commit"]
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
    from app.services import fixture_run_ownership as ownership

    run_id = uuid.uuid4()
    item = SimpleNamespace(
        id=uuid.uuid4(),
        file_type="video",
        dataset_id=uuid.uuid4(),
        file_path="e2e/video/source.mp4",
        metadata_={"_e2e_video_fixture": "baseline"},
    )
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[run_id, uuid.uuid4() if valid else None])
    )
    monkeypatch.setattr(ownership, "lock_original_scope", AsyncMock())
    if valid:
        assert await seed._require_fixture_video_item(db, item) == run_id
    else:
        with pytest.raises(HTTPException) as failure:
            await seed._require_fixture_video_item(db, item)
        assert failure.value.status_code == 422


@pytest.mark.parametrize("outcome", ["ready", "error", "unsettled"])
async def test_fixture_publication_waits_for_source_and_output_close(
    monkeypatch, tmp_path, outcome
):
    import sqlalchemy.ext.asyncio as sqlalchemy_async

    from app.services import media_cache_accounting, media_native_stage

    output = SimpleNamespace(
        id=uuid.uuid4(),
        dataset_item_id=uuid.uuid4(),
        state="building",
        lease_token=uuid.uuid4(),
        metadata_output_id=None,
        original_ref={"source": "exact"},
        auth_ref={"mode": "system"},
        storage_ref={"endpoint_url": "http://127.0.0.1:9000", "bucket": "cache"},
    )
    source, authority, stage, client = object(), object(), object(), object()
    permit = SimpleNamespace(id=uuid.uuid4(), location_id=uuid.uuid4(), revision=1)
    path = tmp_path / f"aap-media-stage-{permit.id}"
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
    monkeypatch.setattr(
        media_native_stage.tempfile, "gettempdir", lambda: str(tmp_path)
    )

    async def checkpoint(native_stage):
        assert native_stage.permit_id == permit.id
        assert native_stage.location_id == permit.location_id
        assert native_stage.receipt["output_id"] == str(output.id)
        assert native_stage.receipt["kind"] == "video_output"
        status = native_stage.receipt["state"]
        assert path.exists() is (status == "opened")
        events.append(status)

    async def finish(recorder, *, settled):
        assert recorder.owner_id == output.id
        assert recorder.producer == "video_derivative_outputs"
        assert recorder.bucket == "cache"
        assert settled is (outcome != "unsettled")
        events.append("accounted")

    monkeypatch.setattr(media_native_stage.NativeStage, "_checkpoint", checkpoint)
    monkeypatch.setattr(media_cache_accounting.Recorder, "finish", finish)

    @asynccontextmanager
    async def media(actual_db, actual_authority, actual_source):
        assert (actual_db, actual_authority, actual_source) == (db, authority, source)
        events.append("source-open")
        try:
            yield stage
        finally:
            events.append("source-close")

    @asynccontextmanager
    async def access(
        actual_db, actual_output, guard, *, single_attempt, on_acquired, owned
    ):
        assert (actual_db, actual_output) == (db, output)
        assert single_attempt is True and owned is True
        await on_acquired(permit)
        await guard(7)
        events.append("output-open")
        try:
            yield client
        finally:
            events.append("output-close")
        await guard(7)

    result, inventory = {"kind": "metadata"}, {"schema": 1}
    failure = RuntimeError("native video failed")
    if outcome == "unsettled":
        storage_access.mark_unsettled(failure)

    def build(
        actual_client,
        actual_output,
        actual_stage,
        metadata,
        *,
        accounting,
        native_stage,
    ):
        assert (actual_client, actual_output, actual_stage, metadata) == (
            client,
            output,
            stage,
            None,
        )
        assert accounting.owner_id == output.id
        with native_stage as directory:
            assert directory == path.resolve()
            (directory / "frame.webp").write_bytes(b"video fixture")
            events.append("build")
            if outcome != "ready":
                raise failure
        return result, inventory

    monkeypatch.setattr(worker, "build", build)
    monkeypatch.setattr(originals, "media", media)
    monkeypatch.setattr(objects, "access", access)
    settle_failure = AsyncMock(side_effect=lambda *_: events.append("fail"))
    monkeypatch.setattr(worker, "fail_with_retry", settle_failure)
    publish = AsyncMock(side_effect=lambda *_a: events.append("publish"))
    monkeypatch.setattr(video_derivatives, "publish", publish)
    expected = [
        "stage-commit",
        "source-open",
        "output-open",
        "prepared",
        "opened",
        "build",
    ]
    if outcome != "unsettled":
        expected.append("settled")
    expected.extend(["output-close", "accounted", "source-close"])
    if outcome == "ready":
        await seed._publish_fixture_video_outputs(db, [output.id])
        publish.assert_awaited_once_with(
            fresh, output, authority, output.lease_token, result, inventory, 7
        )
        expected.extend(["publish", "publish-commit"])
    else:
        with pytest.raises(RuntimeError) as raised:
            await seed._publish_fixture_video_outputs(db, [output.id])
        assert raised.value is failure
        publish.assert_not_awaited()
    if outcome == "error":
        assert settle_failure.await_args.args[1:3] == (output.id, output.lease_token)
        expected.append("fail")
    else:
        settle_failure.assert_not_awaited()
    assert path.exists() is (outcome == "unsettled")
    assert events == expected


@pytest.mark.parametrize("fixture_owned", [False, True])
async def test_product_dispatch_defers_only_fixture_requests(
    monkeypatch, fixture_owned
):
    output = SimpleNamespace(
        state="building", fixture_request_versions={} if fixture_owned else None
    )
    ctx = SimpleNamespace(item=SimpleNamespace(id=uuid.uuid4()), authority=object())
    monkeypatch.setattr(
        frame_service.derivatives, "prepare", AsyncMock(return_value=(output, True))
    )
    monkeypatch.setattr(frame_service.objects, "snapshot", lambda row: row)
    prepared, ordinary_dispatch = await frame_service._prepare(
        object(), ctx, "chunk", {}
    )
    assert prepared is output
    assert ordinary_dispatch is not fixture_owned


async def test_request_ownership_marker_cannot_be_supplied_by_caller(db_session):
    with pytest.raises(DBAPIError, match="fixture_video_request_versions_immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO public.video_derivative_outputs "
                    "(id,fixture_request_versions) VALUES (:id,'{}'::jsonb)"
                ),
                {"id": uuid.uuid4()},
            )


def _video_request_drain_runtime(run_id, owner_url=None):
    from app.services import fixture_native_runtime as native

    runtime = object.__new__(native.FixtureNativeRuntime)
    runtime.suite_id = uuid.uuid4()
    runtime.runs = {
        run_id: {
            "journal": {"state": "ready"},
            "owner_url": owner_url,
            "video_publisher": (
                asyncio.Event(),
                asyncio.get_running_loop().create_future(),
            ),
        }
    }
    return runtime


async def test_fixture_seal_drain_retains_pending_request(monkeypatch):
    from app.services import fixture_native_runtime as native

    run_id, output = uuid.uuid4(), uuid.uuid4()
    runtime = _video_request_drain_runtime(run_id)
    db = SimpleNamespace(scalar=AsyncMock(side_effect=[True, [output], []]))
    extension = AsyncMock(side_effect=RuntimeError("fixture_video_request_drain_busy"))
    monkeypatch.setattr(runtime, "_wait_video_request_publication", extension)
    delay = AsyncMock()
    monkeypatch.setattr(native.asyncio, "sleep", delay)
    await runtime.drain_video_requests_before_seal(
        db, run_id, asyncio.get_running_loop().time() + 5
    )
    assert db.scalar.await_count == 3
    delay.assert_awaited_once()
    extension.assert_not_awaited()

    db.scalar = AsyncMock(return_value=True)
    with pytest.raises(RuntimeError, match="fixture_video_request_drain_busy"):
        await runtime.drain_video_requests_before_seal(
            db, run_id, asyncio.get_running_loop().time() - 1
        )
    db.scalar.assert_awaited_once()
    extension.assert_not_awaited()


@pytest.mark.parametrize(
    "outcome",
    [
        "published",
        "published-as-worker-released",
        "queue-only",
        "queue-with-worker",
        "stalled",
        "unknown",
        "unproven",
        "lost-worker",
        "lost-generation",
        "disappeared",
        "publisher-failed",
        "publisher-stopping",
    ],
)
async def test_fixture_seal_drain_waits_only_for_exact_active_publication(
    monkeypatch, outcome
):
    from app.services import fixture_native_runtime as native

    run_id, output, lease, token = (uuid.uuid4() for _ in range(4))
    queued, queued_lease, queued_token = (uuid.uuid4() for _ in range(3))
    outputs = [output, queued] if outcome == "queue-with-worker" else [output]
    runtime = _video_request_drain_runtime(run_id)

    async def scalar(statement, _params):
        if "pg_try_advisory_xact_lock" in statement.text:
            return True
        return outputs

    db = SimpleNamespace(scalar=scalar)
    observed, publish = asyncio.Event(), asyncio.Event()
    call_count = 0

    async def execute(statement, params):
        nonlocal call_count
        assert statement is native._VIDEO_REQUEST_PUBLICATIONS
        assert params == {"run": run_id, "suite": runtime.suite_id, "outputs": outputs}
        call_count += 1
        if outcome == "unknown":
            rows = []
        elif outcome == "unproven":
            rows = [(output, lease, token, "unproven")]
        elif outcome == "queue-only":
            rows = [(output, None, None, "queued")]
        elif outcome == "queue-with-worker":
            if call_count == 1:
                rows = [
                    (output, lease, token, "running"),
                    (queued, None, None, "queued"),
                ]
                observed.set()
            else:
                await publish.wait()
                next_output = {
                    2: (None, None, "queued"),
                    3: (None, queued_token, "running"),
                    4: (queued_lease, queued_token, "published"),
                }[call_count]
                rows = [(output, lease, token, "published"), (queued, *next_output)]
        elif call_count == 1:
            rows = [(output, lease, token, "running")]
            observed.set()
        else:
            await publish.wait()
            if outcome == "disappeared":
                rows = []
            elif outcome == "lost-generation":
                rows = [(output, uuid.uuid4(), token, "published")]
            elif outcome == "lost-worker" or (
                outcome == "published-as-worker-released" and call_count == 2
            ):
                rows = [(output, lease, token, "unlocked")]
            else:
                rows = [(output, lease, token, "published")]
        return SimpleNamespace(all=lambda: rows)

    @asynccontextmanager
    async def connect():
        yield SimpleNamespace(execute=execute)

    dispose = AsyncMock()
    monkeypatch.setattr(
        native,
        "create_async_engine",
        lambda *_args, **_kwargs: SimpleNamespace(connect=connect, dispose=dispose),
    )
    monkeypatch.setattr(native, "_VIDEO_REQUEST_PUBLICATION_SECONDS", 0.3)
    deadline = asyncio.get_running_loop().time() + 0.1
    async with asyncio.timeout(2):
        async with asyncio.TaskGroup() as group:

            async def drain():
                if outcome in {
                    "published",
                    "published-as-worker-released",
                    "queue-with-worker",
                }:
                    await runtime.drain_video_requests_before_seal(db, run_id, deadline)
                else:
                    code = {
                        "publisher-failed": "publisher_failed",
                        "publisher-stopping": "fixture_video_publisher_stopped",
                    }.get(outcome, "fixture_video_request_drain_busy")
                    with pytest.raises(RuntimeError, match=code):
                        await runtime.drain_video_requests_before_seal(
                            db, run_id, deadline
                        )

            cleanup = group.create_task(drain())
            if outcome not in {"unknown", "unproven", "queue-only"}:
                await observed.wait()
                assert not cleanup.done()
                stopped, publisher = runtime.runs[run_id]["video_publisher"]
                if outcome == "publisher-failed":
                    publisher.set_exception(RuntimeError("publisher_failed"))
                elif outcome == "publisher-stopping":
                    stopped.set()
                if outcome != "stalled":
                    publish.set()
    assert call_count == {
        "unknown": 1,
        "unproven": 1,
        "queue-only": 1,
        "queue-with-worker": 4,
        "lost-worker": 3,
        "published-as-worker-released": 3,
        "publisher-failed": 1,
        "publisher-stopping": 1,
    }.get(outcome, 2)
    dispose.assert_awaited_once()


async def test_fixture_seal_drain_keeps_its_postgres_admission_budget(
    monkeypatch, test_engine
):
    run_id = uuid.uuid4()
    runtime = _video_request_drain_runtime(run_id, test_engine.url)
    extension = AsyncMock()
    monkeypatch.setattr(runtime, "_wait_video_request_publication", extension)
    shared = text(
        "SELECT pg_catalog.pg_try_advisory_xact_lock_shared("
        "pg_catalog.hashtextextended(:key,0))"
    )
    scope = {"key": "aap-fixture-video-requests:" + str(run_id)}
    loop = asyncio.get_running_loop()
    async with test_engine.connect() as entrant, test_engine.connect() as cleanup:
        assert await entrant.scalar(shared, scope) is True
        async with asyncio.timeout(2):
            with pytest.raises(RuntimeError, match="fixture_video_request_drain_busy"):
                await runtime.drain_video_requests_before_seal(
                    cleanup, run_id, loop.time() + 0.1
                )
        extension.assert_not_awaited()
        await entrant.rollback()
        await cleanup.rollback()
        await runtime.drain_video_requests_before_seal(cleanup, run_id, loop.time() + 2)
        assert await entrant.scalar(shared, scope) is False
        await cleanup.rollback()
        assert await entrant.scalar(shared, scope) is True


async def test_fixture_suite_cutoff_rejects_a_new_child(monkeypatch, test_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services import fixture_run_ownership as ownership

    run_id, late_id = uuid.uuid4(), uuid.uuid4()
    runtime = _video_request_drain_runtime(run_id)
    suite_id = runtime.suite_id
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(fixture_native_runtime=runtime))
    )
    payload = seed.SeedSuiteCleanupRequest(suite_id=suite_id)
    monkeypatch.setattr(seed.settings, "e2e_seed_enabled", True)

    drained, registered = asyncio.Event(), asyncio.Event()
    real_drain = runtime.drain_video_requests_before_seal

    async def pause_after_drain(db, child, deadline):
        assert child == run_id
        await real_drain(db, child, deadline)
        drained.set()
        await registered.wait()

    async def unexpected_retirement(*_args, **_kwargs):
        pytest.fail("suite reached retirement without preserving its cutoff")

    monkeypatch.setattr(runtime, "drain_video_requests_before_seal", pause_after_drain)
    monkeypatch.setattr(seed, "_retire_fixture_execution_work", unexpected_retirement)
    async with factory.begin() as db:
        await db.execute(
            text(
                "INSERT INTO yoao_fixture_v1.fixture_runs (id,family) VALUES (:id,'owned')"
            ),
            {"id": suite_id},
        )
        await ownership.start_owned_scope(
            db, suite_id=suite_id, run_id=run_id, namespace="first"
        )

    async def cleanup():
        async with factory() as db:
            with pytest.raises(HTTPException) as refused:
                await seed.seed_cleanup(payload, request, db)
            assert refused.value.status_code == 409
            assert refused.value.detail == {"code": "fixture_suite_scope_changed"}

    try:
        async with asyncio.timeout(5), asyncio.TaskGroup() as group:
            group.create_task(cleanup())
            await drained.wait()
            async with factory.begin() as db:
                await ownership.start_owned_scope(
                    db, suite_id=suite_id, run_id=late_id, namespace="late"
                )
            registered.set()
        async with factory() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT id,state FROM yoao_fixture_v1.fixture_runs WHERE id=ANY(:ids)"
                    ),
                    {"ids": [suite_id, run_id, late_id]},
                )
            ).all()
            assert dict(rows) == dict.fromkeys((suite_id, run_id, late_id), "open")
    finally:
        async with factory.begin() as db:
            await db.execute(
                text("DELETE FROM yoao_fixture_v1.fixture_runs WHERE id=ANY(:ids)"),
                {"ids": [run_id, late_id]},
            )
            await db.execute(
                text("DELETE FROM yoao_fixture_v1.fixture_runs WHERE id=:id"),
                {"id": suite_id},
            )


@pytest.mark.parametrize("stage", ["proof", "suite-cutoff"])
async def test_fixture_video_publication_proof_uses_private_postgres_state(
    db_session, test_engine, super_admin, monkeypatch, stage
):
    import os

    from app.services import fixture_native_runtime as native

    ids = {
        name: uuid.uuid4()
        for name in (
            "suite",
            "run",
            "user",
            "dataset",
            "item",
            "admission",
            "original",
            "creation",
            "operation",
            "asset",
            "target",
            "connection",
            "metadata",
            "output",
            "lease",
            "token",
            "publication",
            "version",
        )
    }
    ids.update(
        user=super_admin[0].id,
        sha="a" * 64,
        bucket=f"original-{ids['original'].hex}",
        output_bucket=f"output-{ids['target'].hex}",
        key=f"video/{ids['item']}",
        scope=f"d{ids['run'].hex[:11]}",
    )
    source = {
        "schema": 1,
        "owner_kind": "dataset_asset",
        "content_id": str(ids["asset"]),
        "location_id": str(ids["original"]),
        "sha256": ids["sha"],
        "size_bytes": 12,
        "location_revision": 1,
        "maintenance_revision": 1,
        "binding": "b" * 64,
    }
    ids["source"] = json.dumps(source)
    ids["auth"] = json.dumps(
        {
            "mode": "http",
            "owner_kind": "dataset_item",
            "owner_id": str(ids["item"]),
            "original": source,
        }
    )
    ids["saved"] = json.dumps(
        {
            "schema": 1,
            "purpose": "media-cache",
            "location_id": "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7",
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": "http://fixture.invalid",
            "bucket": "ordinary-media-cache",
            "prefix": f"video-derivatives/objects/{ids['metadata']}/",
            "storage_identity": "c" * 64,
        }
    )

    async def execute(sql, **params):
        return await db_session.execute(text(sql), {**ids, **params})

    for statement in (
        "INSERT INTO yoao_fixture_v1.fixture_runs (id,family) VALUES (:suite,'owned')",
        "INSERT INTO yoao_fixture_v1.fixture_runs (id,family,parent_run_id,scope_key) "
        "VALUES (:run,'owned',:suite,:scope)",
        "INSERT INTO yoao_fixture_v1.fixture_run_admissions (id,run_id,kind) "
        "VALUES (:admission,:run,'test')",
        "INSERT INTO public.datasets (id,display_id,name,created_by) "
        "VALUES (:dataset,:scope,'video drain',:user)",
        "INSERT INTO public.dataset_items (id,dataset_id,file_name,file_path,file_type,"
        "file_size,content_hash) VALUES (:item,:dataset,'movie.mp4',:key,'video',12,:sha)",
        "INSERT INTO public.storage_locations (id,name,kind,config) "
        "VALUES (:original,:scope,'s3','{\"endpoint\":\"fixture.invalid\"}')",
        "INSERT INTO public.storage_maintenance_gates (location_id) VALUES (:original)",
        "INSERT INTO public.managed_original_creations "
        "(id,owner_kind,dataset_id,location_id,storage_identity,bucket,object_key,logical_path,"
        "origin,max_bytes,timeout_seconds,content_type,authority,configuration,input_fingerprint,"
        "maintenance_revision,status,operation_id,prepared,sha256,md5,size_bytes,receipt,etag,"
        "version_id,verification,claimed_at,frozen_at,write_started_at,acknowledged_at,readback_at,"
        "settled_at,completed_at) VALUES "
        "(:creation,'dataset',:dataset,:original,yoao_assets_v1._managed_original_identity("
        "'http://fixture.invalid',:bucket),:bucket,:key,:key,'zip',12,60,'video/mp4',"
        "'{}','{}',:sha,1,'verified',:operation,'{}',:sha,repeat('a',32),12,'{}','etag',"
        "'original-version','{}',now(),now(),now(),now(),now(),now(),now())",
        "INSERT INTO public.dataset_assets "
        "(id,dataset_item_id,media_type,size_bytes,checksum_algorithm,checksum_value,verification_state) "
        "VALUES (:asset,:item,'video',12,'sha256',:sha,'verified')",
        "INSERT INTO public.dataset_asset_locations "
        "(asset_id,location_id,bucket,object_key,role,reference_state,creation_id) "
        "VALUES (:asset,:original,:bucket,:key,'managed_original','present',:creation)",
        "INSERT INTO public.video_derivative_outputs "
        "(id,dataset_item_id,kind,spec,original_ref,auth_ref,storage_ref) "
        "VALUES (:metadata,:item,'metadata','{}',CAST(:source AS jsonb),'{}',CAST(:saved AS jsonb))",
        "UPDATE public.video_derivative_outputs SET lease_token=:lease,"
        "lease_expires_at=now()+interval '1 minute' WHERE id=:metadata",
        "UPDATE public.video_derivative_outputs SET state='ready',result='{}',"
        "inventory=jsonb_build_object('schema',1,'objects','{}'::jsonb) WHERE id=:metadata",
        "UPDATE public.dataset_items SET video_output_id=:metadata WHERE id=:item",
        "INSERT INTO yoao_fixture_v1.fixture_run_resources "
        "(run_id,kind,resource_id,logical_key,state) VALUES "
        "(:run,'dataset',:dataset,'dataset','published'),(:run,'item',:item,'item','published')",
        "INSERT INTO yoao_fixture_v1.fixture_run_resources "
        "(run_id,kind,resource_id,logical_key,state,creation_id,admission_id,item_id,item_fingerprint) "
        "VALUES (:run,'creation',:creation,'creation','published',:creation,:admission,:item,:sha)",
        "INSERT INTO public.storage_connections (id,name,kind,config,scope) "
        "VALUES (:connection,:output_bucket,'s3','{}','global')",
        "INSERT INTO public.storage_locations (id,name,kind,config,connection_id) "
        "VALUES (:target,:output_bucket,'s3','{}',:connection)",
        "INSERT INTO yoao_assets_v1.fixture_native_cache_targets "
        "(run_id,endpoint,bucket,storage_identity,receipt_sha256,birth_receipt,"
        "connection_id,location_id,binding_generation) "
        "VALUES (:run,'http://fixture.invalid',:output_bucket,:sha,:sha,'sql-boundary',"
        ":connection,:target,2)",
    ):
        await execute(statement)

    restricted = os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    async with db_session.begin_nested():
        if restricted:
            await execute("SET LOCAL ROLE legacy_runtime")
            assert (
                await execute("SELECT current_user")
            ).scalar_one() == "legacy_runtime"
        await execute(
            "INSERT INTO public.video_derivative_outputs "
            "(id,dataset_item_id,kind,spec,original_ref,auth_ref,metadata_output_id,storage_ref) "
            "VALUES (:output,:item,'frame','{}',CAST(:source AS jsonb),CAST(:auth AS jsonb),:metadata,'{}')"
        )
        if restricted:
            await execute("RESET ROLE")

    async def probe(**scope):
        params = {"run": ids["run"], "suite": ids["suite"], "outputs": [ids["output"]]}
        return (
            await db_session.execute(
                native._VIDEO_REQUEST_PUBLICATIONS, {**params, **scope}
            )
        ).all()

    def evidence(status):
        return [(ids["output"], ids["lease"], ids["token"], status)]

    assert (
        await execute(
            "SELECT fixture_request_versions FROM public.video_derivative_outputs WHERE id=:output"
        )
    ).scalar_one() == {}
    delivery_matches = "SELECT yoao_jobs_v1.fixture_video_request_delivery_matches(:claimed_run,:output)"
    assert (
        await execute(delivery_matches, claimed_run=ids["run"])
    ).scalar_one() is True
    assert (
        await execute(delivery_matches, claimed_run=ids["suite"])
    ).scalar_one() is False
    await execute(
        "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended("
        "'aap-jobs:fixture-media:' || CAST(CAST(:output AS uuid) AS text),0))"
    )
    assert (
        await execute(
            "SELECT yoao_jobs_v1.begin_fixture_video_request_publication(:run,:output,:publication)"
        )
    ).scalar_one() is None
    receipt = (
        await execute(
            "SELECT yoao_jobs_v1.begin_fixture_media_publication(:output,:publication)"
        )
    ).scalar_one()
    assert receipt["state"] == "entered"
    assert receipt["delivery_id"] == str(ids["output"])
    assert receipt["envelope"] == {
        "schema": "aap-jobs-queue/v2",
        "operation": "dispatch",
        "task_name": "app.workers.media.generate_video_derivatives",
        "execution_id": str(ids["output"]),
        "args": [[str(ids["output"])]],
        "kwargs": {},
        "options": {"queue": f"aap_workspace_worker_{ids['run'].hex}_media"},
    }
    assert await probe() == []
    assert (
        await execute(
            "SELECT yoao_jobs_v1.finish_fixture_media_publication(:output,:publication,'accepted')"
        )
    ).scalar_one() is True
    assert await probe() == [(ids["output"], None, None, "queued")]

    key = (
        f"_aap_managed/media-cache/video-derivatives/objects/{ids['output']}/frame.webp"
    )
    publish = (
        "UPDATE public.video_derivative_outputs SET state='ready',result='{}',"
        "inventory=jsonb_build_object('schema',1,'objects',jsonb_build_object("
        "CAST(:derivative_key AS text),jsonb_build_object('sha256',CAST(:sha AS text),'size_bytes',12))) "
        "WHERE id=:output RETURNING fixture_request_versions"
    )
    versions = {key: {"etag": "etag", "version_id": "physical-version"}}

    async def stamp():
        await execute(
            "INSERT INTO yoao_assets_v1.fixture_media_versions "
            "(id,run_id,dataset_id,item_id,execution_kind,execution_id,execution_token,"
            "owner_kind,owner_id,object_key,key_digest,sha256,size_bytes,state,etag,version_id) "
            "VALUES (:version,:run,:dataset,:item,'independent',:output,:token,'video_output',"
            ":output,:derivative_key,pg_catalog.sha256(pg_catalog.convert_to(:derivative_key,'UTF8')),"
            ":sha,12,'verified','etag','physical-version')",
            derivative_key=key,
        )
        assert (await execute(publish, derivative_key=key)).scalar_one() == versions

    async def verify_suite_cutoff():
        runtime = _video_request_drain_runtime(ids["run"])
        runtime.suite_id = ids["suite"]
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(fixture_native_runtime=runtime))
        )
        await execute(
            "UPDATE yoao_fixture_v1.fixture_run_admissions SET state='settled',"
            "settled_at=now() WHERE id=:admission"
        )

        class CutoffReached(Exception):
            pass

        original_scalar = db_session.scalar
        published = False

        async def publish_after_first_pending_poll(statement, params=None, **kwargs):
            nonlocal published
            result = await original_scalar(statement, params, **kwargs)
            if statement is native._VIDEO_REQUEST_PENDING and result:
                assert result == [ids["output"]]
                assert await probe() == evidence("running")
                await stamp()
                published = True
            return result

        async def reach_retirement(db, owned, **_kwargs):
            assert db is db_session and owned.run_id == ids["run"]
            assert published
            rows = (
                await execute(
                    "SELECT id,state FROM yoao_fixture_v1.fixture_runs WHERE id IN (:suite,:run)"
                )
            ).all()
            assert dict(rows) == {ids["suite"]: "sealing", ids["run"]: "sealing"}
            raise CutoffReached

        monkeypatch.setattr(seed.settings, "e2e_seed_enabled", True)
        monkeypatch.setattr(db_session, "scalar", publish_after_first_pending_poll)
        monkeypatch.setattr(seed, "_retire_fixture_execution_work", reach_retirement)
        with pytest.raises(CutoffReached):
            await seed.seed_cleanup(
                seed.SeedSuiteCleanupRequest(suite_id=ids["suite"]), request, db_session
            )

    lock_key = {"key": "aap-fixture-independent-execution:" + str(ids["output"])}
    acquire = text(
        "SELECT pg_catalog.pg_advisory_lock(pg_catalog.hashtextextended(:key,0))"
    )
    release = text(
        "SELECT pg_catalog.pg_advisory_unlock(pg_catalog.hashtextextended(:key,0))"
    )
    async with test_engine.connect() as worker_connection:
        await worker_connection.execute(acquire, lock_key)
        try:
            worker_pid = await worker_connection.scalar(
                text("SELECT pg_catalog.pg_backend_pid()")
            )
            await execute(
                "INSERT INTO yoao_assets_v1.fixture_independent_media_executions "
                "(delivery_id,token,backend_pid,state) VALUES (:output,:token,:pid,'running')",
                pid=worker_pid,
            )
            assert await probe() == [(ids["output"], None, ids["token"], "running")]
            await execute(
                "UPDATE public.video_derivative_outputs SET lease_token=:lease,"
                "lease_expires_at=clock_timestamp()+interval '1 minute' WHERE id=:output"
            )
            assert await probe() == evidence("running")
            assert await worker_connection.scalar(release, lock_key) is True
            try:
                assert await probe() == evidence("unproven")
            finally:
                await worker_connection.execute(acquire, lock_key)
            await execute(
                "INSERT INTO yoao_assets_v1.fixture_independent_video_attempts "
                "(output_id,lease_token,delivery_id) VALUES (:output,:lease,:output)"
            )
            assert await probe() == evidence("running")
            assert await probe(suite=uuid.uuid4()) == []
            assert await probe(outputs=[ids["metadata"]]) == []
            if stage == "suite-cutoff":
                await verify_suite_cutoff()
        finally:
            assert await worker_connection.scalar(release, lock_key) is True
    if stage == "suite-cutoff":
        return
    assert await probe() == evidence("unlocked")

    with pytest.raises(DBAPIError, match="fixture_video_request_versions_unproven"):
        async with db_session.begin_nested():
            await execute(publish, derivative_key=key)
    await stamp()
    assert await probe() == evidence("published")


@pytest.mark.parametrize("acknowledged", [False, True])
async def test_scoped_publisher_never_settles_unknown_broker_ack(
    monkeypatch, acknowledged
):
    from yoao_jobs import publication_postgres
    from yoao_jobs.queue_contract import JobsQueueUnavailable

    run, delivery = uuid.uuid4(), uuid.uuid4()
    envelope = {
        "schema": "aap-jobs-queue/v2",
        "operation": "dispatch",
        "task_name": "app.workers.media.generate_video_derivatives",
        "execution_id": str(delivery),
        "args": [[str(delivery)]],
        "kwargs": {},
        "options": {"queue": f"aap_workspace_worker_{run.hex}_media"},
    }
    connection = SimpleNamespace(
        fetchval=AsyncMock(
            side_effect=[
                True,
                {
                    "state": "entered",
                    "delivery_id": str(delivery),
                    "payload_digest": "a" * 64,
                    "envelope": envelope,
                },
                True,
            ]
        )
    )

    @asynccontextmanager
    async def connect(_url):
        yield connection

    monkeypatch.setattr(publication_postgres, "_publication_connection", connect)
    publisher = SimpleNamespace(
        publish_envelope=AsyncMock(
            return_value=str(delivery),
            side_effect=None if acknowledged else JobsQueueUnavailable(),
        )
    )
    action = publication_postgres.publish_fixture_video_request(
        "scoped", run, delivery, publisher
    )
    if acknowledged:
        assert await action == str(delivery)
    else:
        with pytest.raises(JobsQueueUnavailable):
            await action
    publisher.publish_envelope.assert_awaited_once_with(envelope)
    lock, begin, *finish = [call.args for call in connection.fetchval.await_args_list]
    assert "pg_try_advisory_lock" in lock[0] and lock[1:] == (delivery,)
    assert "begin_fixture_video_request_publication" in begin[0]
    assert begin[1:3] == (run, delivery)
    if acknowledged:
        assert len(finish) == 1
        assert "finish_fixture_video_request_publication" in finish[0][0]
        assert finish[0][1:] == (run, delivery, begin[3], "accepted")
    else:
        assert not finish


@pytest.mark.parametrize("changed_claim", [False, True])
async def test_native_disposition_uses_owner_and_requires_exact_direct_seed_claim(
    monkeypatch, tmp_path, changed_claim
):
    from app.services import fixture_native_runtime as native

    run_id, dataset, output, seed_output = (uuid.uuid4() for _ in range(4))
    runtime = object.__new__(native.FixtureNativeRuntime)
    run = {
        "directory": tmp_path,
        "journal": {"state": "ready"},
        "owner_url": "owner",
        "control_url": "media-control",
        "ordinary_media_bucket": "cache",
        "target": {"storage_identity": "native"},
    }
    runtime.runs = {run_id: run}
    receipt = {
        "dataset_id": str(dataset),
        "item_id": str(uuid.uuid4()),
        "token": str(uuid.uuid4()),
        "original_ref": {"source": "exact"},
        "storage_ref": {"bucket": "cache", "storage_identity": "legacy"},
    }
    runtime.record_video_seed_claims(run_id, {str(seed_output): receipt})
    if changed_claim:
        observed = {**receipt, "token": str(uuid.uuid4())}
    else:
        observed = receipt

    connection = SimpleNamespace(
        scalar=AsyncMock(side_effect=["owned", "unknown", True])
    )

    @asynccontextmanager
    async def session():
        yield connection

    engine = SimpleNamespace(connect=session, begin=session, dispose=AsyncMock())
    create = MagicMock(return_value=engine)
    monkeypatch.setattr(native, "create_async_engine", create)
    outputs = [(dataset, output, {}), (dataset, seed_output, observed)]
    if changed_claim:
        with pytest.raises(RuntimeError, match="fixture_native_video_owner_unknown"):
            await runtime.classify_video_outputs(run_id, outputs)
        assert not runtime.video_scope_prepared(run_id)
    else:
        await runtime.classify_video_outputs(run_id, outputs)
        assert run["journal"]["video_outputs"] == [[str(dataset), str(output)]]
        assert run["journal"]["video_seed_outputs"] == {str(seed_output): receipt}
        await runtime._retire_video_anchors(run_id, run)
        statement, params = connection.scalar.await_args.args
        assert "retire_fixture_video_output_anchor" in str(statement)
        assert params == {"run": run_id, "dataset": dataset, "output": output}
    assert [call.args for call in create.call_args_list] == (
        [("owner",)] if changed_claim else [("owner",), ("owner",)]
    )


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
    output = SimpleNamespace(
        id=uuid.uuid4(),
        state="building",
        original_ref={"location_id": str(uuid.uuid4())},
        storage_ref={"location_id": str(uuid.uuid4())},
    )
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())
    monkeypatch.setattr(
        seed,
        "_fixture_video_outputs",
        AsyncMock(return_value=((uuid.uuid4(),), [output])),
    )
    monkeypatch.setattr(
        seed, "_drain_fixture_video_operations", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(seed, "_fixture_video_source_operations", AsyncMock())
    monkeypatch.setattr(seed, "_fixture_video_foreign_refs", AsyncMock())
    monkeypatch.setattr(objects, "snapshot", lambda value: value)
    monkeypatch.setattr(
        seed, "_fixture_video_output_in_use", AsyncMock(return_value=True)
    )
    with pytest.raises(HTTPException) as failure:
        await seed._prepare_fixture_video_cleanup(db, object())
    assert failure.value.detail == {"code": "e2e_seed_video_operation_unsettled"}
    assert output.state == "building"
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("settled", [True, False])
async def test_cleanup_drains_known_work_before_judging_live_permits(
    monkeypatch, settled
):
    """A permit the page still holds is drained, not reported as unknown owner."""
    location = uuid.uuid4()
    output = SimpleNamespace(
        id=uuid.uuid4(),
        state="ready",
        original_ref={"location_id": str(location)},
        storage_ref={"location_id": str(location)},
    )
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())
    monkeypatch.setattr(
        seed,
        "_fixture_video_outputs",
        AsyncMock(return_value=((uuid.uuid4(),), [output])),
    )
    drain = AsyncMock(return_value=settled)
    monkeypatch.setattr(seed, "_drain_fixture_video_operations", drain)
    monkeypatch.setattr(seed, "_fixture_video_source_operations", AsyncMock())
    monkeypatch.setattr(seed, "_fixture_video_foreign_refs", AsyncMock())
    monkeypatch.setattr(objects, "snapshot", lambda value: value)
    if settled:
        plan = await seed._prepare_fixture_video_cleanup(db, object())
        assert output.state == "retiring"
        assert plan.receipts == (output,)
        return
    with pytest.raises(HTTPException) as failure:
        await seed._prepare_fixture_video_cleanup(db, object())
    assert failure.value.detail == {"code": "e2e_seed_video_operation_unsettled"}
    assert output.state == "ready"
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
        original_ref={"location_id": str(uuid.uuid4())},
        storage_ref={"location_id": str(uuid.uuid4())},
    )
    plan = seed._FixtureVideoCleanup(scope, (), (receipt,))
    client, db = object(), SimpleNamespace(delete=AsyncMock())
    fresh = SimpleNamespace(
        scalar=AsyncMock(side_effect=["0", receipt, "0", receipt]),
        execute=AsyncMock(),
        scalars=AsyncMock(return_value=[SimpleNamespace(revision=7)]),
        commit=AsyncMock(),
    )

    @asynccontextmanager
    async def session():
        yield fresh

    @asynccontextmanager
    async def access(actual_db, actual_receipt, guard, *, on_acquired=None):
        assert (actual_db, actual_receipt) == (db, receipt)
        if on_acquired is not None:
            await on_acquired(SimpleNamespace(id=uuid.uuid4(), revision=7))
        await guard(7)
        yield client
        if close_fails:
            raise RuntimeError("owned close uncertain")
        await guard(7)

    monkeypatch.setattr(
        sqlalchemy_async, "async_sessionmaker", lambda *_a, **_k: session
    )
    monkeypatch.setattr(storage_access, "engine", lambda _db: object())
    drain = AsyncMock(return_value=True)
    monkeypatch.setattr(seed, "_drain_fixture_video_operations", drain)
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
    assert drain.await_count == (1 if close_fails else 2)
    assert drain.await_args.kwargs["excluding"]
    assert drain.await_args.kwargs["purposes"] == (objects.purpose(receipt.id),)


async def test_cleanup_keeps_saved_owner_when_known_work_outlives_the_drain(
    monkeypatch,
):
    import sqlalchemy.ext.asyncio as sqlalchemy_async

    scope = object()
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        state="retiring",
        original_ref={"location_id": str(uuid.uuid4())},
        storage_ref={"location_id": str(uuid.uuid4())},
    )
    plan = seed._FixtureVideoCleanup(scope, (), (receipt,))
    client, db = object(), SimpleNamespace(delete=AsyncMock())
    fresh = SimpleNamespace(
        scalar=AsyncMock(side_effect=["0", receipt, "0", receipt]),
        execute=AsyncMock(),
        scalars=AsyncMock(return_value=[SimpleNamespace(revision=7)]),
        commit=AsyncMock(),
    )

    @asynccontextmanager
    async def session():
        yield fresh

    @asynccontextmanager
    async def access(actual_db, actual_receipt, guard, *, on_acquired=None):
        await on_acquired(SimpleNamespace(id=uuid.uuid4(), revision=7))
        await guard(7)
        yield client

    monkeypatch.setattr(
        sqlalchemy_async, "async_sessionmaker", lambda *_a, **_k: session
    )
    monkeypatch.setattr(storage_access, "engine", lambda _db: object())
    monkeypatch.setattr(
        seed, "_drain_fixture_video_operations", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(objects, "snapshot", lambda value: value)
    monkeypatch.setattr(objects, "access", access)
    remove = MagicMock(return_value=2)
    monkeypatch.setattr(physical, "delete_output", remove)
    monkeypatch.setattr(
        originals,
        "physical",
        AsyncMock(side_effect=lambda call, *args, **_k: call(*args)),
    )
    with pytest.raises(HTTPException) as failure:
        await seed._delete_fixture_video_objects(
            db, plan, AsyncMock(return_value=scope)
        )
    assert failure.value.detail == {"code": "e2e_seed_video_operation_unsettled"}
    remove.assert_not_called()
    fresh.commit.assert_not_awaited()
    assert receipt.state == "retiring"
    assert plan.revisions == {}


async def test_fixture_video_drain_settles_known_work_and_reports_expiry():
    class Probe:
        def __init__(self, probes):
            self.probes = list(probes)

        @property
        def no_autoflush(self):
            return nullcontext()

        async def scalars(self, _statement):
            return self.probes.pop(0)

    deadline = asyncio.get_running_loop().time() + 1
    settled = await seed._drain_fixture_video_operations(
        Probe([[uuid.uuid4()], []]),
        deadline,
        locations={uuid.uuid4()},
        purposes=("video_derivative_" + uuid.uuid4().hex,),
    )
    assert settled is True
    expired = await seed._drain_fixture_video_operations(
        Probe([[uuid.uuid4()]]),
        0.0,
        locations={uuid.uuid4()},
    )
    assert expired is False
    assert await seed._drain_fixture_video_operations(Probe([]), deadline) is True, (
        "an empty scope has nothing to drain"
    )


@pytest.mark.parametrize("exact", [False, True])
async def test_finalize_adopts_only_idle_unbuilt_owners_allocated_during_cleanup(
    monkeypatch, exact
):
    scope = object()
    plan_identifier, extra_identifier = uuid.uuid4(), uuid.uuid4()
    receipt = SimpleNamespace(
        id=plan_identifier,
        state="retiring",
        storage_ref={"location_id": str(uuid.uuid4())},
    )
    planned = SimpleNamespace(
        id=plan_identifier,
        state="retiring",
        original_ref={"location_id": str(uuid.uuid4())},
        storage_ref={"location_id": str(uuid.uuid4())},
    )
    extra = SimpleNamespace(
        id=extra_identifier,
        state="building",
        lease_token=None,
        inventory=None,
        original_ref={"location_id": str(uuid.uuid4())},
        storage_ref={"location_id": str(uuid.uuid4())},
    )
    plan = seed._FixtureVideoCleanup(scope, (), (receipt,), exact=exact)
    db = SimpleNamespace(execute=AsyncMock(), flush=AsyncMock())
    monkeypatch.setattr(
        seed,
        "_fixture_video_outputs",
        AsyncMock(return_value=((), [planned, extra])),
    )
    monkeypatch.setattr(seed, "_fixture_video_foreign_refs", AsyncMock())
    monkeypatch.setattr(seed, "_fixture_video_source_operations", AsyncMock())
    monkeypatch.setattr(
        seed, "_drain_fixture_video_operations", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        objects,
        "snapshot",
        lambda value: receipt if value is planned else value,
    )
    monkeypatch.setattr(
        seed, "_fixture_video_output_in_use", AsyncMock(return_value=False)
    )
    resolve = AsyncMock(return_value=scope)
    if exact:
        with pytest.raises(HTTPException) as failure:
            await seed._finalize_fixture_video_cleanup(db, plan, resolve)
        assert failure.value.detail == {"code": "e2e_seed_video_owner_changed"}
        assert extra.state == "building"
        db.execute.assert_not_awaited()
    else:
        await seed._finalize_fixture_video_cleanup(db, plan, resolve)
        assert extra.state == "retiring"


async def test_finalize_keeps_published_late_owner_as_unknown_ownership(monkeypatch):
    scope = object()
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        state="retiring",
        original_ref={"location_id": str(uuid.uuid4())},
        storage_ref={"location_id": str(uuid.uuid4())},
    )
    extra = SimpleNamespace(
        id=uuid.uuid4(),
        state="ready",
        lease_token=None,
        inventory={"schema": 1},
        original_ref={"location_id": str(uuid.uuid4())},
        storage_ref={"location_id": str(uuid.uuid4())},
    )
    plan = seed._FixtureVideoCleanup(scope, (), (receipt,))
    db = SimpleNamespace(execute=AsyncMock(), flush=AsyncMock())
    monkeypatch.setattr(
        seed,
        "_fixture_video_outputs",
        AsyncMock(return_value=((), [receipt, extra])),
    )
    monkeypatch.setattr(objects, "snapshot", lambda value: value)
    monkeypatch.setattr(
        seed, "_fixture_video_output_in_use", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        seed, "_drain_fixture_video_operations", AsyncMock(return_value=True)
    )
    with pytest.raises(HTTPException) as failure:
        await seed._finalize_fixture_video_cleanup(
            db, plan, AsyncMock(return_value=scope)
        )
    assert failure.value.detail == {"code": "e2e_seed_video_owner_changed"}
    assert extra.state == "ready"
    db.execute.assert_not_awaited()
