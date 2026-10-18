"""Focused native tests for the Assets media authorization/control boundary."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

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
_READ_VIDEO_TOTALS = text(
    "SELECT aap_assets_v1.read_video_media_totals(CAST(:items AS uuid[]))"
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


async def test_media_preferences_preserve_missing_zero_and_current_settings(db_session):
    from aap_assets_media.contract import MediaFailure, UsageRequest
    from app.db.models.system_setting import SystemSetting
    from app.services.assets_media import SqlAlchemyMediaDatabase

    binding = SqlAlchemyMediaDatabase(db_session, commit=False)
    missing = await binding.call("read_media_preferences", UsageRequest())
    assert missing.warmup_lookahead is None
    setting = SystemSetting(
        key="video_chunk_warmup_lookahead", value_type="int", value_json=2
    )
    db_session.add(setting)
    await db_session.flush()
    current = await binding.call("read_media_preferences", UsageRequest())
    assert current.warmup_lookahead == 2
    setting.value_json = 0
    await db_session.flush()
    disabled = await binding.call("read_media_preferences", UsageRequest())
    assert disabled.warmup_lookahead == 0
    setting.value_type = "str"
    setting.value_json = "2"
    await db_session.flush()
    with pytest.raises(MediaFailure, match="invalid_response"):
        await binding.call("read_media_preferences", UsageRequest())


async def test_nonempty_timetable_and_backfill_pages_preserve_cursor_and_digest(
    db_session,
):
    from aap_assets_media.contract import (
        BackfillRequest,
        BackfillSelection,
        TimetableRequest,
    )
    from app.db.models.dataset import VideoFrameIndex
    from app.services.assets_media import SqlAlchemyMediaDatabase

    actor_id = await _seed_actor(db_session)
    dataset_id, item_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="video",
        name="paged-timetable",
        metadata={"video": {"frame_count": 4, "fps": 30}},
    )
    other_id = uuid4()
    db_session.add(
        DatasetItem(
            id=other_id,
            dataset_id=dataset_id,
            file_name="second.mp4",
            file_path=f"native-test/{other_id}",
            file_type="video",
            metadata_={},
        )
    )
    project = await create_project(db_session, owner_id=actor_id)
    task = Task(
        project_id=project.id,
        dataset_item_id=item_id,
        display_id=f"T-PAGED-{uuid4().hex[:12]}",
        file_name="paged.mp4",
        file_path=f"native-test/{item_id}",
        file_type="video",
        status="pending",
    )
    db_session.add(task)
    db_session.add_all(
        [
            VideoFrameIndex(
                dataset_item_id=item_id,
                frame_index=i,
                pts_ms=i * 33 - 20,
                is_keyframe=i == 0,
            )
            for i in range(4)
        ]
    )
    await db_session.flush()
    await db_session.execute(
        text(
            "UPDATE public.dataset_items SET media_materialized_generation=media_source_generation,media_timetable_revision=1 WHERE id=:id"
        ),
        {"id": item_id},
    )
    binding = SqlAlchemyMediaDatabase(db_session, commit=False)
    request = TimetableRequest(
        actor_id=actor_id, dataset_item_id=item_id, task_id=task.id, page_size=2
    )
    first = await binding.call("read_video_timetable", request)
    second = await binding.call(
        "read_video_timetable",
        request.model_copy(update={"after_frame": first.next_frame}),
    )
    assert [frame.frame_index for frame in first.frames + second.frames] == [0, 1, 2, 3]
    assert first.next_frame == 1 and not first.complete
    assert second.complete and second.next_frame is None
    assert first.total == second.total == 4
    assert first.snapshot_digest == second.snapshot_digest
    cutoff = await db_session.scalar(text("SELECT clock_timestamp()"))
    selection = BackfillSelection(
        dataset_id=dataset_id, only_missing=False, accepted_before=cutoff
    )
    request = BackfillRequest(selection=selection, page_size=1)
    first = await binding.call("scan_video_backfill", request)
    second = await binding.call(
        "scan_video_backfill",
        request.model_copy(update={"after_item_id": first.next_item_id}),
    )
    assert {source.dataset_item_id for source in first.items + second.items} == {
        item_id,
        other_id,
    }
    assert first.next_item_id is not None and not first.complete
    assert second.next_item_id is None and second.complete
    assert first.total == second.total == 2
    assert first.snapshot_digest == second.snapshot_digest


async def test_task_retry_serializes_task_changes_until_acceptance_transaction_ends(
    test_engine,
):
    from aap_assets_media.contract import FrameSelection

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = dataset_id = project_id = item_id = task_id = None
    try:
        async with maker.begin() as owner:
            actor_id = await _seed_actor(owner)
            dataset_id, item_id = await _seed_item(
                owner,
                actor_id=actor_id,
                file_type="video",
                name="retry-lock",
                metadata={"video": {"frame_count": 2, "fps": 30}},
            )
            await owner.execute(
                text(
                    "UPDATE public.dataset_items SET media_materialized_generation=media_source_generation WHERE id=:id"
                ),
                {"id": item_id},
            )
            project = await create_project(owner, owner_id=actor_id)
            project_id = project.id
            task = Task(
                project_id=project_id,
                dataset_item_id=item_id,
                display_id=f"T-MEDIA-{uuid4().hex[:12]}",
                file_name="retry.mp4",
                file_path=f"native-test/{item_id}",
                file_type="video",
                status="pending",
            )
            owner.add(task)
            await owner.flush()
            task_id = task.id
        async with (
            test_engine.connect() as admission,
            test_engine.connect() as mutation,
        ):
            selection = FrameSelection(
                intent="task_frame_retry",
                frame_indices=[0],
                failed_only=True,
            )
            reply = _object(
                await admission.scalar(
                    _ENSURE,
                    {
                        "actor": actor_id,
                        "item": item_id,
                        "task": task_id,
                        "selection": selection.model_dump_json(),
                    },
                )
            )
            assert reply["ok"] is True
            with pytest.raises(DBAPIError) as blocked:
                await mutation.execute(
                    text("SELECT id FROM public.tasks WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": task_id},
                )
            assert getattr(blocked.value.orig, "sqlstate", None) == "55P03"
            await mutation.rollback()
            await admission.rollback()
            assert (
                await mutation.scalar(
                    text("SELECT id FROM public.tasks WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": task_id},
                )
                == task_id
            )
            await mutation.rollback()
    finally:
        if task_id is not None:
            async with test_engine.begin() as cleanup:
                for table, key in (
                    ("tasks", task_id),
                    ("projects", project_id),
                    ("dataset_items", item_id),
                    ("datasets", dataset_id),
                    ("users", actor_id),
                ):
                    await cleanup.execute(
                        text(f"DELETE FROM public.{table} WHERE id=:id"), {"id": key}
                    )


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


async def test_task_frame_retry_accepts_failed_only_and_forced_selection_shapes(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="super_admin")
    _, item_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="video",
        name="task-frame-retry",
        metadata={"video": {"frame_count": 3, "fps": 30, "width": 640, "height": 360}},
    )
    project = await create_project(
        db_session,
        owner_id=actor_id,
        name=f"media-frame-retry-{uuid4().hex[:8]}",
        type_key="video",
    )
    task_id = uuid4()
    db_session.add(
        Task(
            id=task_id,
            project_id=project.id,
            dataset_item_id=item_id,
            display_id=f"T-MEDIA-{task_id.hex[:12]}",
            file_name="task-frame-retry.mp4",
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
    source = _object(
        await db_session.scalar(
            _READ_SOURCE, {"actor": actor_id, "item": item_id, "task": task_id}
        )
    )
    assert source["ok"] is True
    snapshot = source["data"]["source_snapshot"]
    await db_session.execute(
        text(
            "INSERT INTO public.video_frame_cache "
            "(id,dataset_item_id,frame_index,width,format,status,error,source_generation,"
            "timetable_revision,created_at,updated_at) "
            "VALUES (:id,:item,0,512,'webp','failed','decode failed',1,0,now(),now())"
        ),
        {"id": uuid4(), "item": item_id},
    )
    selection = {
        "kind": "frames",
        "intent": "task_frame_retry",
        "expected_source_snapshot": snapshot,
        "frame_indices": [0],
        "width": 512,
        "format": "webp",
        "force": False,
        "failed_only": True,
    }
    params = {
        "actor": actor_id,
        "item": item_id,
        "task": task_id,
        "selection": json.dumps(selection),
    }
    failed_only = _object(await db_session.scalar(_ENSURE, params))
    assert failed_only["ok"] is True
    assert len(failed_only["data"]["assets"]) == 1
    first_asset = failed_only["data"]["assets"][0]
    assert first_asset["status"] == "failed"
    assert first_asset["job_id"] is not None
    assert first_asset["generation"] == 1

    forced_selection = {
        **selection,
        "force": True,
        "failed_only": False,
    }
    forced = _object(
        await db_session.scalar(
            _ENSURE,
            {**params, "selection": json.dumps(forced_selection)},
        )
    )
    assert forced["ok"] is True
    assert len(forced["data"]["assets"]) == 1
    forced_asset = forced["data"]["assets"][0]
    assert forced_asset["job_id"] == first_asset["job_id"]
    assert forced_asset["generation"] == 2
    assert forced_asset["status"] == "pending"

    unfiltered = _object(
        await db_session.scalar(
            _ENSURE,
            {
                **params,
                "selection": json.dumps(
                    {**selection, "force": False, "failed_only": False}
                ),
            },
        )
    )
    assert unfiltered["ok"] is True
    assert unfiltered["data"]["assets"][0]["generation"] == 2

    _error(
        await db_session.scalar(
            _ENSURE,
            {
                **params,
                "selection": json.dumps(
                    {**selection, "force": True, "failed_only": True}
                ),
            },
        ),
        "invalid_request",
    )


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


async def test_video_media_totals_weight_duplicates_and_exclude_unready_videos(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    _, ready_video_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="video",
        name="totals-ready-video",
        metadata={"video": {"duration_ms": 100, "frame_count": 8}},
    )
    _, unready_video_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="video",
        name="totals-unready-video",
        metadata={"video": {"duration_ms": 900, "frame_count": 90}},
    )
    _, drifted_video_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="video",
        name="totals-drifted-video",
        metadata={"video": {"duration_ms": 800, "frame_count": 80}},
    )
    _, legacy_image_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="image",
        name="totals-legacy-image",
        metadata={"video": {"duration_ms": "7", "frame_count": 3}},
    )
    _, nullable_image_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="image",
        name="totals-nullable-image",
        metadata={"video": {"duration_ms": None}},
    )
    await db_session.execute(
        text(
            "UPDATE public.dataset_items "
            "SET media_materialized_generation=media_source_generation "
            "WHERE id=:item"
        ),
        {"item": ready_video_id},
    )
    await db_session.execute(
        text(
            "UPDATE public.dataset_items "
            "SET media_materialized_generation=media_source_generation, "
            "media_source_drifted=true WHERE id=:item"
        ),
        {"item": drifted_video_id},
    )

    reply = _object(
        await db_session.scalar(
            _READ_VIDEO_TOTALS,
            {
                "items": [
                    ready_video_id,
                    ready_video_id,
                    unready_video_id,
                    drifted_video_id,
                    legacy_image_id,
                    nullable_image_id,
                    uuid4(),
                ]
            },
        )
    )

    assert reply == {
        "schema": "aap-assets-media/v1",
        "ok": True,
        "data": {"frame_count": 19, "duration_ms": 207, "matched_items": 6},
    }


async def test_video_media_totals_reject_invalid_known_numeric_metadata(db_session):
    actor_id = await _seed_actor(db_session)
    _, item_id = await _seed_item(
        db_session,
        actor_id=actor_id,
        file_type="video",
        name="totals-invalid-metadata",
        metadata={"video": {"duration_ms": "not-an-integer", "frame_count": 1}},
    )
    await db_session.execute(
        text(
            "UPDATE public.dataset_items "
            "SET media_materialized_generation=media_source_generation "
            "WHERE id=:item"
        ),
        {"item": item_id},
    )

    _error(
        await db_session.scalar(_READ_VIDEO_TOTALS, {"items": [item_id]}),
        "invalid_response",
    )
