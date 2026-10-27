"""Real transactions for Assets-owned legacy upload completion."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from aap_assets_source.legacy_upload_contract import decode_legacy_upload_reply
from app.db.models.dataset import DatasetItem
from tests.test_dataset_upload_native import _cleanup_world, _seed_world

pytestmark = pytest.mark.asyncio

_LOCK = text(
    "SELECT aap_assets_v1.lock_legacy_dataset_upload("
    "CAST(:operation AS uuid),CAST(:actor AS uuid),CAST(:dataset AS uuid),CAST(:item AS uuid))"
)
_COMPLETE = text(
    "SELECT aap_assets_v1.complete_legacy_dataset_upload("
    "CAST(:operation AS uuid),CAST(:actor AS uuid),CAST(:dataset AS uuid),CAST(:item AS uuid),"
    ":path,CAST(:size AS bigint),CAST(:hash AS text),CAST(:width AS integer),CAST(:height AS integer))"
)


@pytest.fixture
async def legacy_world(test_engine):
    world = await _seed_world(test_engine)
    item_id = uuid4()
    world.item_ids.add(item_id)
    async with world.maker.begin() as db:
        db.add(
            DatasetItem(
                id=item_id,
                dataset_id=world.dataset_id,
                file_name="legacy.jpg",
                file_path=f"legacy/{item_id}.jpg",
                file_type="image",
                file_size=1,
                width=40,
            )
        )
        await db.execute(
            text("UPDATE public.datasets SET file_count=1 WHERE id=:id"),
            {"id": world.dataset_id},
        )
    try:
        yield world, item_id
    finally:
        async with world.maker.begin() as db:
            await db.execute(
                text(
                    "DELETE FROM public.dataset_legacy_upload_operations WHERE dataset_id=:id"
                ),
                {"id": world.dataset_id},
            )
            await db.execute(
                text("DELETE FROM public.task_append_operations WHERE dataset_id=:id"),
                {"id": world.dataset_id},
            )
        await _cleanup_world(world)


def _arguments(world, item_id, operation_id, **overrides):
    return {
        "operation": operation_id,
        "actor": world.actor_id,
        "dataset": world.dataset_id,
        "item": item_id,
        "path": f"legacy/{item_id}.jpg",
        "size": 12,
        "hash": "a" * 32,
        "width": 320,
        "height": 240,
        **overrides,
    }


async def _facts(db, world, item_id):
    item = (
        await db.execute(
            text(
                "SELECT file_size,content_hash,width,height FROM public.dataset_items WHERE id=:id"
            ),
            {"id": item_id},
        )
    ).one()
    return {
        "item": tuple(item),
        "tasks": await db.scalar(
            text("SELECT count(*) FROM public.tasks WHERE project_id=:id"),
            {"id": world.project_id},
        ),
        "counter": await db.scalar(
            text("SELECT total_tasks FROM public.projects WHERE id=:id"),
            {"id": world.project_id},
        ),
        "file_count": await db.scalar(
            text("SELECT file_count FROM public.datasets WHERE id=:id"),
            {"id": world.dataset_id},
        ),
        "receipts": await db.scalar(
            text(
                "SELECT count(*) FROM public.dataset_legacy_upload_operations WHERE dataset_id=:id"
            ),
            {"id": world.dataset_id},
        ),
    }


async def test_legacy_completion_metadata_tasks_and_receipt_commit_and_replay(
    legacy_world,
):
    world, item = legacy_world
    params = _arguments(world, item, uuid4())
    async with world.maker.begin() as db:
        pending = decode_legacy_upload_reply(await db.scalar(_LOCK, params))
        assert (
            pending.state == "pending"
            and pending.width == 40
            and pending.height is None
        )
        completed = decode_legacy_upload_reply(await db.scalar(_COMPLETE, params))
        assert completed.state == "completed" and completed.result.linked_tasks == 1
    async with world.maker.begin() as db:
        assert await _facts(db, world, item) == {
            "item": (12, "a" * 32, 320, 240),
            "tasks": 1,
            "counter": 1,
            "file_count": 1,
            "receipts": 1,
        }
        replay = decode_legacy_upload_reply(await db.scalar(_LOCK, params))
        assert replay == completed
        assert (
            decode_legacy_upload_reply(await db.scalar(_COMPLETE, params)) == completed
        )
        conflict = await db.scalar(_COMPLETE, {**params, "size": 13})
        assert conflict["error"]["code"] == "operation_conflict"
        other = await db.scalar(_LOCK, {**params, "actor": world.other_actor_id})
        assert other["error"]["code"] == "operation_conflict"
    async with world.maker.begin() as db:
        await db.execute(
            text("UPDATE public.users SET is_active=false WHERE id=:id"),
            {"id": world.actor_id},
        )
    async with world.maker.begin() as db:
        inactive = await db.scalar(_LOCK, params)
        assert inactive["error"]["code"] == "account_not_active"


async def test_legacy_replay_does_not_adopt_replaced_source(legacy_world):
    world, item = legacy_world
    params = _arguments(world, item, uuid4())
    async with world.maker.begin() as db:
        assert (
            decode_legacy_upload_reply(await db.scalar(_COMPLETE, params)).state
            == "completed"
        )
    async with world.maker.begin() as db:
        await db.execute(
            text(
                "UPDATE public.dataset_items SET file_path='another/source.jpg' WHERE id=:id"
            ),
            {"id": item},
        )
    async with world.maker.begin() as db:
        changed = await db.scalar(_LOCK, params)
        assert changed["error"]["code"] == "source_changed"
        assert (await _facts(db, world, item))["receipts"] == 1


async def test_legacy_outer_rollback_leaves_no_metadata_task_or_receipt(legacy_world):
    world, item = legacy_world
    params = _arguments(world, item, uuid4())
    async with world.maker() as db:
        assert (
            decode_legacy_upload_reply(
                await db.scalar(_COMPLETE, params)
            ).result.linked_tasks
            == 1
        )
        await db.rollback()
    async with world.maker.begin() as db:
        assert await _facts(db, world, item) == {
            "item": (1, None, 40, None),
            "tasks": 0,
            "counter": 0,
            "file_count": 1,
            "receipts": 0,
        }
        assert (
            decode_legacy_upload_reply(
                await db.scalar(_COMPLETE, params)
            ).result.linked_tasks
            == 1
        )


async def test_legacy_deferred_commit_failure_rolls_back_all_owners(legacy_world):
    world, item = legacy_world
    params = _arguments(world, item, uuid4())
    async with world.maker() as db:
        connection = await db.connection()
        await connection.exec_driver_sql(
            "CREATE TEMP TABLE legacy_upload_commit_parent (actor_id uuid PRIMARY KEY)"
        )
        await connection.exec_driver_sql(
            "CREATE TEMP TABLE legacy_upload_commit_probe ("
            "actor_id uuid REFERENCES legacy_upload_commit_parent(actor_id) DEFERRABLE INITIALLY DEFERRED)"
        )
        assert (
            decode_legacy_upload_reply(
                await db.scalar(_COMPLETE, params)
            ).result.linked_tasks
            == 1
        )
        await db.execute(
            text("INSERT INTO legacy_upload_commit_probe(actor_id) VALUES(:id)"),
            {"id": uuid4()},
        )
        with pytest.raises(DBAPIError):
            await db.commit()
        await db.rollback()
    async with world.maker.begin() as db:
        assert await _facts(db, world, item) == {
            "item": (1, None, 40, None),
            "tasks": 0,
            "counter": 0,
            "file_count": 1,
            "receipts": 0,
        }
        assert (
            decode_legacy_upload_reply(
                await db.scalar(_COMPLETE, params)
            ).result.linked_tasks
            == 1
        )


@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires owned provider-role instrumentation",
)
async def test_legacy_invalid_labeling_receipt_rolls_back_metadata(
    legacy_world, test_engine
):
    world, item = legacy_world
    function = "aap_labeling_v1.append_dataset_items"
    original = "aap_labeling_v1._legacy_original_append"
    async with test_engine.begin() as owner:
        await owner.exec_driver_sql(
            f"ALTER FUNCTION {function}(uuid,uuid[],uuid) RENAME TO _legacy_original_append"
        )
        await owner.exec_driver_sql(
            f"CREATE FUNCTION {function}(p_dataset_id uuid,p_item_ids uuid[],p_operation_id uuid) "
            "RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$ "
            "SELECT jsonb_build_object('schema','aap-task-append/v1','operation_id',p_operation_id,"
            "'dataset_id',p_dataset_id,'created',true,'project_totals','[]'::jsonb,'associations','[]'::jsonb) $$"
        )
        await owner.exec_driver_sql(
            f"REVOKE ALL ON FUNCTION {function}(uuid,uuid[],uuid) FROM PUBLIC"
        )
        await owner.exec_driver_sql(
            f"ALTER FUNCTION {function}(uuid,uuid[],uuid) OWNER TO labeling_code"
        )
        await owner.exec_driver_sql(
            f"GRANT EXECUTE ON FUNCTION {function}(uuid,uuid[],uuid) TO assets_code"
        )
    try:
        async with world.maker() as db:
            with pytest.raises(DBAPIError, match="Labeling receipt mismatch"):
                await db.scalar(_COMPLETE, _arguments(world, item, uuid4()))
            await db.rollback()
        async with world.maker.begin() as db:
            facts = await _facts(db, world, item)
            assert facts["item"] == (1, None, 40, None) and facts["receipts"] == 0
    finally:
        async with test_engine.begin() as owner:
            await owner.exec_driver_sql(f"DROP FUNCTION {function}(uuid,uuid[],uuid)")
            await owner.exec_driver_sql(
                f"ALTER FUNCTION {original}(uuid,uuid[],uuid) RENAME TO append_dataset_items"
            )


async def test_legacy_duplicate_and_wrong_parent_never_delete_or_rewrite_source(
    legacy_world,
):
    world, item = legacy_world
    duplicate_id = uuid4()
    world.item_ids.add(duplicate_id)
    async with world.maker.begin() as db:
        db.add(
            DatasetItem(
                id=duplicate_id,
                dataset_id=world.dataset_id,
                file_name="existing.jpg",
                file_path="legacy/existing.jpg",
                file_type="image",
                content_hash="a" * 32,
            )
        )
    async with world.maker.begin() as db:
        params = _arguments(world, item, uuid4())
        duplicate = decode_legacy_upload_reply(await db.scalar(_COMPLETE, params))
        assert duplicate.state == "duplicate" and duplicate.duplicate_of == duplicate_id
        facts = await _facts(db, world, item)
        assert (
            facts["item"] == (1, None, 40, None)
            and facts["tasks"] == facts["receipts"] == 0
        )
        wrong_parent = await db.scalar(_LOCK, {**params, "dataset": uuid4()})
        assert wrong_parent["error"]["code"] == "upload_missing"
        changed = await db.scalar(_COMPLETE, {**params, "path": "another/key.jpg"})
        assert changed["error"]["code"] == "source_changed"


async def test_legacy_active_noncreator_can_complete_without_inventing_intent(
    legacy_world,
):
    world, item = legacy_world
    params = _arguments(world, item, uuid4(), actor=world.other_actor_id)
    async with world.maker.begin() as db:
        completed = decode_legacy_upload_reply(await db.scalar(_COMPLETE, params))
        assert (
            completed.actor_id == world.other_actor_id
            and completed.result.linked_tasks == 1
        )
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.dataset_upload_intents WHERE item_id=:id"
                ),
                {"id": item},
            )
            == 0
        )


async def test_legacy_cannot_adopt_modern_intent_and_nowait_contention_retries(
    legacy_world,
):
    world, item = legacy_world
    params = _arguments(world, item, uuid4())
    async with world.maker() as first, world.maker() as second:
        assert (
            decode_legacy_upload_reply(await first.scalar(_LOCK, params)).state
            == "pending"
        )
        busy = await second.scalar(_LOCK, {**params, "operation": uuid4()})
        assert busy["error"]["code"] == "dataset_busy"
        await second.rollback()
        await first.rollback()
        assert (
            decode_legacy_upload_reply(await second.scalar(_LOCK, params)).state
            == "pending"
        )
        await second.rollback()
    async with world.maker.begin() as db:
        await db.execute(
            text(
                "INSERT INTO public.dataset_upload_intents(item_id,dataset_id,actor_id,file_name,file_type,content_type,staging_key) VALUES(:item,:dataset,:actor,'legacy.jpg','image','image/jpeg',:key)"
            ),
            {
                "item": item,
                "dataset": world.dataset_id,
                "actor": world.actor_id,
                "key": f"dataset-uploads/{item}/source",
            },
        )
        rejected = await db.scalar(_LOCK, params)
        assert rejected["error"]["code"] == "upload_scope_mismatch"


@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the disposable module-data boundary verifier",
)
async def test_source_login_can_complete_without_private_relations(
    legacy_world,
):
    world, item = legacy_world
    url = make_url(os.environ["AAP_MODULE_DATA_ASSET_SOURCE_URL"])
    assert url.username == "asset_source_client" and url.database.endswith("_test")
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.begin() as source:
            functions = (
                (
                    await source.execute(
                        text(
                            "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname LIKE 'aap_%_v1' AND has_function_privilege(current_user,p.oid,'EXECUTE')"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert all(name.startswith("aap_assets_v1.") for name in functions)
            assert {name.split("(", 1)[0] for name in functions} >= {
                "aap_assets_v1.resolve_internal_image_source",
                "aap_assets_v1.schedule_image_media",
                "aap_assets_v1.read_legacy_dataset_upload",
            }
            assert not await source.scalar(
                text(
                    "SELECT has_any_column_privilege(current_user,'public.dataset_items','SELECT') OR has_table_privilege(current_user,'public.dataset_legacy_upload_operations','SELECT') OR has_any_column_privilege(current_user,'public.storage_connections','SELECT') OR has_table_privilege(current_user,'public.storage_connection_operations','SELECT')"
                )
            )
            assert (
                decode_legacy_upload_reply(
                    await source.scalar(_COMPLETE, _arguments(world, item, uuid4()))
                ).result.linked_tasks
                == 1
            )
    finally:
        await engine.dispose()
