"""Disposable native ZIP/scan ownership, transaction and replay qualification."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_assets_source.ingest_contract import (
    SCAN_REQUEST_DIGEST,
    IngestBatchPlan,
    IngestBatchResult,
    IngestCommand,
    IngestContext,
    IngestDescriptor,
    IngestEntryCompletion,
    IngestFailure,
    IngestFinish,
    IngestLayoutPage,
    IngestReceipt,
    IngestScenePlan,
    IngestSourceEntry,
    decode_ingest_reply,
)
from aap_assets_source.frame_layout import PreparedDatasetItem
from aap_assets_source.scene_layout import SceneLayoutInput, plan_scenes
from tests.test_dataset_upload_native import _cleanup_world, _seed_world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned disposable module-data verifier",
    ),
]


@dataclass
class _World:
    base: object
    source_maker: object
    operation_ids: set[UUID] = field(default_factory=set)
    batch_ids: set[UUID] = field(default_factory=set)

    @property
    def maker(self):
        return self.base.maker

    @property
    def actor_id(self):
        return self.base.actor_id

    @property
    def dataset_id(self):
        return self.base.dataset_id

    def command(self, actor=None):
        value = IngestCommand(
            operation_id=uuid4(),
            actor_id=actor or self.actor_id,
            dataset_id=self.dataset_id,
        )
        self.operation_ids.add(value.operation_id)
        return value


@pytest_asyncio.fixture
async def ingest_world(test_engine):
    raw = os.environ["AAP_MODULE_DATA_ASSET_SOURCE_URL"]
    url = make_url(raw)
    assert url.username == "asset_source_client"
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert url.database.startswith("aap_wt_") and url.database.endswith("_test")
    engine = create_async_engine(url, hide_parameters=True)
    world = _World(
        await _seed_world(test_engine),
        async_sessionmaker(engine, expire_on_commit=False),
    )
    try:
        yield world
    finally:
        async with world.maker.begin() as db:
            operations = list(world.operation_ids)
            for table in (
                "dataset_source_ingest_deliveries",
                "dataset_source_ingest_entries",
                "dataset_source_ingest_operations",
            ):
                await db.execute(
                    text(
                        "DELETE FROM public."
                        + table
                        + " WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": operations},
                )
            if world.batch_ids:
                await db.execute(
                    text(
                        "DELETE FROM public.task_append_operations WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": list(world.batch_ids)},
                )
        await _cleanup_world(world.base)
        await engine.dispose()


def _descriptor(kind="zip", count=1):
    return IngestDescriptor(
        kind=kind,
        request_digest=SCAN_REQUEST_DIGEST if kind == "scan" else "a" * 64,
        total_in_zip=count if kind == "zip" else None,
        planned_entries=count if kind == "zip" else None,
    )


def _entry(path="frames/a.png", digest="a" * 32, kind="image", key=None):
    return IngestSourceEntry(
        source_name=path,
        relpath=path,
        source_key=key,
        file_type=kind,
        content_type="image/png" if kind == "image" else "application/octet-stream",
        file_size=3,
        content_hash=digest,
        width=2 if kind == "image" else None,
        height=2 if kind == "image" else None,
    )


async def _invoke(
    db,
    command,
    function,
    result_type,
    *,
    batch=None,
    payload=None,
    after=None,
    limit=None,
):
    arguments = [
        "CAST(:operation AS uuid)",
        "CAST(:actor AS uuid)",
        "CAST(:dataset AS uuid)",
    ]
    parameters = {
        "operation": command.operation_id,
        "actor": command.actor_id,
        "dataset": command.dataset_id,
    }
    if batch is not None:
        arguments.append("CAST(:batch AS uuid)")
        parameters["batch"] = batch
    if function == "read_dataset_ingest_layout":
        arguments.extend(["CAST(:after AS uuid)", "CAST(:limit AS integer)"])
        parameters.update(after=after, limit=limit or 500)
    else:
        arguments.append("CAST(:payload AS jsonb)")
        if isinstance(payload, list):
            value = [item.model_dump(mode="json") for item in payload]
        else:
            value = payload.model_dump(mode="json")
        parameters["payload"] = json.dumps(value)
    reply = await db.scalar(
        text("SELECT aap_assets_v1." + function + "(" + ",".join(arguments) + ")"),
        parameters,
    )
    result = decode_ingest_reply(reply, result_type)
    assert result is not None
    return result


async def _begin(db, command, descriptor=None):
    return await _invoke(
        db,
        command,
        "begin_dataset_ingest",
        IngestContext,
        payload=descriptor or _descriptor(),
    )


async def _prepare(db, world, command, entries):
    batch = uuid4()
    world.batch_ids.add(batch)
    return await _invoke(
        db,
        command,
        "prepare_dataset_ingest_batch",
        IngestBatchPlan,
        batch=batch,
        payload=entries,
    )


async def _complete(db, command, plan):
    completions = [
        IngestEntryCompletion(
            entry_id=entry.entry_id,
            status="ready",
            publication_id=entry.publication_id,
            file_size=entry.file_size,
            content_hash=entry.content_hash,
            width=entry.width,
            height=entry.height,
            error_code=None,
        )
        for entry in plan.entries
        if entry.status == "pending"
    ]
    return await _invoke(
        db,
        command,
        "complete_dataset_ingest_batch",
        IngestBatchResult,
        batch=plan.batch_id,
        payload=completions,
    )


async def _finish(db, command, *, infer=True):
    finish = IngestFinish(layout_snapshot_digest=None, scene_plan=None)
    if infer:
        first = await _invoke(
            db,
            command,
            "read_dataset_ingest_layout",
            IngestLayoutPage,
            after=None,
            limit=2,
        )
        pages = [first]
        while pages[-1].next_item_id is not None:
            pages.append(
                await _invoke(
                    db,
                    command,
                    "read_dataset_ingest_layout",
                    IngestLayoutPage,
                    after=pages[-1].next_item_id,
                    limit=2,
                )
            )
        assert all(page.snapshot_digest == first.snapshot_digest for page in pages)
        items = tuple(
            PreparedDatasetItem(
                id=item.id,
                file_name=item.file_name,
                file_path=item.file_path,
                file_type=item.file_type,
                scene_id=item.scene_id,
                frame_index=item.frame_index,
                logical_path=item.logical_path,
            )
            for page in pages
            for item in page.items
        )
        layout = plan_scenes(
            SceneLayoutInput(
                dataset_name="unused-for-explicit-layouts",
                items=items,
                scene_count=first.scene_count,
                assigned_count=first.assigned_count,
                item_count=first.total_items,
            )
        )
        from dataclasses import asdict

        finish = IngestFinish(
            layout_snapshot_digest=first.snapshot_digest,
            scene_plan=IngestScenePlan.model_validate(asdict(layout)),
        )
    return await _invoke(
        db, command, "finish_dataset_ingest", IngestReceipt, payload=finish
    )


async def _counts(world):
    async with world.maker() as db:
        return (
            await db.scalar(
                text("SELECT count(*) FROM dataset_items WHERE dataset_id=:id"),
                {"id": world.dataset_id},
            ),
            await db.scalar(
                text("SELECT count(*) FROM tasks WHERE project_id=:id"),
                {"id": world.base.project_id},
            ),
            await db.scalar(
                text("SELECT file_count FROM datasets WHERE id=:id"),
                {"id": world.dataset_id},
            ),
            await db.scalar(
                text("SELECT total_tasks FROM projects WHERE id=:id"),
                {"id": world.base.project_id},
            ),
        )


async def test_zip_staged_targets_are_immutable_and_receipt_replays(ingest_world):
    world, command = ingest_world, ingest_world.command()
    async with world.source_maker.begin() as db:
        await _begin(db, command, _descriptor(count=3))
        plan = await _prepare(
            db,
            world,
            command,
            [
                _entry("nested/a.png"),
                _entry("nested/b.png"),
                _entry("other.png", "b" * 32),
            ],
        )
        assert plan.entries[0].status == "pending"
        entry = plan.entries[0]
        assert entry.file_path == (
            f"dataset-sources/{world.dataset_id}/{entry.item_id}/{entry.publication_id}/nested/a.png"
        )
        result = await _complete(db, command, plan)
        assert result.progress.added == 2 and result.progress.deduped == 1
        receipt = await _finish(db, command)
        assert receipt.added == receipt.linked_tasks == 2
        assert receipt.deduped == 1 and receipt.error_count == 0
    assert await _counts(world) == (2, 2, 2, 2)
    async with world.source_maker.begin() as db:
        replay = await _begin(db, command, _descriptor(count=1))
        assert replay.state == "completed" and replay.result == receipt
        assert replay.descriptor.planned_entries == 3
    assert await _counts(world) == (2, 2, 2, 2)


async def test_entire_outer_transaction_failure_removes_items_tasks_receipts_and_outbox(
    ingest_world,
):
    world, command = ingest_world, ingest_world.command()
    with pytest.raises(RuntimeError, match="final commit failure"):
        async with world.source_maker.begin() as db:
            await _begin(db, command)
            plan = await _prepare(db, world, command, [_entry()])
            await _complete(db, command, plan)
            await _finish(db, command)
            raise RuntimeError("final commit failure")
    assert await _counts(world) == (0, 0, 0, 0)
    async with world.maker() as db:
        for table in (
            "dataset_source_ingest_operations",
            "dataset_source_ingest_entries",
            "dataset_source_ingest_deliveries",
        ):
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM " + table + " WHERE operation_id=:id"),
                    {"id": command.operation_id},
                )
                == 0
            )


async def test_scene_insert_conflict_cannot_commit_positive_ingest_counters(
    ingest_world,
):
    world, command = ingest_world, ingest_world.command()
    name = "ingest_scene_conflict_" + uuid4().hex
    async with world.maker.begin() as db:
        connection = await db.connection()
        await connection.exec_driver_sql(
            f"CREATE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN IF NEW.dataset_id='{world.dataset_id}'::uuid THEN "
            "RAISE EXCEPTION 'controlled Scene conflict' USING ERRCODE='23505'; "
            "END IF; RETURN NEW; END $$"
        )
        await connection.exec_driver_sql(
            f"CREATE TRIGGER {name} BEFORE INSERT ON public.scenes "
            f"FOR EACH ROW EXECUTE FUNCTION public.{name}()"
        )
    try:
        with pytest.raises(IngestFailure, match="scene_conflict"):
            async with world.source_maker.begin() as db:
                await _begin(db, command)
                plan = await _prepare(db, world, command, [_entry()])
                assert (await _complete(db, command, plan)).progress.added == 1
                await _finish(db, command)
        assert await _counts(world) == (0, 0, 0, 0)
        async with world.maker() as db:
            for table in (
                "dataset_source_ingest_operations",
                "dataset_source_ingest_entries",
                "dataset_source_ingest_deliveries",
            ):
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM " + table + " WHERE operation_id=:id"
                        ),
                        {"id": command.operation_id},
                    )
                    == 0
                )
    finally:
        async with world.maker.begin() as db:
            connection = await db.connection()
            await connection.exec_driver_sql(f"DROP TRIGGER {name} ON public.scenes")
            await connection.exec_driver_sql(f"DROP FUNCTION public.{name}()")


async def test_employee_zip_allowed_but_scan_requires_current_manager(ingest_world):
    world = ingest_world
    command = world.command(world.base.other_actor_id)
    async with world.source_maker() as db:
        assert (await _begin(db, command)).state == "preparing"
        await db.rollback()
    async with world.source_maker() as db:
        with pytest.raises(IngestFailure, match="catalog_manager_required"):
            await _begin(
                db, world.command(world.base.other_actor_id), _descriptor("scan")
            )
        await db.rollback()


async def test_prepare_cannot_be_committed_then_continued_as_another_transaction(
    ingest_world,
):
    world, command = ingest_world, ingest_world.command()
    async with world.source_maker.begin() as db:
        await _begin(db, command)
    async with world.source_maker() as db:
        with pytest.raises(IngestFailure, match="operation_conflict"):
            await _prepare(db, world, command, [_entry()])
        await db.rollback()
    assert await _counts(world) == (0, 0, 0, 0)


async def test_scan_publishes_existing_prefix_objects_without_scenes(ingest_world):
    world, command = ingest_world, ingest_world.command()
    async with world.maker.begin() as db:
        await db.execute(
            text("UPDATE datasets SET is_temporal=true WHERE id=:id"),
            {"id": world.dataset_id},
        )
    async with world.source_maker.begin() as db:
        context = await _begin(db, command, _descriptor("scan"))
        key = context.source_prefix + "nested/frame.pcd"
        plan = await _prepare(
            db,
            world,
            command,
            [_entry("nested/frame.pcd", kind="point_cloud", key=key)],
        )
        assert plan.entries[0].file_path == key
        assert plan.entries[0].publication_id is None
        await _complete(db, command, plan)
        result = await _finish(db, command, infer=False)
        assert result.kind == "scan" and result.added == 1
        assert result.scene_inference_notes == [] and result.total_in_zip is None
    async with world.maker() as db:
        assert (
            await db.scalar(
                text("SELECT count(*) FROM scenes WHERE dataset_id=:id"),
                {"id": world.dataset_id},
            )
            == 0
        )
    assert await _counts(world) == (1, 1, 1, 1)


async def test_exact_source_login_cannot_read_business_tables_or_foreign_capabilities(
    ingest_world,
):
    world = ingest_world
    for statement in (
        "SELECT * FROM public.dataset_items LIMIT 1",
        "SELECT aap_identity_v1.lock_active_actor(NULL::uuid)",
        "SELECT aap_assets_v1.claim_dataset_source_ingest_delivery(NULL::uuid,NULL::integer)",
        "SELECT aap_assets_v1.complete_dataset_source_ingest_delivery(NULL::uuid,NULL::uuid)",
        "SET ROLE owner",
    ):
        async with world.source_maker() as db:
            with pytest.raises(DBAPIError):
                await db.execute(text(statement))
            await db.rollback()


async def test_maintenance_caller_can_deliver_without_raw_assets_table_access(
    ingest_world,
):
    from app.services.dataset_source_ingest_delivery import (
        claim_dataset_source_ingest_delivery,
    )
    from app.workers.dataset_import_delivery import _deliver_source_ingest_one
    from unittest.mock import Mock

    world, command = ingest_world, ingest_world.command()
    async with world.source_maker.begin() as db:
        await _begin(db, command)
        plan = await _prepare(db, world, command, [_entry()])
        await _complete(db, command, plan)
        await _finish(db, command)
    url = make_url(os.environ["AAP_MODULE_DATA_APPEND_URL"])
    reference = world.source_maker.kw["bind"].url
    assert url.username == "append_client"
    assert (url.host, url.port, url.database) == (
        reference.host,
        reference.port,
        reference.database,
    )
    engine = create_async_engine(url, hide_parameters=True)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            with pytest.raises(DBAPIError):
                await db.execute(text("SELECT * FROM dataset_source_ingest_deliveries"))
            await db.rollback()
            lease = await claim_dataset_source_ingest_delivery(
                db, lease_token=uuid4(), lease_seconds=120
            )
            await db.commit()
            assert lease is not None and lease.operation_id == command.operation_id
            dispatcher = Mock()
            assert (
                await _deliver_source_ingest_one(db, lease, dispatcher) == "delivered"
            )
            dispatcher.dispatch_thumbnail.assert_called_once_with(lease.target_id)
    finally:
        await engine.dispose()


async def test_scan_cannot_adopt_reserved_modern_publication_namespace(ingest_world):
    world = ingest_world
    async with world.maker.begin() as db:
        await db.execute(
            text("UPDATE datasets SET name='dataset-sources' WHERE id=:id"),
            {"id": world.dataset_id},
        )
    async with world.source_maker() as db:
        with pytest.raises(IngestFailure, match="reserved_namespace"):
            await _begin(db, world.command(), _descriptor("scan"))
        await db.rollback()


async def test_duplicate_candidate_can_succeed_after_first_copy_failure(ingest_world):
    world, command = ingest_world, ingest_world.command()
    async with world.source_maker.begin() as db:
        await _begin(db, command, _descriptor(count=2))
        plan = await _prepare(
            db, world, command, [_entry("first.png"), _entry("second.png")]
        )
        first, second = plan.entries
        assert first.status == second.status == "pending"
        completions = [
            IngestEntryCompletion(
                entry_id=first.entry_id,
                status="error",
                publication_id=first.publication_id,
                file_size=None,
                content_hash=None,
                width=None,
                height=None,
                error_code="storage_write_failed",
            ),
            IngestEntryCompletion(
                entry_id=second.entry_id,
                status="ready",
                publication_id=second.publication_id,
                file_size=second.file_size,
                content_hash=second.content_hash,
                width=second.width,
                height=second.height,
                error_code=None,
            ),
        ]
        settled = await _invoke(
            db,
            command,
            "complete_dataset_ingest_batch",
            IngestBatchResult,
            batch=plan.batch_id,
            payload=completions,
        )
        assert settled.progress.added == settled.progress.error_count == 1
        receipt = await _finish(db, command)
        assert receipt.added == 1 and receipt.deduped == 0 and receipt.error_count == 1
    assert await _counts(world) == (1, 1, 1, 1)


async def test_layout_ignores_invalid_logical_markers_in_legacy_metadata(ingest_world):
    from app.db.models.dataset import DatasetItem

    world, command = ingest_world, ingest_world.command()
    identifiers = [uuid4(), uuid4(), uuid4()]
    markers = [
        {"operation_id": "not-a-uuid", "relpath": "wrong/lidar/0001.pcd"},
        {"operation_id": str(uuid4()), "relpath": "../wrong/lidar/0002.pcd"},
        {"operation_id": "urn:uuid:" + str(uuid4()), "relpath": "right/lidar/0003.pcd"},
    ]
    async with world.maker.begin() as db:
        for index, (identifier, marker) in enumerate(
            zip(identifiers, markers, strict=True)
        ):
            db.add(
                DatasetItem(
                    id=identifier,
                    dataset_id=world.dataset_id,
                    file_name=f"{index:04d}.pcd",
                    file_type="point_cloud",
                    file_path=f"legacy/trip-{index}/lidar/{index:04d}.pcd",
                    metadata_={"source_ingest": marker},
                )
            )
        await db.flush()
        before = await db.scalar(
            text("SELECT aap_assets_v1._dataset_source_ingest_dataset_snapshot(:id)"),
            {"id": world.dataset_id},
        )
        await db.execute(
            text(
                "UPDATE dataset_items SET metadata=jsonb_set(metadata,'{source_ingest,relpath}','\"another/wrong/path.pcd\"'::jsonb) WHERE id=:id"
            ),
            {"id": identifiers[0]},
        )
        after = await db.scalar(
            text("SELECT aap_assets_v1._dataset_source_ingest_dataset_snapshot(:id)"),
            {"id": world.dataset_id},
        )
        assert before == after
    async with world.source_maker() as db:
        await _begin(db, command)
        plan = await _prepare(db, world, command, [_entry()])
        await _complete(db, command, plan)
        page = await _invoke(
            db, command, "read_dataset_ingest_layout", IngestLayoutPage, limit=500
        )
        logical = {item.id: item.logical_path for item in page.items}
        assert [logical[identifier] for identifier in identifiers] == [
            None,
            None,
            "right/lidar/0003.pcd",
        ]
        await db.rollback()
