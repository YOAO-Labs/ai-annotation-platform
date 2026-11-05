"""Real seed HTTP/database flows retain attachment destruction responsibility."""

import asyncio
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import io
import threading
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import _test_seed as seed, _test_seed_attachments as cleanup
from app.db.models.annotation import Annotation
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.comment_attachment_upload import CommentAttachmentUpload as Upload
from app.db.models.project import Project
from app.db.models.storage_maintenance import StorageOperation
from tests.identity_e2e_support import (
    committed_owner as committed_owner,
    fenced_http as fenced_http,
    fixture_and_fenced_http as fixture_and_fenced_http,
    owner_fault_http as owner_fault_http,
)
from app.services import comment_attachment_storage as storage
from app.services import comment_attachment_uploads as uploads, storage_access
from app.services import storage as storage_module
from tests._owned_s3 import patch_owned_client
from tests.seed_attachment_support import Objects, build, upload
from tests.seed_owner_support import (
    _fixture_versions,
    _read_versions,
    fixture_original_reader,
)


@pytest.fixture
async def context(
    test_engine,
    monkeypatch,
    committed_owner,
    fenced_http,
    fixture_and_fenced_http,
    owner_fault_http,
):
    """Use private fixture HTTP and restricted product HTTP with fake attachments."""
    db = committed_owner
    objects = Objects(storage_module.storage_service)
    patch_owned_client(monkeypatch, objects.client)
    monkeypatch.setattr(storage_module, "storage_service", objects)
    monkeypatch.setattr(storage, "storage_service", objects)
    monkeypatch.setattr(uploads, "storage_service", objects)
    monkeypatch.setattr(cleanup, "storage_service", objects)
    value = SimpleNamespace(
        db=db,
        http=fenced_http,
        owner_http=owner_fault_http,
        suite=fixture_and_fenced_http.owner_suite,
        objects=objects,
        runs=[],
        operations=set(),
        engine=test_engine,
    )
    try:
        yield value
    finally:
        objects.probe = None
        await db.rollback()
        assert (
            await db.scalar(
                select(StorageOperation.id)
                .where(StorageOperation.id.in_(value.operations))
                .limit(1)
            )
            is None
        )
        await db.rollback()
        for run in reversed(value.runs):
            response = await value.owner_http.post(
                "/api/v1/__test/seed/owned-cleanup", json=run.payload()
            )
            assert response.status_code == 200, response.text


async def clean(ctx, world):
    return await ctx.owner_http.post(
        "/api/v1/__test/seed/owned-cleanup", json=world["run"].payload()
    )


async def present(ctx, row):
    # A failed ASGI request rolls back this session and expires even primary keys.
    current = await ctx.db.get(Upload, inspect(row).identity[0], populate_existing=True)
    await ctx.db.commit()
    return current


@pytest.mark.parametrize(
    "kind", ["bound", "unbound", "retired", "empty", "missing_children"]
)
async def test_terminal_owner_is_explicitly_destroyed_before_fixture_and_rebuild(
    context, kind
):
    ctx = context
    a, b = await build(ctx), await build(ctx)
    raw = b"" if kind == "empty" else b"generic\x00bytes"
    row, _, comment_id = await upload(ctx, a, raw, bind=kind != "unbound")
    neighbour, _, _ = await upload(ctx, b)
    if kind == "retired":
        result = await ctx.http.delete(
            f"/api/v1/comments/{comment_id}", headers=a["headers"]
        )
        assert result.status_code == 204
    if kind == "missing_children":
        from app.db.models.annotation_feedback import AnnotationFeedback

        await ctx.db.execute(
            delete(AnnotationFeedback).where(
                AnnotationFeedback.annotation_id == row.annotation_id
            )
        )
        await ctx.db.execute(
            delete(AnnotationComment).where(
                AnnotationComment.annotation_id == row.annotation_id
            )
        )
        await ctx.db.execute(
            delete(Annotation).where(Annotation.id == row.annotation_id)
        )
        await ctx.db.commit()
    saved = dict(row.storage_ref)
    assert (await clean(ctx, a)).status_code == 200
    assert await present(ctx, row) is None
    assert (saved["bucket"], saved["object_key"]) not in ctx.objects.data
    assert (await present(ctx, neighbour)).state == "ready"
    assert (
        ctx.objects.data[
            neighbour.storage_ref["bucket"], neighbour.storage_ref["object_key"]
        ]
        == b"generic\x00bytes"
    )
    assert (await clean(ctx, a)).status_code == 200
    rebuilt_run = ctx.suite.new_run(a["namespace"])
    rebuilt = await ctx.suite.owner.post(
        "/api/v1/__test/seed/owned", json=rebuilt_run.payload()
    )
    assert rebuilt.status_code == 200, rebuilt.text
    ctx.runs.append(rebuilt_run)


async def test_uploader_in_another_scope_is_not_selected(context):
    a, b = await build(context), await build(context)
    own, _, _ = await upload(context, a)
    foreign, _, _ = await upload(context, b, bind=False, actor=a)
    assert foreign.owner_id == own.owner_id
    assert (await clean(context, a)).status_code == 200
    assert (await present(context, foreign)).state == "ready"
    assert context.objects.data[
        foreign.storage_ref["bucket"], foreign.storage_ref["object_key"]
    ]


async def test_bound_foreign_comment_keeps_original_namespace_as_retry_anchor(context):
    ctx = context
    # The fixture teardown also cleans B before A when an assertion fails.
    a, b = await build(ctx), await build(ctx)
    own, _, own_comment = await upload(ctx, a)
    foreign, attachment, foreign_comment = await upload(ctx, b, actor=a)
    own_receipt, foreign_receipt = storage.snapshot(own), storage.snapshot(foreign)
    foreign_ref = foreign_receipt.storage_ref

    response = await clean(ctx, a)
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["code"] == "e2e_seed_cleanup_incomplete"
    assert detail["residuals"]["users"] > 0
    retained = await present(ctx, own)
    assert retained.state == "retired" and storage.same(retained, own_receipt)
    assert await ctx.db.get(Project, uuid.UUID(a["project_id"])) is not None
    assert await ctx.db.get(AnnotationComment, uuid.UUID(own_comment)) is not None
    current = await present(ctx, foreign)
    assert current.state == "ready" and storage.same(current, foreign_receipt)
    original = await ctx.db.get(AnnotationComment, uuid.UUID(foreign_comment))
    assert original.attachments == [attachment]
    assert (
        ctx.objects.data[foreign_ref["bucket"], foreign_ref["object_key"]]
        == b"generic\x00bytes"
    )
    assert await ctx.db.get(Project, uuid.UUID(b["project_id"])) is not None

    assert (await clean(ctx, b)).status_code == 200
    assert (await clean(ctx, a)).status_code == 200


@pytest.mark.parametrize("has_bytes", [False, True])
async def test_any_pending_refuses_with_zero_physical_delete_and_keeps_ready(
    context, has_bytes
):
    ctx = context
    world = await build(ctx)
    ready, _, _ = await upload(ctx, world)
    created = datetime.now(timezone.utc) - timedelta(days=200)
    pending = Upload(
        id=uuid.uuid4(),
        owner_id=ready.owner_id,
        annotation_id=ready.annotation_id,
        task_id=ready.task_id,
        project_id=ready.project_id,
        sha256=hashlib.sha256(b"pending").hexdigest(),
        size_bytes=7,
        mime_type="application/octet-stream",
        file_name="pending.dat",
        created_at=created,
        expires_at=created + timedelta(days=90),
        state="pending",
    )
    pending.storage_ref = {
        **ready.storage_ref,
        "object_key": storage.key_for(pending.annotation_id, pending.id),
    }
    ctx.db.add(pending)
    await ctx.db.commit()
    if has_bytes:
        ctx.objects.data[
            pending.storage_ref["bucket"], pending.storage_ref["object_key"]
        ] = b"pending"
    before = dict(ctx.objects.data)
    response = await clean(ctx, world)
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "e2e_seed_attachment_pending"
    )
    assert ctx.objects.data == before and not ctx.objects.deleted
    assert (await present(ctx, ready)).state == "ready"
    assert await ctx.db.get(Project, uuid.UUID(world["project_id"])) is not None
    # This pending row and its optional bytes were injected by this test.
    await ctx.db.delete(pending)
    await ctx.db.commit()
    ctx.objects.data.pop(
        (pending.storage_ref["bucket"], pending.storage_ref["object_key"]), None
    )
    assert (await clean(ctx, world)).status_code == 200


@pytest.mark.parametrize("purpose", ["read", "write", "delete", "seed_delete"])
async def test_durable_operation_blocks_fixture_destroy_then_exact_settlement_allows_retry(
    context, purpose
):
    ctx = context
    world = await build(ctx)
    row, _, _ = await upload(ctx, world)
    label = "comment_attachment_" + purpose + "_" + row.id.hex
    permit = await storage_access.acquire(
        ctx.db, uuid.UUID(row.storage_ref["location_id"]), label
    )
    ctx.operations.add(permit.id)
    try:
        response = await clean(ctx, world)
        assert response.status_code == 409
        assert not ctx.objects.deleted and (await present(ctx, row)).state == "ready"
    finally:
        await permit.__aexit__(None, None, None)
    assert (await clean(ctx, world)).status_code == 200


async def test_saved_bucket_trap_and_unknown_canonical_are_never_prefix_deleted(
    context, monkeypatch
):
    ctx = context
    world = await build(ctx)
    row, value, _ = await upload(ctx, world)
    saved = dict(row.storage_ref)
    from app.services import annotations_storage

    current_bucket = ctx.objects.export_bucket
    monkeypatch.setattr(annotations_storage.storage_service, "bucket", current_bucket)
    monkeypatch.setattr(ctx.objects, "bucket", current_bucket)
    ctx.objects.data[current_bucket, saved["object_key"]] = b"current bucket trap"
    response = await clean(ctx, world)
    assert response.status_code == 409
    assert (
        ctx.objects.data[current_bucket, saved["object_key"]] == b"current bucket trap"
    )
    assert (saved["bucket"], saved["object_key"]) not in ctx.objects.data
    assert (await present(ctx, row)).state == "retired"
    unknown = f"comment-attachments/{world['annotation_id']}/{uuid.uuid4()}"
    ctx.objects.data[current_bucket, unknown] = b"unknown canonical"
    del ctx.objects.data[current_bucket, saved["object_key"]]
    assert (await clean(ctx, world)).status_code == 409
    assert ctx.objects.data[current_bucket, unknown] == b"unknown canonical"
    del ctx.objects.data[current_bucket, unknown]
    assert (await clean(ctx, world)).status_code == 200


async def test_delete_failure_keeps_owner_original_json_and_retry_anchor(context):
    ctx = context
    world = await build(ctx)
    row, value, comment_id = await upload(ctx, world)
    before = storage.snapshot(row)
    ctx.objects.fail_delete = True
    response = await clean(ctx, world)
    assert response.status_code == 503
    current = await present(ctx, row)
    assert current.state == "retired" and storage.same(current, before)
    original = await ctx.db.get(AnnotationComment, uuid.UUID(comment_id))
    assert original.attachments == [value]
    assert await ctx.db.get(Project, uuid.UUID(world["project_id"])) is not None
    ctx.objects.fail_delete = False
    assert (await clean(ctx, world)).status_code == 200


@pytest.mark.parametrize("state", ["pending", "ready"])
async def test_new_owner_during_network_rejects_final_scope_instead_of_escaping(
    context, monkeypatch, state
):
    ctx = context
    world = await build(ctx)
    row, _, _ = await upload(ctx, world)
    receipt = storage.snapshot(row)
    real_delete_objects = cleanup.delete_objects
    injected_ids = []

    async def inject():
        created = datetime.now(timezone.utc)
        async with async_sessionmaker(ctx.engine, expire_on_commit=False)() as other:
            new = Upload(
                id=uuid.uuid4(),
                owner_id=receipt.owner_id,
                annotation_id=receipt.annotation_id,
                task_id=receipt.task_id,
                project_id=receipt.project_id,
                sha256=hashlib.sha256(b"").hexdigest(),
                size_bytes=0,
                mime_type="application/octet-stream",
                file_name="late.dat",
                state=state,
                created_at=created,
                expires_at=created + timedelta(days=90),
            )
            new.storage_ref = {
                **receipt.storage_ref,
                "object_key": storage.key_for(new.annotation_id, new.id),
            }
            other.add(new)
            injected_ids.append(new.id)
            await other.commit()

    async def delete_then_inject(*args):
        await real_delete_objects(*args)
        await inject()

    with monkeypatch.context() as patch:
        patch.setattr(cleanup, "delete_objects", delete_then_inject)
        response = await clean(ctx, world)
    assert response.status_code == 409
    assert (await present(ctx, row)).state == "retired"
    assert await ctx.db.get(Project, uuid.UUID(world["project_id"])) is not None
    assert len(injected_ids) == 1
    late = await ctx.db.get(Upload, injected_ids[0])
    await ctx.db.delete(late)
    await ctx.db.commit()
    assert (await clean(ctx, world)).status_code == 200


async def test_neighbour_read_permit_does_not_block_exact_owner_cleanup(context):
    ctx = context
    a, b = await build(ctx), await build(ctx)
    await upload(ctx, a)
    neighbour, _, _ = await upload(ctx, b)
    permit = await storage_access.acquire(
        ctx.db,
        uuid.UUID(neighbour.storage_ref["location_id"]),
        uploads.read_purpose(neighbour.id),
    )
    ctx.operations.add(permit.id)
    try:
        assert (await clean(ctx, a)).status_code == 200
        assert (await present(ctx, neighbour)).state == "ready"
    finally:
        await permit.__aexit__(None, None, None)


async def test_maintenance_refusal_preserves_retired_owner_and_fixture(context):
    from app.db.models.storage_maintenance import StorageMaintenanceGate

    ctx = context
    world = await build(ctx)
    row, _, _ = await upload(ctx, world)
    location_id = uuid.UUID(row.storage_ref["location_id"])
    gate = await ctx.db.get(StorageMaintenanceGate, location_id)
    gate.state, gate.revision = "stopped", gate.revision + 1
    await ctx.db.commit()
    try:
        response = await clean(ctx, world)
        assert response.status_code == 409 and not ctx.objects.deleted
        assert (await present(ctx, row)).state == "retired"
        assert await ctx.db.get(Project, uuid.UUID(world["project_id"])) is not None
    finally:
        await ctx.db.rollback()
        gate = await ctx.db.get(StorageMaintenanceGate, location_id)
        gate.state, gate.revision = "active", gate.revision + 1
        await ctx.db.commit()
    assert (await clean(ctx, world)).status_code == 200


async def test_network_and_rebuild_never_hold_cleanup_business_locks(
    context, monkeypatch
):
    ctx = context
    world = await build(ctx)
    await upload(ctx, world)
    loop = asyncio.get_running_loop()
    probes = []
    scope = await seed._resolve_fixture_scope(ctx.db, world["run"].scope())
    await ctx.db.commit()

    async def observe():
        from sqlalchemy import text

        async with ctx.engine.begin() as connection:
            for table, ids in (
                ("users", scope.user_ids),
                ("projects", scope.project_ids),
            ):
                await connection.execute(
                    text(
                        f"SELECT id FROM {table} WHERE id = ANY(:ids) FOR UPDATE NOWAIT"
                    ),
                    {"ids": list(ids)},
                )
            for table in ("annotation_comments", "comment_attachment_uploads"):
                await connection.execute(
                    text(
                        f"SELECT id FROM {table} WHERE project_id = :pid FOR UPDATE NOWAIT"
                    ),
                    {"pid": world["project_id"]},
                )
        probes.append(True)

    def probe():
        assert not ctx.db.in_transaction()
        asyncio.run_coroutine_threadsafe(observe(), loop).result(timeout=10)

    ctx.objects.probe = probe
    assert (await clean(ctx, world)).status_code == 200
    ctx.objects.probe = None
    real_builder = seed._build_image_workbench_fixture

    async def builder(db, ids):
        assert not db.in_transaction()
        return await real_builder(db, ids)

    monkeypatch.setattr(seed, "_build_image_workbench_fixture", builder)
    rebuilt_run = ctx.suite.new_run(world["namespace"])
    result = await ctx.owner_http.post(
        "/api/v1/__test/seed/owned", json=rebuilt_run.payload()
    )
    assert result.status_code == 200, result.text
    ctx.runs.append(rebuilt_run)
    assert probes


async def wait_for_blocked_fence(ctx, holder_pid, timeout=20):
    """Wait until a cleanup fence is queued behind another session's row lock."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with ctx.engine.connect() as connection:
        while loop.time() < deadline:
            blocked = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE NOT granted "
                    "AND :holder=ANY(pg_blocking_pids(pid))"
                ),
                {"holder": holder_pid},
            )
            if blocked:
                return
            await asyncio.sleep(0.02)
    raise AssertionError("the cleanup scope fence never blocked")


async def hold_account_share_lock(ctx, user_id, ready, release):
    """Act like a live fixture request: FOR SHARE held for the whole request."""
    async with ctx.engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text("SELECT id FROM users WHERE id = :id FOR SHARE"), {"id": user_id}
        )
        ready.set_result(await connection.scalar(text("SELECT pg_backend_pid()")))
        try:
            await asyncio.wait_for(release.wait(), 20)
        finally:
            await transaction.commit()


async def hold_project_share_lock(ctx, project_id, ready, release):
    async with ctx.engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text("SELECT id FROM projects WHERE id = :id FOR SHARE"),
            {"id": project_id},
        )
        ready.set_result(await connection.scalar(text("SELECT pg_backend_pid()")))
        try:
            await asyncio.wait_for(release.wait(), 20)
        finally:
            await transaction.commit()


@pytest.mark.parametrize("lock_owner", ["account", "project"])
async def test_live_fixture_request_is_awaited_by_the_scope_fence(context, lock_owner):
    """The preflight must drain either fixture authority lock before NOWAIT."""
    ctx = context
    world = await build(ctx)
    row, _, _ = await upload(ctx, world)
    receipt = storage.snapshot(row)
    scope = await seed._resolve_fixture_scope(ctx.db, world["run"].scope())
    await ctx.db.commit()

    ready, release = asyncio.get_running_loop().create_future(), asyncio.Event()
    holder = asyncio.create_task(
        hold_account_share_lock(ctx, scope.user_ids[0], ready, release)
        if lock_owner == "account"
        else hold_project_share_lock(ctx, scope.project_ids[0], ready, release)
    )
    try:
        holder_pid = await asyncio.wait_for(ready, 20)
        cleanup = asyncio.create_task(clean(ctx, world))
        await wait_for_blocked_fence(ctx, holder_pid)
        assert not cleanup.done()
        release.set()
        response = await asyncio.wait_for(cleanup, 30)
    finally:
        release.set()
        await holder

    assert response.status_code == 200, response.text
    assert await present(ctx, row) is None
    assert (
        receipt.storage_ref["bucket"],
        receipt.storage_ref["object_key"],
    ) not in ctx.objects.data
    assert await ctx.db.get(Project, uuid.UUID(world["project_id"])) is None


async def test_stuck_scope_fence_still_fails_closed_after_bounded_wait(
    context, monkeypatch
):
    """A holder that outlives the bound keeps the fail-closed busy rejection."""
    ctx = context
    world = await build(ctx)
    row, _, _ = await upload(ctx, world)
    receipt = storage.snapshot(row)
    scope = await seed._resolve_fixture_scope(ctx.db, world["run"].scope())
    await ctx.db.commit()
    monkeypatch.setattr(cleanup, "FENCE_LOCK_TIMEOUT", "200ms")

    ready, release = asyncio.get_running_loop().create_future(), asyncio.Event()
    holder = asyncio.create_task(
        hold_account_share_lock(ctx, scope.user_ids[0], ready, release)
    )
    try:
        await asyncio.wait_for(ready, 20)
        response = await asyncio.wait_for(clean(ctx, world), 30)
    finally:
        release.set()
        await holder

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "e2e_seed_attachment_cleanup_busy"
    retained = await present(ctx, row)
    assert retained.state == "ready" and storage.same(retained, receipt)
    assert await ctx.db.get(Project, uuid.UUID(world["project_id"])) is not None
    assert (await clean(ctx, world)).status_code == 200


async def test_repeated_cancellation_drains_exact_original_reclaim_before_rebuild(
    context, monkeypatch
):
    from botocore.exceptions import ClientError

    from app.db.models.fixture_run import FixtureRun
    from app.db.models.managed_original_reclaim import ManagedOriginalReclaimAttempt
    from app.services import managed_original_reclaim_io

    ctx = context
    world, neighbour = await build(ctx), await build(ctx)
    await upload(ctx, world)
    own_versions = await _fixture_versions(ctx.engine, world["run"])
    neighbour_versions = await _fixture_versions(ctx.engine, neighbour["run"])
    with closing(fixture_original_reader()) as reader:
        before = _read_versions(reader, neighbour_versions)
    by_locator = {
        (row["bucket"], row["object_key"], row["version_id"]): row
        for row in own_versions
    }
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    selected = {}
    real_reclaim = managed_original_reclaim_io.run_reclaim

    def controlled_reclaim(candidate, *, checkpoint, client_context=None):
        assert client_context is not None

        @contextmanager
        def controlled_client():
            with client_context() as client:

                class Client:
                    def __getattr__(self, name):
                        return getattr(client, name)

                    def delete_object(self, **kwargs):
                        locator = (kwargs["Bucket"], kwargs["Key"], kwargs["VersionId"])
                        assert locator in by_locator
                        selected["row"] = by_locator[locator]
                        started.set()
                        assert release.wait(10)
                        try:
                            return client.delete_object(**kwargs)
                        finally:
                            finished.set()

                yield Client()

        return real_reclaim(
            candidate, checkpoint=checkpoint, client_context=controlled_client
        )

    with monkeypatch.context() as patch:
        patch.setattr(managed_original_reclaim_io, "run_reclaim", controlled_reclaim)
        task = asyncio.create_task(clean(ctx, world))
        try:
            assert await asyncio.to_thread(started.wait, 10)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            _, pending = await asyncio.wait({task}, timeout=1)
            assert task in pending and not finished.is_set()
            async with async_sessionmaker(ctx.engine)() as observer:
                run = await observer.get(FixtureRun, world["run"].run_id)
                assert run.state == "sealing"
                attempt = await observer.scalar(
                    select(ManagedOriginalReclaimAttempt).where(
                        ManagedOriginalReclaimAttempt.creation_id
                        == selected["row"]["id"]
                    )
                )
                assert (
                    attempt.status == "running" and attempt.delete_intent_at is not None
                )
                assert attempt.operation_id is not None
                assert (
                    await observer.get(StorageOperation, attempt.operation_id)
                    is not None
                )
            with closing(fixture_original_reader()) as reader:
                _read_versions(reader, [selected["row"]])
                assert _read_versions(reader, neighbour_versions) == before
        finally:
            release.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert finished.is_set()

    with closing(fixture_original_reader()) as reader:
        row = selected["row"]
        with pytest.raises(ClientError) as removed:
            reader.head_object(
                Bucket=row["bucket"], Key=row["object_key"], VersionId=row["version_id"]
            )
        assert removed.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
        assert _read_versions(reader, neighbour_versions) == before
    assert (await clean(ctx, world)).status_code == 200
    rebuilt_run = ctx.suite.new_run(world["namespace"])
    rebuilt = await ctx.suite.owner.post(
        "/api/v1/__test/seed/owned", json=rebuilt_run.payload()
    )
    assert rebuilt.status_code == 200, rebuilt.text
    ctx.runs.append(rebuilt_run)
    rebuilt_versions = await _fixture_versions(ctx.engine, rebuilt_run)
    assert {row["object_key"] for row in own_versions}.isdisjoint(
        row["object_key"] for row in rebuilt_versions
    )
    with closing(fixture_original_reader()) as reader:
        _read_versions(reader, rebuilt_versions)
        assert _read_versions(reader, neighbour_versions) == before


async def test_real_open_body_and_unknown_close_keep_durable_read_responsibility(
    context,
):
    ctx = context
    world = await build(ctx)
    row, _, _ = await upload(ctx, world)
    receipt = storage.snapshot(row)
    location_id = uuid.UUID(receipt.storage_ref["location_id"])
    opened, release = threading.Event(), threading.Event()

    class Body(io.BytesIO):
        def read(self, size=-1):
            opened.set()
            assert release.wait(10)
            return super().read(size)

        def close(self):
            raise OSError("unknown physical close")

    ctx.objects.body_factory = Body
    purpose = uploads.read_purpose(receipt.id)
    task = asyncio.create_task(
        storage_access.run(
            ctx.db,
            location_id,
            purpose,
            storage.read,
            ctx.objects.client,
            receipt,
        )
    )
    try:
        assert await asyncio.to_thread(opened.wait, 10)
        assert (await clean(ctx, world)).status_code == 409
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done() and not ctx.objects.deleted
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # First assert uncertainty remains. Cleanup cannot infer a closed body.
        assert (await clean(ctx, world)).status_code == 409
        for body in ctx.objects.bodies:
            io.BytesIO.close(body)
        async with ctx.engine.begin() as connection:
            identifiers = list(
                await connection.scalars(
                    select(StorageOperation.id).where(
                        StorageOperation.location_id == location_id,
                        StorageOperation.purpose == purpose,
                    )
                )
            )
            assert identifiers
            ctx.operations.update(identifiers)
            await connection.execute(
                delete(StorageOperation).where(StorageOperation.id.in_(identifiers))
            )
    assert (await clean(ctx, world)).status_code == 200
