"""Disposable native ZIP/scan ownership, transaction and replay qualification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
from io import BytesIO
import json
import os
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from PIL import Image

from yoao_assets_source.ingest_contract import (
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
from yoao_assets_source.frame_layout import PreparedDatasetItem
from yoao_assets_source.scene_layout import SceneLayoutInput, plan_scenes
from app.config import settings
from app.services.dataset_assets import ensure_legacy_bindings
from tests.fakes.managed_creation_storage import CreationStorage
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
    physical: object = field(default_factory=CreationStorage)
    image_facts: dict[UUID, object] = field(default_factory=dict)

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
            authority={
                "credential_type": "jwt",
                "credential_id": uuid4(),
                "generation": 0,
                "expires_at": datetime(2100, 1, 1, tzinfo=UTC),
                "api_key_revision": None,
                "dataset_lifecycle_revision": 1,
                "directory_protected": False,
            },
        )
        self.operation_ids.add(value.operation_id)
        return value


@pytest_asyncio.fixture
async def ingest_world(test_engine):
    raw = os.environ["AAP_MODULE_DATA_ASSET_SOURCE_URL"]
    url = make_url(raw)
    assert url.username == "yoao_asset_source_client"
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert url.database.startswith("yoao_wt_") and url.database.endswith("_test")
    engine = create_async_engine(url, hide_parameters=True)
    world = _World(
        await _seed_world(test_engine),
        async_sessionmaker(engine, expire_on_commit=False),
    )
    try:
        async with world.maker.begin() as owner:
            await ensure_legacy_bindings(owner)
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


_ENTRY_BYTES = {}


def _entry(path="frames/a.png", digest="a" * 32, kind="image", key=None):
    raw = bytes.fromhex(digest[-6:])
    if kind == "image":
        buffer = BytesIO()
        Image.new("RGB", (2, 2), tuple(raw)).save(buffer, format="PNG")
        raw = buffer.getvalue()
    content_hash = hashlib.md5(raw, usedforsecurity=False).hexdigest()
    _ENTRY_BYTES[content_hash] = raw
    return IngestSourceEntry(
        source_name=path,
        relpath=path,
        source_key=key,
        file_type=kind,
        content_type=(
            "image/png"
            if kind == "image"
            else "video/mp4"
            if kind == "video"
            else "application/octet-stream"
        ),
        file_size=len(raw),
        content_hash=content_hash,
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
        if function == "begin_dataset_ingest":
            value["authority"] = command.authority.model_dump(mode="json")
        parameters["payload"] = json.dumps(value)
    reply = await db.scalar(
        text("SELECT yoao_assets_v1." + function + "(" + ",".join(arguments) + ")"),
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


async def _stage_physical(db, world, command, plan, completions, *, stage=True):
    from app.services.assets_source_ingest import DatasetSourceIngestData
    from yoao_assets_source_runtime.ingest_storage import IngestStorage
    from yoao_assets_source_runtime.ingest_workflow import (
        IngestWorkflow,
        _ingest_original_request,
    )

    ready = {item.entry_id for item in completions if item.status == "ready"}
    async with world.maker() as owner:
        data = DatasetSourceIngestData(owner)
        storage = IngestStorage(world.physical, settings.minio_datasets_bucket)
        workflow = IngestWorkflow(data=data, storage=storage, image_admission=None)
        for entry in plan.entries:
            if entry.entry_id not in ready:
                continue
            target = await data.allocate(
                _ingest_original_request(command, entry, plan.batch_id)
            )
            await data.open_operation(target.creation_id)
            facts, proof = await storage.publish(
                BytesIO(_ENTRY_BYTES[entry.content_hash]),
                target,
                content_type=entry.content_type,
                expected_size=entry.file_size,
                expected_hash=entry.content_hash,
                image=entry.file_type == "image",
                on_prepared=lambda prepared: data.original_prepared(
                    target.creation_id, prepared.evidence()
                ),
                on_writing=lambda: data.original_writing(target.creation_id),
                on_written=lambda receipt: data.original_written(
                    target.creation_id,
                    {
                        "etag": receipt.etag,
                        "version_id": receipt.version_id,
                        "request_id": receipt.request_id,
                        "response": receipt.response,
                    },
                ),
            )
            await workflow._finish_original(target, proof)
            if entry.file_type == "image":
                world.image_facts[entry.entry_id] = facts
    if stage:
        await _invoke(
            db,
            command,
            "stage_dataset_ingest_batch",
            IngestBatchResult,
            batch=plan.batch_id,
            payload=completions,
        )


async def _complete(db, command, plan, *, world=None):
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
    if world is not None and any(entry.source_key is None for entry in plan.entries):
        await _stage_physical(db, world, command, plan, completions)
    result = await _invoke(
        db,
        command,
        "complete_dataset_ingest_batch",
        IngestBatchResult,
        batch=plan.batch_id,
        payload=completions,
    )
    if world is not None:
        await _admit_images(db, world, plan, completions)
    return result


async def _admit_images(db, world, plan, completions):
    from yoao_assets_source_runtime.image_admission import ImageAdmission
    from app.services.assets_images import image_gateway, image_policy

    admission = ImageAdmission(
        image_gateway(db, caller_transaction=True), image_policy()
    )
    ready = {entry.entry_id for entry in completions if entry.status == "ready"}
    for entry in plan.entries:
        if entry.file_type != "image" or entry.entry_id not in ready:
            continue
        facts = world.image_facts[entry.entry_id]
        await admission.admit_dataset_item(
            item_id=entry.item_id,
            operation_namespace=(
                entry.publication_id if entry.source_key is None else entry.entry_id
            ),
            file_path=entry.file_path,
            file_size=facts.file_size,
            content_hash=facts.content_hash,
            object_identity=facts.object_identity,
            missing_is_skipped=True,
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


async def test_zip_owner_rejects_completion_before_all_entries_are_staged(ingest_world):
    world, command = ingest_world, ingest_world.command()
    async with world.source_maker() as db:
        await _begin(db, command, _descriptor(count=201))
        first = await _prepare(
            db,
            world,
            command,
            [
                _entry(path=f"frames/{i:03}.bin", digest=f"{i:032x}", kind="other")
                for i in range(200)
            ],
        )
        with pytest.raises(IngestFailure, match="operation_conflict"):
            await _complete(db, command, first)
        assert await _counts(world) == (0, 0, 0, 0)

        last = await _prepare(
            db,
            world,
            command,
            [_entry(path="frames/200.bin", digest=f"{200:032x}", kind="other")],
        )
        assert last.progress.staged == 201
        forged = [
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
            for entry in first.entries
        ]
        with pytest.raises(IngestFailure, match="operation_conflict"):
            await _invoke(
                db,
                command,
                "stage_dataset_ingest_batch",
                IngestBatchResult,
                batch=first.batch_id,
                payload=forged,
            )
        with pytest.raises(IngestFailure, match="operation_conflict"):
            await _complete(db, command, first)
        assert await _counts(world) == (0, 0, 0, 0)
        await db.rollback()


@pytest.mark.parametrize("kind", ["image", "dimensionless_image", "other"])
async def test_zip_rejects_forged_later_dimensions_before_any_batch_publishes(
    ingest_world, kind
):
    world, command = ingest_world, ingest_world.command()
    async with world.source_maker() as db:
        await _begin(db, command, _descriptor(count=2))
        first = await _prepare(db, world, command, [_entry(path="first.png")])
        proposal = _entry(
            path="last",
            digest="b" * 32,
            kind="other" if kind == "dimensionless_image" else kind,
        )
        if kind == "dimensionless_image":
            proposal = proposal.model_copy(
                update={"file_type": "image", "content_type": "image/png"}
            )
        last = await _prepare(db, world, command, [proposal])

        def completion(entry):
            return IngestEntryCompletion(
                entry_id=entry.entry_id,
                status="ready",
                publication_id=entry.publication_id,
                file_size=entry.file_size,
                content_hash=entry.content_hash,
                width=entry.width,
                height=entry.height,
                error_code=None,
            )

        first_completion = [completion(first.entries[0])]
        last_completion = [completion(last.entries[0])]
        await _stage_physical(db, world, command, first, first_completion)
        await _stage_physical(db, world, command, last, last_completion, stage=False)
        forged = last_completion[0].model_copy(update={"width": 9, "height": 9})
        with pytest.raises(IngestFailure, match="operation_conflict"):
            await _invoke(
                db,
                command,
                "stage_dataset_ingest_batch",
                IngestBatchResult,
                batch=last.batch_id,
                payload=[forged],
            )
        with pytest.raises(IngestFailure, match="operation_conflict"):
            await _complete(db, command, first)
        assert await _counts(world) == (0, 0, 0, 0)
        await db.rollback()


async def test_failed_zip_stage_cannot_seal_partial_completions(ingest_world):
    world, command = ingest_world, ingest_world.command()
    async with world.source_maker() as db:
        await _begin(db, command, _descriptor(count=2))
        plan = await _prepare(
            db,
            world,
            command,
            [_entry(path="first.png"), _entry(path="last.png", digest="b" * 32)],
        )
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
        ]
        await _stage_physical(db, world, command, plan, completions, stage=False)
        for completion in completions:
            with pytest.raises(IngestFailure, match="invalid_request"):
                await _invoke(
                    db,
                    command,
                    "stage_dataset_ingest_batch",
                    IngestBatchResult,
                    batch=plan.batch_id,
                    payload=[completion, completion],
                )
        with pytest.raises(IngestFailure, match="operation_conflict"):
            await _complete(db, command, plan)
        assert await _counts(world) == (0, 0, 0, 0)
        await db.rollback()


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
        result = await _complete(db, command, plan, world=world)
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
            await _complete(db, command, plan, world=world)
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
                assert (
                    await _complete(db, command, plan, world=world)
                ).progress.added == 1
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
    from app.services.assets_source_ingest import DatasetSourceIngestData
    from yoao_assets_source_runtime.ingest_storage import IngestStorage
    from yoao_assets_source_runtime.ingest_workflow import (
        IngestWorkflow,
        _initial_progress,
        _scan_source_entry,
    )

    world, command = ingest_world, ingest_world.command()
    source_bytes = b"existing point cloud bytes"
    content_hash = hashlib.md5(source_bytes, usedforsecurity=False).hexdigest()
    async with world.maker.begin() as db:
        await db.execute(
            text("UPDATE datasets SET is_temporal=true WHERE id=:id"),
            {"id": world.dataset_id},
        )
    async with world.source_maker.begin() as db:
        context = await _begin(db, command, _descriptor("scan"))
        key = context.source_prefix + "nested/frame.pcd"
        world.physical.objects[key] = source_bytes
        storage = IngestStorage(world.physical, settings.minio_datasets_bucket)
        [listed] = [item async for item in storage.iter_objects(context.source_prefix)]
        source = _scan_source_entry(
            listed.key[len(context.source_prefix) :], listed.key, listed.size
        )
        assert source.content_hash is None
        workflow = IngestWorkflow(
            data=DatasetSourceIngestData(db), storage=storage, image_admission=None
        )
        batch_id, plan, completions, image_facts = await workflow._scan_batch(
            command,
            context.source_prefix,
            [source],
            [(listed.key, listed.etag, listed.size)],
            context.next_index,
            _initial_progress(context),
        )
        world.batch_ids.add(batch_id)
        assert plan.entries[0].file_path == key
        assert plan.entries[0].publication_id is None
        [completion] = completions
        assert completion.status == "ready" and completion.publication_id is None
        assert completion.file_size == len(source_bytes)
        assert completion.content_hash == content_hash
        assert image_facts == {}
        settled = await _invoke(
            db,
            command,
            "complete_dataset_ingest_batch",
            IngestBatchResult,
            batch=batch_id,
            payload=completions,
        )
        assert settled.progress.added == settled.progress.linked_tasks == 1
        result = await _finish(db, command, infer=False)
        assert result.kind == "scan" and result.added == 1
        assert result.scene_inference_notes == [] and result.total_in_zip is None
    assert world.physical.objects == {key: source_bytes}
    assert not world.physical.uploads
    assert not any(
        call[0] in {"put", "create", "complete", "abort", "delete"}
        for call in world.physical.calls
    )
    async with world.maker() as db:
        item = (
            await db.execute(
                text(
                    "SELECT file_path,file_size,content_hash,scene_id,frame_index "
                    "FROM dataset_items WHERE dataset_id=:id"
                ),
                {"id": world.dataset_id},
            )
        ).one()
        assert tuple(item) == (key, len(source_bytes), content_hash, None, None)
        verification = (
            await db.execute(
                text(
                    "SELECT status,sha256,md5,size_bytes "
                    "FROM managed_original_creations "
                    "WHERE dataset_id=:id AND authority->>'operation_id'=:operation"
                ),
                {"id": world.dataset_id, "operation": str(command.operation_id)},
            )
        ).one()
        assert tuple(verification) == (
            "verified",
            hashlib.sha256(source_bytes).hexdigest(),
            content_hash,
            len(source_bytes),
        )
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
        "SELECT dataset_id FROM public.dataset_sources LIMIT 1",
        "SELECT yoao_identity_v1.lock_active_actor(NULL::uuid)",
        "SELECT yoao_assets_v1.claim_dataset_source_ingest_delivery(NULL::uuid,NULL::integer)",
        "SELECT yoao_assets_v1.complete_dataset_source_ingest_delivery(NULL::uuid,NULL::uuid)",
        "SET ROLE owner",
    ):
        async with world.source_maker() as db:
            with pytest.raises(DBAPIError):
                await db.execute(text(statement))
            await db.rollback()


async def test_nonimage_delivery_requires_current_lease_without_private_table_access(
    ingest_world,
):
    from app.services.dataset_source_ingest_delivery import (
        claim_dataset_source_ingest_delivery,
        complete_dataset_source_ingest_delivery,
    )

    world, command = ingest_world, ingest_world.command()
    async with world.source_maker.begin() as db:
        await _begin(db, command)
        plan = await _prepare(db, world, command, [_entry("clip.mp4", kind="video")])
        await _complete(db, command, plan, world=world)
        await _finish(db, command)
    url = make_url(os.environ["AAP_MODULE_DATA_APPEND_URL"])
    reference = world.source_maker.kw["bind"].url
    assert url.username == "yoao_append_client"
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
            assert lease is not None
            assert (lease.operation_id, lease.target_id, lease.kind) == (
                command.operation_id,
                plan.entries[0].item_id,
                "video_metadata",
            )
            with pytest.raises(IngestFailure):
                await complete_dataset_source_ingest_delivery(
                    db, delivery_id=lease.id, lease_token=uuid4()
                )
            await db.rollback()
            assert (
                await claim_dataset_source_ingest_delivery(
                    db, lease_token=uuid4(), lease_seconds=120
                )
                is None
            )
            acknowledged = await complete_dataset_source_ingest_delivery(
                db, delivery_id=lease.id, lease_token=lease.lease_token
            )
            assert acknowledged.delivered
            await db.commit()
        async with maker() as observer:
            assert (
                await claim_dataset_source_ingest_delivery(
                    observer, lease_token=uuid4(), lease_seconds=120
                )
                is None
            )
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
        await _stage_physical(db, world, command, plan, completions)
        settled = await _invoke(
            db,
            command,
            "complete_dataset_ingest_batch",
            IngestBatchResult,
            batch=plan.batch_id,
            payload=completions,
        )
        await _admit_images(db, world, plan, completions)
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
            text("SELECT yoao_assets_v1._dataset_source_ingest_dataset_snapshot(:id)"),
            {"id": world.dataset_id},
        )
        await db.execute(
            text(
                "UPDATE dataset_items SET metadata=jsonb_set(metadata,'{source_ingest,relpath}','\"another/wrong/path.pcd\"'::jsonb) WHERE id=:id"
            ),
            {"id": identifiers[0]},
        )
        after = await db.scalar(
            text("SELECT yoao_assets_v1._dataset_source_ingest_dataset_snapshot(:id)"),
            {"id": world.dataset_id},
        )
        assert before == after
    async with world.source_maker() as db:
        await _begin(db, command)
        plan = await _prepare(db, world, command, [_entry()])
        await _complete(db, command, plan, world=world)
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


async def test_directory_manager_uses_current_public_identity_facts(
    ingest_world, test_engine
):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.services.dataset_source_access import assert_dataset_manage

    world = ingest_world
    url = make_url(os.environ["DATABASE_URL"])
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    engine = create_async_engine(url, hide_parameters=True)
    runtime = async_sessionmaker(engine, expire_on_commit=False)
    actor = SimpleNamespace(id=world.actor_id)
    try:
        async with runtime() as db:
            privileges = (
                await db.execute(
                    text(
                        "SELECT current_user, rolsuper, "
                        "has_table_privilege(current_user, 'public.users', 'SELECT') "
                        "AS private_identity_read "
                        "FROM pg_roles WHERE rolname=current_user"
                    )
                )
            ).one()
            assert privileges.current_user == url.username
            assert not privileges.rolsuper and not privileges.private_identity_read
            current = await assert_dataset_manage(db, world.dataset_id, actor)
            assert current.id == world.actor_id and current.role == "super_admin"
        async with world.maker.begin() as owner:
            await owner.execute(
                text("UPDATE users SET role='employee' WHERE id=:id"),
                {"id": world.actor_id},
            )
        async with runtime() as db:
            with pytest.raises(HTTPException) as rejected:
                await assert_dataset_manage(db, world.dataset_id, actor)
            assert rejected.value.status_code == 403
            assert rejected.value.detail == {"code": "access_revoked"}
    finally:
        await engine.dispose()
