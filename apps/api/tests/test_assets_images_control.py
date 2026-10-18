"""Assets-owned native regressions for image source and admission control."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.db.models.dataset import DatasetItem
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset

pytestmark = pytest.mark.asyncio

_READ_SOURCE = text(
    "SELECT aap_assets_v1.read_image_source(CAST(:actor_id AS uuid), "
    "CAST(:task_id AS uuid), CAST(:item_id AS uuid))"
)
_OBSERVE_SOURCE = text(
    "SELECT aap_assets_v1.observe_image_source(CAST(:actor_id AS uuid), "
    "CAST(:task_id AS uuid), CAST(:item_id AS uuid), :snapshot, CAST(:identity AS jsonb))"
)
_RESOLVE_INTERNAL = text(
    "SELECT aap_assets_v1.resolve_internal_image_source(:owner_kind, "
    "CAST(:owner_id AS uuid), :snapshot, CAST(:identity AS jsonb))"
)
_READ_SUMMARIES = text(
    "SELECT aap_assets_v1.read_image_summaries(CAST(:actor_id AS uuid), CAST(:selectors AS jsonb))"
)
_READ_GENERATION = text(
    "SELECT aap_assets_v1.read_image_generation(CAST(:actor_id AS uuid), "
    "CAST(:task_id AS uuid), CAST(:item_id AS uuid), CAST(:generation AS integer))"
)
_PREPARE = text(
    "SELECT aap_assets_v1.prepare_image_media(CAST(:operation_id AS uuid), "
    "CAST(:actor_id AS uuid), CAST(:task_id AS uuid), CAST(:item_id AS uuid), "
    ":purpose, :snapshot, CAST(:policy AS jsonb), CAST(:force AS boolean))"
)
_RETRY = text(
    "SELECT aap_assets_v1.retry_image_media(CAST(:operation_id AS uuid), "
    "CAST(:actor_id AS uuid), CAST(:task_id AS uuid), CAST(:item_id AS uuid), "
    ":purpose, :snapshot, CAST(:policy AS jsonb), CAST(:force AS boolean))"
)
_READ_WORK = text(
    "SELECT aap_assets_v1.read_image_work(CAST(:actor_id AS uuid), CAST(:job_id AS uuid))"
)
_READ_INTERNAL_WORK = text(
    "SELECT aap_assets_v1.read_internal_image_work(CAST(:job_id AS uuid))"
)
_CANCEL = text(
    "SELECT aap_assets_v1.cancel_image_media(CAST(:operation_id AS uuid), "
    "CAST(:actor_id AS uuid), CAST(:job_id AS uuid))"
)
_SCHEDULE = text(
    "SELECT aap_assets_v1.schedule_image_media(CAST(:operation_id AS uuid), "
    "CAST(:owner_kind AS text), CAST(:owner_id AS uuid), CAST(:spec AS jsonb), "
    "CAST(:force AS boolean))"
)
_SCAN = text(
    "SELECT aap_assets_v1.scan_image_backfill(CAST(:selection AS jsonb), "
    "CAST(:after_kind AS text), CAST(:after_id AS uuid), CAST(:page_size AS integer))"
)
_INVALIDATE = text(
    "SELECT aap_assets_v1.invalidate_image_media(CAST(:actor_id AS uuid), "
    "CAST(:task_id AS uuid), CAST(:item_id AS uuid), CAST(:invalidation AS jsonb))"
)


def _object(value: object) -> dict:
    parsed = json.loads(value) if isinstance(value, str) else value
    assert isinstance(parsed, dict)
    return parsed


def _data(reply: object) -> dict:
    parsed = _object(reply)
    assert set(parsed) == {"schema", "ok", "data"}
    assert parsed["schema"] == "aap-assets-images/v1"
    assert parsed["ok"] is True
    assert isinstance(parsed["data"], dict)
    return parsed["data"]


def _error(reply: object, code: str) -> None:
    assert _object(reply) == {
        "schema": "aap-assets-images/v1",
        "ok": False,
        "error": {"code": code},
    }


async def _seed_image_item(db_session, *, actor_id, width=5000, height=5000):
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        name=f"image-native-{uuid4().hex[:8]}",
        data_type="image",
    )
    item_id = uuid4()
    db_session.add(
        DatasetItem(
            id=item_id,
            dataset_id=dataset_id,
            file_name=f"image-{item_id.hex[:8]}.png",
            file_path=f"image-source/{item_id}.png",
            file_type="image",
            file_size=2048,
            content_hash="a" * 64,
            width=width,
            height=height,
            metadata_={},
        )
    )
    await db_session.flush()
    return dataset_id, item_id


async def _read(db_session, actor_id, item_id, *, task_id=None):
    return _object(
        await db_session.scalar(
            _READ_SOURCE,
            {"actor_id": actor_id, "task_id": task_id, "item_id": item_id},
        )
    )


async def _observe(
    db_session, actor_id, source, identity, *, task_id=None, item_id=None
):
    return _object(
        await db_session.scalar(
            _OBSERVE_SOURCE,
            {
                "actor_id": actor_id,
                "task_id": task_id,
                "item_id": item_id,
                "snapshot": source["source_snapshot"],
                "identity": json.dumps(identity),
            },
        )
    )


async def test_item_source_observation_and_pyramid_summary_follow_generation(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    _, item_id = await _seed_image_item(db_session, actor_id=actor_id)

    source = _data(await _read(db_session, actor_id, item_id))["data"]
    assert source["owner"] == {"kind": "dataset_item", "id": str(item_id)}
    assert source["object_identity"] is None
    assert source["width"] == 5000 and source["height"] == 5000

    identity = {"etag": "image-object-v1", "byte_size": 2048}
    bound_reply = await _observe(
        db_session, actor_id, source, identity, item_id=item_id
    )
    bound = _data(bound_reply)["data"]
    assert bound["object_identity"]["etag"] == "image-object-v1"
    assert bound["source_generation"] == source["source_generation"]
    assert bound["source_snapshot"] == source["source_snapshot"]

    same = _data(
        await _observe(
            db_session,
            actor_id,
            bound,
            {**identity, "version_id": None},
            item_id=item_id,
        )
    )["data"]
    assert same["source_generation"] == bound["source_generation"]
    assert same["source_snapshot"] == bound["source_snapshot"]
    trusted = _data(
        await db_session.scalar(
            _RESOLVE_INTERNAL,
            {
                "owner_kind": "dataset_item",
                "owner_id": item_id,
                "snapshot": None,
                "identity": None,
            },
        )
    )
    assert trusted["owner"] == {"kind": "dataset_item", "id": str(item_id)}
    assert trusted["source_snapshot"] == bound["source_snapshot"]

    summary = _data(
        await db_session.scalar(
            _READ_SUMMARIES,
            {
                "actor_id": actor_id,
                "selectors": json.dumps(
                    [{"task_id": None, "dataset_item_id": str(item_id)}]
                ),
            },
        )
    )["items"][0]
    assert summary["source"]["source_snapshot"] == bound["source_snapshot"]
    assert summary["thumbnail_current"] is False
    assert summary["pyramid"]["status"] == "missing"
    assert summary["pyramid"]["required"] is False

    changed = _data(
        await _observe(
            db_session,
            actor_id,
            bound,
            {"etag": "unexpected-replacement", "byte_size": 2048},
            item_id=item_id,
        )
    )["data"]
    assert changed["source_drifted"] is True
    assert changed["source_generation"] == bound["source_generation"] + 1
    assert changed["source_snapshot"] != bound["source_snapshot"]
    assert changed["width"] is None and changed["height"] is None


async def test_linked_task_source_uses_dataset_item_facts_and_current_visibility(
    db_session,
):
    from tests.test_image_source_authorities import _seed_image_task

    actor_id = await _seed_actor(db_session, role="employee")
    other_id = await _seed_actor(db_session, role="employee")
    dataset_id, item_id = await _seed_image_item(
        db_session, actor_id=actor_id, width=320, height=240
    )
    task = await _seed_image_task(db_session, actor_id=actor_id)
    task.dataset_item_id = item_id
    task.file_path = "stale-task-copy/must-not-win.png"
    await db_session.flush()

    source = _data(await _read(db_session, actor_id, None, task_id=task.id))["data"]
    assert source["owner"] == {"kind": "dataset_item", "id": str(item_id)}
    assert source["task_id"] == str(task.id)
    assert source["project_id"] == str(task.project_id)
    assert source["dataset_id"] == str(dataset_id)
    assert source["file_path"] == f"image-source/{item_id}.png"

    _error(await _read(db_session, other_id, None, task_id=task.id), "task_not_visible")


async def test_retry_receipt_replays_before_ready_and_new_retry_is_rejected(db_session):
    actor_id = await _seed_actor(db_session)
    _, item_id = await _seed_image_item(
        db_session, actor_id=actor_id, width=None, height=None
    )
    source = _data(await _read(db_session, actor_id, item_id))["data"]
    bound = _data(
        await _observe(
            db_session,
            actor_id,
            source,
            {"etag": "retry-source", "byte_size": 2048},
            item_id=item_id,
        )
    )["data"]
    operation_id = uuid4()
    accepted = _data(
        await db_session.scalar(
            _RETRY,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "task_id": None,
                "item_id": item_id,
                "purpose": "dimensions",
                "snapshot": None,
                "policy": "{}",
                "force": True,
            },
        )
    )
    assert accepted["operation_id"] == str(operation_id)
    job_id = accepted["work"]["job_id"]
    assert accepted["source"]["source_snapshot"] == bound["source_snapshot"]

    await db_session.execute(
        text("UPDATE public.dataset_items SET width=100,height=200 WHERE id=:item_id"),
        {"item_id": item_id},
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_image_jobs SET status='ready',completed_at=clock_timestamp() "
            "WHERE job_id=:job_id"
        ),
        {"job_id": job_id},
    )
    replay = _data(
        await db_session.scalar(
            _RETRY,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "task_id": None,
                "item_id": item_id,
                "purpose": "dimensions",
                "snapshot": None,
                "policy": "{}",
                "force": True,
            },
        )
    )
    assert replay["operation_id"] == str(operation_id)
    assert replay["work"]["job_id"] == job_id
    assert replay["source"]["width"] is None

    new_retry = await db_session.scalar(
        _RETRY,
        {
            "operation_id": uuid4(),
            "actor_id": actor_id,
            "task_id": None,
            "item_id": item_id,
            "purpose": "dimensions",
            "snapshot": None,
            "policy": "{}",
            "force": True,
        },
    )
    _error(new_retry, "already_ready")

    cursor_id = uuid4()
    await db_session.execute(
        text(
            "UPDATE public.asset_image_jobs SET progress=CAST(:progress AS jsonb) WHERE job_id=:job_id"
        ),
        {
            "job_id": job_id,
            "progress": json.dumps(
                {
                    "scheduled": 3,
                    "next_owner_kind": "task",
                    "next_owner_id": str(cursor_id),
                    "candidates": [{"file_path": "private"}],
                    "source_facts": {"object_identity": {"etag": "private"}},
                }
            ),
        },
    )
    public_status = _data(
        await db_session.scalar(_READ_WORK, {"actor_id": actor_id, "job_id": job_id})
    )
    assert public_status["work"]["job_id"] == job_id
    assert public_status["progress"] == {
        "scheduled": 3,
        "next_owner_kind": "task",
        "next_owner_id": str(cursor_id),
    }


async def test_pyramid_retry_receipt_replays_after_dimensions_make_it_ineligible(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    _, item_id = await _seed_image_item(
        db_session, actor_id=actor_id, width=5000, height=5000
    )
    source = _data(await _read(db_session, actor_id, item_id))["data"]
    _data(
        await _observe(
            db_session,
            actor_id,
            source,
            {"etag": "pyramid-replay", "byte_size": 2048},
            item_id=item_id,
        )
    )
    operation_id = uuid4()
    accepted = _data(
        await db_session.scalar(
            _RETRY,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "task_id": None,
                "item_id": item_id,
                "purpose": "pyramid",
                "snapshot": None,
                "policy": "{}",
                "force": True,
            },
        )
    )

    await db_session.execute(
        text("UPDATE public.dataset_items SET width=100,height=100 WHERE id=:item_id"),
        {"item_id": item_id},
    )
    replay = _data(
        await db_session.scalar(
            _RETRY,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "task_id": None,
                "item_id": item_id,
                "purpose": "pyramid",
                "snapshot": None,
                "policy": "{}",
                "force": True,
            },
        )
    )
    assert replay["work"]["job_id"] == accepted["work"]["job_id"]
    assert replay["source"]["source_snapshot"] == accepted["source"]["source_snapshot"]

    _error(
        await db_session.scalar(
            _RETRY,
            {
                "operation_id": uuid4(),
                "actor_id": actor_id,
                "task_id": None,
                "item_id": item_id,
                "purpose": "pyramid",
                "snapshot": None,
                "policy": "{}",
                "force": True,
            },
        ),
        "resource_limit",
    )


async def test_saved_retry_source_snapshot_change_returns_source_changed(db_session):
    actor_id = await _seed_actor(db_session)
    _, item_id = await _seed_image_item(db_session, actor_id=actor_id)
    source = _data(await _read(db_session, actor_id, item_id))["data"]
    _data(
        await _observe(
            db_session,
            actor_id,
            source,
            {"etag": "snapshot-replay", "byte_size": 2048},
            item_id=item_id,
        )
    )
    operation_id = uuid4()
    await db_session.scalar(
        _RETRY,
        {
            "operation_id": operation_id,
            "actor_id": actor_id,
            "task_id": None,
            "item_id": item_id,
            "purpose": "pyramid",
            "snapshot": None,
            "policy": "{}",
            "force": True,
        },
    )
    await db_session.execute(
        text("UPDATE public.dataset_items SET file_path=:path WHERE id=:item_id"),
        {"item_id": item_id, "path": f"replacement/{item_id}.png"},
    )

    _error(
        await db_session.scalar(
            _RETRY,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "task_id": None,
                "item_id": item_id,
                "purpose": "pyramid",
                "snapshot": None,
                "policy": "{}",
                "force": True,
            },
        ),
        "source_changed",
    )


async def test_malformed_saved_retry_receipt_still_returns_invalid_response(db_session):
    from aap_assets_images.policy import ImagePolicy

    actor_id = await _seed_actor(db_session)
    _, item_id = await _seed_image_item(db_session, actor_id=actor_id)
    operation_id = uuid4()
    await db_session.execute(
        text(
            "INSERT INTO public.asset_image_operations "
            "(operation_id,actor_id,action,scope,intent,result) "
            "VALUES (:operation_id,:actor_id,'retry',CAST(:scope AS jsonb), "
            "CAST(:intent AS jsonb),CAST(:result AS jsonb))"
        ),
        {
            "operation_id": operation_id,
            "actor_id": actor_id,
            "scope": json.dumps(
                {
                    "task_id": None,
                    "dataset_item_id": str(item_id),
                    "purpose": "pyramid",
                }
            ),
            "intent": json.dumps(
                {
                    "expected_source_snapshot": None,
                    "policy": ImagePolicy().model_dump(mode="json"),
                    "force": True,
                }
            ),
            "result": json.dumps(
                {"schema": "aap-assets-images/v1", "ok": True, "data": None}
            ),
        },
    )

    _error(
        await db_session.scalar(
            _RETRY,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "task_id": None,
                "item_id": item_id,
                "purpose": "pyramid",
                "snapshot": None,
                "policy": "{}",
                "force": True,
            },
        ),
        "invalid_response",
    )


async def test_saved_task_retry_detects_relinked_owner_before_source_readiness(
    db_session,
):
    from tests.test_image_source_authorities import _seed_image_task

    actor_id = await _seed_actor(db_session)
    _, first_item_id = await _seed_image_item(db_session, actor_id=actor_id)
    _, replacement_item_id = await _seed_image_item(db_session, actor_id=actor_id)
    task = await _seed_image_task(db_session, actor_id=actor_id)
    task.dataset_item_id = first_item_id
    await db_session.flush()

    source = _data(await _read(db_session, actor_id, None, task_id=task.id))["data"]
    _data(
        await _observe(
            db_session,
            actor_id,
            source,
            {"etag": "linked-replay", "byte_size": 2048},
            task_id=task.id,
        )
    )
    operation_id = uuid4()
    accepted = _data(
        await db_session.scalar(
            _RETRY,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "task_id": task.id,
                "item_id": None,
                "purpose": "pyramid",
                "snapshot": None,
                "policy": "{}",
                "force": True,
            },
        )
    )
    assert accepted["source"]["owner"] == {
        "kind": "dataset_item",
        "id": str(first_item_id),
    }

    task.dataset_item_id = replacement_item_id
    await db_session.flush()
    _error(
        await db_session.scalar(
            _RETRY,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "task_id": task.id,
                "item_id": None,
                "purpose": "pyramid",
                "snapshot": None,
                "policy": "{}",
                "force": True,
            },
        ),
        "source_changed",
    )


async def test_explicit_cancel_is_receipted_and_fences_pending_work(db_session):
    actor_id = await _seed_actor(db_session)
    _, item_id = await _seed_image_item(
        db_session, actor_id=actor_id, width=None, height=None
    )
    source = _data(await _read(db_session, actor_id, item_id))["data"]
    _data(
        await _observe(
            db_session,
            actor_id,
            source,
            {"etag": "cancel-source", "byte_size": 2048},
            item_id=item_id,
        )
    )
    accepted = _data(
        await db_session.scalar(
            _PREPARE,
            {
                "operation_id": uuid4(),
                "actor_id": actor_id,
                "task_id": None,
                "item_id": item_id,
                "purpose": "dimensions",
                "snapshot": None,
                "policy": "{}",
                "force": False,
            },
        )
    )
    operation_id = uuid4()
    first = _data(
        await db_session.scalar(
            _CANCEL,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "job_id": accepted["work"]["job_id"],
            },
        )
    )
    assert first == {
        "operation_id": str(operation_id),
        "job_id": accepted["work"]["job_id"],
        "canceled": True,
    }
    replay = _data(
        await db_session.scalar(
            _CANCEL,
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "job_id": accepted["work"]["job_id"],
            },
        )
    )
    assert replay == first
    status = _data(
        await db_session.scalar(
            _READ_INTERNAL_WORK, {"job_id": accepted["work"]["job_id"]}
        )
    )
    assert status["work"]["status"] == "canceled"


async def test_schedule_backfill_freezes_cutoff_and_item_scan_pages(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id, item_id = await _seed_image_item(
        db_session, actor_id=actor_id, width=100, height=100
    )
    source = _data(await _read(db_session, actor_id, item_id))["data"]
    _data(
        await _observe(
            db_session,
            actor_id,
            source,
            {"etag": "scan-source", "byte_size": 2048},
            item_id=item_id,
        )
    )
    operation_id = uuid4()
    spec = {
        "kind": "image_backfill",
        "selection": {
            "owner_kind": "dataset_item",
            "dataset_id": str(dataset_id),
            "project_id": None,
            "owner_ids": [],
            "all_owners": False,
            "purpose": "thumbnail",
            "only_missing": True,
            "accepted_before": None,
        },
    }
    accepted = _data(
        await db_session.scalar(
            _SCHEDULE,
            {
                "operation_id": operation_id,
                "owner_kind": None,
                "owner_id": None,
                "spec": json.dumps(spec),
                "force": False,
            },
        )
    )
    assert accepted["operation_id"] == str(operation_id)
    assert accepted["source"] is None
    assert accepted["work"]["owner"] is None
    assert accepted["spec"]["selection"]["accepted_before"] is not None
    replay = _data(
        await db_session.scalar(
            _SCHEDULE,
            {
                "operation_id": operation_id,
                "owner_kind": None,
                "owner_id": None,
                "spec": json.dumps(spec),
                "force": False,
            },
        )
    )
    assert replay == accepted

    cutoff = await db_session.scalar(text("SELECT clock_timestamp()"))
    selection = {
        "owner_kind": "dataset_item",
        "dataset_id": str(dataset_id),
        "project_id": None,
        "owner_ids": [],
        "all_owners": False,
        "purpose": "thumbnail",
        "only_missing": True,
        "accepted_before": cutoff.isoformat(),
    }

    async def scan(after_kind=None, after_id=None):
        return _data(
            await db_session.scalar(
                _SCAN,
                {
                    "selection": json.dumps(selection),
                    "after_kind": after_kind,
                    "after_id": after_id,
                    "page_size": 1,
                },
            )
        )

    first = await scan()
    assert first["total"] == 1 and first["complete"] is True
    assert first["items"][0]["owner"] == {"kind": "dataset_item", "id": str(item_id)}
    assert (
        first["items"][0]["source_snapshot"]
        == _data(await _read(db_session, actor_id, item_id))["data"]["source_snapshot"]
    )


async def test_trusted_source_schedule_rejects_unbound_identity_without_receipt(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    _, item_id = await _seed_image_item(
        db_session, actor_id=actor_id, width=None, height=None
    )
    source = _data(await _read(db_session, actor_id, item_id))["data"]
    operation_id = uuid4()
    spec = {
        "kind": "dimensions",
        "expected_source_snapshot": source["source_snapshot"],
        "policy": {},
    }
    params = {
        "operation_id": operation_id,
        "owner_kind": "dataset_item",
        "owner_id": item_id,
        "spec": json.dumps(spec),
        "force": False,
    }
    _error(await db_session.scalar(_SCHEDULE, params), "source_not_ready")
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.asset_image_operations WHERE operation_id=:id"
            ),
            {"id": operation_id},
        )
        == 0
    )
    _data(
        await _observe(
            db_session,
            actor_id,
            source,
            {"etag": "trusted-schedule-source", "byte_size": 2048},
            item_id=item_id,
        )
    )
    accepted = _data(await db_session.scalar(_SCHEDULE, params))
    assert accepted["source"]["object_identity"]["etag"] == "trusted-schedule-source"
    assert accepted["spec"]["expected_source_snapshot"] == source["source_snapshot"]


async def test_all_task_backfill_status_requires_current_global_superadmin(db_session):
    actor_id = await _seed_actor(db_session, role="super_admin")
    project_admin = await _seed_actor(db_session, role="project_admin")
    selection = {
        "owner_kind": "task",
        "dataset_id": None,
        "project_id": None,
        "owner_ids": [],
        "all_owners": True,
        "purpose": "thumbnail",
        "only_missing": True,
        "accepted_before": None,
    }
    accepted = _data(
        await db_session.scalar(
            _SCHEDULE,
            {
                "operation_id": uuid4(),
                "owner_kind": None,
                "owner_id": None,
                "spec": json.dumps({"kind": "image_backfill", "selection": selection}),
                "force": False,
            },
        )
    )
    job_id = accepted["work"]["job_id"]
    _error(
        await db_session.scalar(
            _READ_WORK, {"actor_id": project_admin, "job_id": job_id}
        ),
        "permission_denied",
    )
    assert (
        _data(
            await db_session.scalar(
                _READ_WORK, {"actor_id": actor_id, "job_id": job_id}
            )
        )["work"]["job_id"]
        == job_id
    )


async def test_pyramid_admission_skips_reserved_orphan_generation(db_session):
    actor_id = await _seed_actor(db_session)
    _, item_id = await _seed_image_item(db_session, actor_id=actor_id)
    source = _data(await _read(db_session, actor_id, item_id))["data"]
    bound = _data(
        await _observe(
            db_session,
            actor_id,
            source,
            {"etag": "pyramid-source", "byte_size": 2048},
            item_id=item_id,
        )
    )["data"]
    asset_id = uuid4()
    await db_session.execute(
        text(
            "INSERT INTO public.image_pyramid_assets(id,dataset_item_id,profile_version) "
            "VALUES (:asset_id,:item_id,'pyramid-v1')"
        ),
        {"asset_id": asset_id, "item_id": item_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO public.asset_image_garbage(prefix,reason) "
            "VALUES (:prefix,'orphan_reserved')"
        ),
        {"prefix": f"image-pyramids/{asset_id}/g7/"},
    )
    accepted = _data(
        await db_session.scalar(
            _PREPARE,
            {
                "operation_id": uuid4(),
                "actor_id": actor_id,
                "task_id": None,
                "item_id": item_id,
                "purpose": "pyramid",
                "snapshot": bound["source_snapshot"],
                "policy": "{}",
                "force": False,
            },
        )
    )
    assert accepted["work"]["kind"] == "pyramid"
    row = (
        await db_session.execute(
            text(
                "SELECT asset.building_generation,generation.source_snapshot "
                "FROM public.image_pyramid_assets AS asset "
                "JOIN public.image_pyramid_generations AS generation "
                "ON generation.asset_id=asset.id AND generation.generation=asset.building_generation "
                "WHERE asset.id=:asset_id"
            ),
            {"asset_id": asset_id},
        )
    ).one()
    assert row.building_generation == 8
    assert row.source_snapshot == bound["source_snapshot"]


async def test_item_thumbnail_invalidation_detaches_before_queueing(db_session):
    actor_id = await _seed_actor(db_session)
    _, item_id = await _seed_image_item(
        db_session, actor_id=actor_id, width=100, height=100
    )
    source = _data(await _read(db_session, actor_id, item_id))["data"]
    bound = _data(
        await _observe(
            db_session,
            actor_id,
            source,
            {"etag": "thumbnail-source", "byte_size": 2048},
            item_id=item_id,
        )
    )["data"]
    key = f"image-assets/{uuid4()}/g1/a{uuid4()}/thumbnail.webp"
    await db_session.execute(
        text(
            "UPDATE public.dataset_items SET thumbnail_path=:key,blurhash='abc',"
            "media_materialized_generation=media_source_generation WHERE id=:item_id"
        ),
        {"item_id": item_id, "key": key},
    )
    source = _data(await _read(db_session, actor_id, item_id))["data"]
    invalidated = _data(
        await db_session.scalar(
            _INVALIDATE,
            {
                "actor_id": actor_id,
                "task_id": None,
                "item_id": item_id,
                "invalidation": json.dumps(
                    {"source_snapshot": bound["source_snapshot"], "kind": "thumbnail"}
                ),
            },
        )
    )
    assert invalidated["changed"] is True
    assert invalidated["source"]["thumbnail_path"] is None
    assert invalidated["source"]["source_snapshot"] == source["source_snapshot"]
    queued = await db_session.scalar(
        text(
            "SELECT count(*) FROM public.asset_image_garbage WHERE prefix=:prefix "
            "AND completed_at IS NULL"
        ),
        {"prefix": key.removesuffix("thumbnail.webp")},
    )
    assert queued == 1
