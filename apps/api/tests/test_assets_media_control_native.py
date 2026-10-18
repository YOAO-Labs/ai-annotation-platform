"""Focused native tests for the Assets media authorization/control boundary."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset
from tests.factory import create_project

pytestmark = pytest.mark.asyncio

_READ_SOURCE = text(
    "SELECT aap_assets_v1.read_media_source("
    "CAST(:actor AS uuid), CAST(:item AS uuid), CAST(:task AS uuid))"
)
_READ_SUMMARIES = text(
    "SELECT aap_assets_v1.read_media_summaries(CAST(:items AS uuid[]))"
)
_ENSURE = text(
    "SELECT aap_assets_v1.ensure_video_media("
    "CAST(:actor AS uuid), CAST(:item AS uuid), CAST(:task AS uuid), "
    "CAST(:selection AS jsonb))"
)
_REQUEST = text(
    "SELECT aap_assets_v1.request_video_media("
    "CAST(:operation AS uuid), CAST(:actor AS uuid), CAST(:item AS uuid), "
    "CAST(:spec AS jsonb), CAST(:force AS boolean))"
)
_SCHEDULE = text(
    "SELECT aap_assets_v1.schedule_video_media("
    "CAST(:operation AS uuid), CAST(:item AS uuid), CAST(:spec AS jsonb), "
    "CAST(:force AS boolean))"
)
_FAILURES = text(
    "SELECT aap_assets_v1.read_video_failures("
    "CAST(:actor AS uuid), CAST(:row_limit AS integer), CAST(:row_offset AS bigint))"
)
_READ_SAMPLES = text(
    "SELECT aap_assets_v1.read_video_samples("
    "CAST(:actor AS uuid), CAST(:item AS uuid), CAST(:task AS uuid), "
    "CAST(:chunk AS integer), CAST(:after_sample AS integer), "
    "CAST(:page_size AS integer))"
)


def _object(value: object) -> dict:
    parsed = json.loads(value) if isinstance(value, str) else value
    assert isinstance(parsed, dict)
    return parsed


def _error(reply: object, code: str) -> None:
    assert _object(reply) == {
        "schema": "aap-assets-media/v1",
        "ok": False,
        "error": {"code": code},
    }


async def _seed_item(
    db_session,
    *,
    actor_id,
    file_type: str,
    name: str,
    metadata: dict | None = None,
):
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        name=f"media-{name}-{uuid4().hex[:8]}",
        data_type=file_type,
    )
    item_id = uuid4()
    db_session.add(
        DatasetItem(
            id=item_id,
            dataset_id=dataset_id,
            file_name=f"{name}.{'mp4' if file_type == 'video' else 'jpg'}",
            file_path=f"native-test/{name}/{item_id}",
            file_type=file_type,
            file_size=1,
            content_hash="a" * 64,
            metadata_=metadata or {},
        )
    )
    await db_session.flush()
    return dataset_id, item_id


async def test_media_source_rechecks_active_actor_and_exact_task_item_link(db_session):
    actor_id = await _seed_actor(db_session)
    _, item_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="image",
        name="original-image",
        metadata={"opaque": {"retained": True}},
    )

    source = _object(
        await db_session.scalar(
            _READ_SOURCE, {"actor": actor_id, "item": item_id, "task": None}
        )
    )
    assert source["schema"] == "aap-assets-media/v1"
    assert source["ok"] is True
    assert source["data"]["dataset_item_id"] == str(item_id)
    assert source["data"]["file_type"] == "image"

    _error(
        await db_session.scalar(
            _READ_SOURCE,
            {"actor": actor_id, "item": item_id, "task": uuid4()},
        ),
        "task_not_visible",
    )
    await db_session.execute(
        text("UPDATE public.users SET is_active=false WHERE id=:actor"),
        {"actor": actor_id},
    )
    _error(
        await db_session.scalar(
            _READ_SOURCE, {"actor": actor_id, "item": item_id, "task": None}
        ),
        "account_not_active",
    )


async def test_media_summaries_project_only_video_metadata_and_preserve_request_order(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    _, video_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="video",
        name="summary-video",
        metadata={
            "private_ingest": {"token": "not-a-video-summary"},
            "video": {"duration_ms": 1200, "codec": "h264"},
        },
    )
    _, image_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="image",
        name="summary-image",
        metadata={"private_ingest": {"token": "not-an-image-summary"}},
    )

    reply = _object(
        await db_session.scalar(
            _READ_SUMMARIES,
            {"items": [video_id, image_id, video_id]},
        )
    )
    assert reply["schema"] == "aap-assets-media/v1"
    assert reply["ok"] is True
    items = reply["data"]["items"]
    assert [item["dataset_item_id"] for item in items] == [
        str(video_id),
        str(image_id),
        str(video_id),
    ]
    assert items[0]["metadata"] == {"duration_ms": 1200, "codec": "h264"}
    assert items[1]["metadata"] == {}
    assert items[2]["metadata"] == items[0]["metadata"]


async def test_ensure_video_media_fences_stale_source_before_creating_cache_or_work(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    _, item_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="video",
        name="source-cas",
        metadata={"video": {"frame_count": 3, "fps": 30, "width": 640, "height": 360}},
    )
    project = await create_project(
        db_session,
        owner_id=actor_id,
        name=f"media-source-cas-{uuid4().hex[:8]}",
        type_key="video",
    )
    task_id = uuid4()
    db_session.add(
        Task(
            id=task_id,
            project_id=project.id,
            dataset_item_id=item_id,
            display_id=f"T-MEDIA-{task_id.hex[:12]}",
            file_name="source-cas.mp4",
            file_path=f"native-test/{item_id}",
            file_type="video",
        )
    )
    await db_session.flush()
    await db_session.execute(
        text(
            "UPDATE public.dataset_items "
            "SET media_materialized_generation=media_source_generation WHERE id=:item"
        ),
        {"item": item_id},
    )
    source_reply = _object(
        await db_session.scalar(
            _READ_SOURCE, {"actor": actor_id, "item": item_id, "task": task_id}
        )
    )
    assert source_reply["ok"] is True
    snapshot = source_reply["data"]["source_snapshot"]
    selection = {
        "kind": "frames",
        "intent": "access",
        "expected_source_snapshot": "0" * 64,
        "frame_indices": [0],
        "width": 512,
        "format": "webp",
        "force": False,
        "failed_only": False,
    }
    stale = await db_session.scalar(
        _ENSURE,
        {
            "actor": actor_id,
            "item": item_id,
            "task": task_id,
            "selection": json.dumps(selection),
        },
    )
    _error(stale, "source_changed")
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.video_frame_cache "
                "WHERE dataset_item_id=:item"
            ),
            {"item": item_id},
        )
        == 0
    )

    selection["expected_source_snapshot"] = snapshot
    accepted = _object(
        await db_session.scalar(
            _ENSURE,
            {
                "actor": actor_id,
                "item": item_id,
                "task": None,
                "selection": json.dumps(selection),
            },
        )
    )
    assert accepted["schema"] == "aap-assets-media/v1"
    assert accepted["ok"] is True
    assert accepted["data"]["source"]["source_snapshot"] == snapshot
    assert len(accepted["data"]["assets"]) == 1
    assert accepted["data"]["assets"][0]["kind"] == "frame"
    assert accepted["data"]["assets"][0]["status"] == "pending"


async def test_manager_request_replays_receipt_while_implicit_schedule_does_not_add_one(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="super_admin")
    _, item_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="video",
        name="request-receipt",
        metadata={"video": {"frame_count": 3}},
    )
    operation_id = uuid4()
    spec = {
        "kind": "video_metadata",
        "generate_playback": True,
        "generate_poster": True,
        "probe_timetable": True,
        "expected_source_snapshot": None,
    }
    params = {
        "operation": operation_id,
        "actor": actor_id,
        "item": item_id,
        "spec": json.dumps(spec),
        "force": False,
    }
    first = _object(await db_session.scalar(_REQUEST, params))
    replay = _object(await db_session.scalar(_REQUEST, params))
    assert first["ok"] is True
    assert replay == first
    assert first["data"]["operation_id"] == str(operation_id)
    _error(
        await db_session.scalar(_REQUEST, {**params, "force": True}),
        "operation_conflict",
    )

    count_before = await db_session.scalar(
        text("SELECT count(*) FROM public.asset_media_operations")
    )
    implicit = _object(
        await db_session.scalar(
            _SCHEDULE,
            {
                "operation": None,
                "item": item_id,
                "spec": json.dumps(spec),
                "force": False,
            },
        )
    )
    count_after = await db_session.scalar(
        text("SELECT count(*) FROM public.asset_media_operations")
    )
    assert implicit["ok"] is True
    assert implicit["data"]["operation_id"] is None
    assert count_after == count_before


async def test_chunk_samples_page_in_packet_order_with_unsorted_b_frame_ranks(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    _, item_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="video",
        name="b-frame-samples",
        metadata={"video": {"frame_count": 4, "fps": 30, "width": 640, "height": 360}},
    )
    project = await create_project(
        db_session,
        owner_id=actor_id,
        name=f"media-b-frame-{uuid4().hex[:8]}",
        type_key="video",
    )
    task_id = uuid4()
    db_session.add(
        Task(
            id=task_id,
            project_id=project.id,
            dataset_item_id=item_id,
            display_id=f"T-MEDIA-{task_id.hex[:12]}",
            file_name="b-frame-samples.mp4",
            file_path=f"native-test/{item_id}",
            file_type="video",
        )
    )
    await db_session.flush()
    await db_session.execute(
        text(
            "UPDATE public.dataset_items "
            "SET media_materialized_generation=media_source_generation WHERE id=:item"
        ),
        {"item": item_id},
    )
    samples = [
        {
            "frame_index": 0,
            "pts_ms": 0,
            "duration_ms": 40,
            "is_keyframe": True,
            "size_bytes": 10,
            "offset_in_chunk": 0,
        },
        {
            "frame_index": 3,
            "pts_ms": 100,
            "duration_ms": 40,
            "is_keyframe": False,
            "size_bytes": 11,
            "offset_in_chunk": 10,
        },
        {
            "frame_index": 1,
            "pts_ms": 33,
            "duration_ms": 40,
            "is_keyframe": False,
            "size_bytes": 12,
            "offset_in_chunk": 21,
        },
        {
            "frame_index": 2,
            "pts_ms": 66,
            "duration_ms": 40,
            "is_keyframe": False,
            "size_bytes": 13,
            "offset_in_chunk": 33,
        },
    ]
    await db_session.execute(
        text(
            "INSERT INTO public.video_chunks "
            "(id,dataset_item_id,chunk_id,start_frame,end_frame,storage_key,byte_size,"
            "status,diagnostics,source_generation,timetable_revision,"
            "created_at,updated_at) "
            "VALUES (:id,:item,0,0,3,'media-assets/test/chunk.mp4',50,'ready',"
            "CAST(:diagnostics AS jsonb),1,0,now(),now())"
        ),
        {
            "id": uuid4(),
            "item": item_id,
            "diagnostics": json.dumps(
                {
                    "codec_string": "avc1.640028",
                    "description": None,
                    "width": 640,
                    "height": 360,
                    "samples": samples,
                }
            ),
        },
    )
    params = {
        "actor": actor_id,
        "item": item_id,
        "task": task_id,
        "chunk": 0,
        "after_sample": None,
        "page_size": 2,
    }
    first = _object(await db_session.scalar(_READ_SAMPLES, params))
    first_data = first["data"]
    assert first["ok"] is True
    assert [row["frame_index"] for row in first_data["samples"]] == [0, 3]
    assert first_data["next_sample"] == 1
    second = _object(
        await db_session.scalar(_READ_SAMPLES, {**params, "after_sample": 1})
    )
    second_data = second["data"]
    assert second["ok"] is True
    assert [row["frame_index"] for row in second_data["samples"]] == [1, 2]
    assert second_data["complete"] is True
    assert second_data["next_sample"] is None
    assert second_data["snapshot_digest"] == first_data["snapshot_digest"]


async def test_video_failure_page_requires_manager_and_returns_current_errors(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="super_admin")
    _, item_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="video",
        name="failure-list",
        metadata={"video": {"probe_error": "probe failed"}},
    )
    await db_session.execute(
        text(
            "INSERT INTO public.video_frame_cache "
            "(id,dataset_item_id,frame_index,width,format,status,error,source_generation,"
            "timetable_revision,created_at,updated_at) "
            "VALUES (:id,:item,0,512,'webp','failed','frame failed',1,0,now(),now())"
        ),
        {"id": uuid4(), "item": item_id},
    )
    denied_actor = await _seed_actor(db_session, role="employee")
    _error(
        await db_session.scalar(
            _FAILURES,
            {"actor": denied_actor, "row_limit": 50, "row_offset": 0},
        ),
        "permission_denied",
    )
    page = _object(
        await db_session.scalar(
            _FAILURES,
            {"actor": actor_id, "row_limit": 50, "row_offset": 0},
        )
    )
    assert page["ok"] is True
    assert page["data"]["total"] == 2
    assert {row["asset_type"] for row in page["data"]["items"]} == {"probe", "frame"}
    assert {row["dataset_item_id"] for row in page["data"]["items"]} == {str(item_id)}
