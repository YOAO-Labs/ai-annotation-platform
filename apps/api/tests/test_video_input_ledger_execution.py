"""Worker lease and retained-frame publication regressions for video inputs."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import text

from tests.test_video_input_authorities_native import (
    _reply,
    _seed_video_job,
    _seed_video_task,
    _selection,
)

pytestmark = pytest.mark.asyncio

_SCHEMA = "aap-assets-video-input-execution/v1"
_CAPTURE = text(
    "SELECT aap_assets_v1.capture_accepted_video_manifest(CAST(:job AS uuid),"
    "CAST(:actor AS uuid),CAST(:project AS uuid),CAST(:selection AS jsonb))"
)
_PAGE = text(
    "SELECT aap_assets_v1.read_accepted_video_manifest_page(CAST(:job AS uuid),"
    "CAST(:actor AS uuid),CAST(:project AS uuid),CAST(:selection_digest AS text),"
    "CAST(:sealed_digest AS text),CAST(:cursor AS bigint),CAST(:page_size AS integer))"
)
_PREPARE = text(
    "SELECT aap_assets_v1.prepare_accepted_video_member(CAST(:job AS uuid),"
    "CAST(:actor AS uuid),CAST(:project AS uuid),CAST(:selection_digest AS text),"
    "CAST(:ordinal AS bigint),CAST(:task AS uuid),CAST(:source_snapshot AS text),"
    "CAST(:timetable_revision AS bigint),CAST(:object_identity AS jsonb),CAST(:terminal_error AS text))"
)
_SEAL = text(
    "SELECT aap_assets_v1.seal_accepted_video_manifest(CAST(:job AS uuid),"
    "CAST(:actor AS uuid),CAST(:project AS uuid),CAST(:selection_digest AS text))"
)
_CLAIM_INPUT = text(
    "SELECT aap_assets_v1.claim_accepted_video_input(CAST(:worker AS uuid),"
    "CAST(:input AS uuid),CAST(:lease_seconds AS integer))"
)
_STAGE = text(
    "SELECT aap_assets_v1.stage_accepted_video_times(CAST(:input AS uuid),"
    "CAST(:generation AS bigint),CAST(:token AS uuid),CAST(:cursor AS bigint),CAST(:entries AS jsonb))"
)
_HEARTBEAT_INPUT = text(
    "SELECT aap_assets_v1.heartbeat_accepted_video_input(CAST(:input AS uuid),"
    "CAST(:generation AS bigint),CAST(:token AS uuid),CAST(:lease_seconds AS integer))"
)
_FINISH_INPUT = text(
    "SELECT aap_assets_v1.finish_accepted_video_input(CAST(:input AS uuid),"
    "CAST(:generation AS bigint),CAST(:token AS uuid),CAST(:plan AS jsonb))"
)
_CLAIM_FRAME = text(
    "SELECT aap_assets_v1.claim_accepted_video_frame(CAST(:worker AS uuid),"
    "CAST(:frame AS uuid),CAST(:lease_seconds AS integer))"
)
_REQUIRE_FRAME = text(
    "SELECT aap_assets_v1.require_accepted_video_frame(CAST(:actor AS uuid),"
    "CAST(:job AS uuid),CAST(:task AS uuid),CAST(:input AS uuid),CAST(:plan AS text),1)"
)
_HEARTBEAT_FRAME = text(
    "SELECT aap_assets_v1.heartbeat_accepted_video_frame(CAST(:frame AS uuid),"
    "CAST(:generation AS bigint),CAST(:token AS uuid),CAST(:lease_seconds AS integer))"
)
_FINISH_FRAME = text(
    "SELECT aap_assets_v1.finish_accepted_video_frame(CAST(:frame AS uuid),"
    "CAST(:generation AS bigint),CAST(:token AS uuid),CAST(:blob AS jsonb))"
)
_FAIL_INPUT = text(
    "SELECT aap_assets_v1.fail_accepted_video_input(CAST(:input AS uuid),"
    "CAST(:generation AS bigint),CAST(:token AS uuid),CAST(:error_code AS text))"
)
_FAIL_FRAME = text(
    "SELECT aap_assets_v1.fail_accepted_video_frame(CAST(:frame AS uuid),"
    "CAST(:generation AS bigint),CAST(:token AS uuid),CAST(:error_code AS text))"
)


def _video_data(value: object) -> dict:
    reply = _reply(value)
    assert reply.get("schema") == _SCHEMA, reply
    assert reply.get("ok") is True, reply.get("error")
    assert set(reply) == {"schema", "ok", "data"}
    return reply["data"]


def _video_error(value: object, code: str) -> None:
    assert _reply(value) == {
        "schema": _SCHEMA,
        "ok": False,
        "error": {"code": code},
    }


async def _seed_prepared_video_input(db):
    actor, project, _, item_id, task_id = await _seed_video_task(db)
    identity = {"etag": "origin-etag", "byte_size": 128, "version_id": "origin-v1"}
    await db.execute(
        text(
            "UPDATE public.dataset_items SET media_source_generation=1,"
            "media_timetable_revision=0,media_object_identity=CAST(:identity AS jsonb),"
            "media_source_drifted=false,file_size=128 WHERE id=:item"
        ),
        {"identity": json.dumps(identity), "item": item_id},
    )
    selection = _selection(task_ids=[str(task_id)])
    job_id = await _seed_video_job(
        db, actor_id=actor, project_id=project.id, selection=selection
    )
    header = _video_data(
        await db.scalar(
            _CAPTURE,
            {
                "job": job_id,
                "actor": actor,
                "project": project.id,
                "selection": json.dumps(selection),
            },
        )
    )
    await db.execute(
        text(
            "UPDATE public.async_jobs SET payload=pg_catalog.jsonb_set(payload,"
            "'{accepted_video_selection_digest}',pg_catalog.to_jsonb(CAST(:digest AS text)),true) "
            "WHERE id=:job"
        ),
        {"digest": header["selection_digest"], "job": job_id},
    )
    page = _video_data(
        await db.scalar(
            _PAGE,
            {
                "job": job_id,
                "actor": actor,
                "project": project.id,
                "selection_digest": header["selection_digest"],
                "sealed_digest": None,
                "cursor": 0,
                "page_size": 10,
            },
        )
    )
    member = page["members"][0]
    prepared = _video_data(
        await db.scalar(
            _PREPARE,
            {
                "job": job_id,
                "actor": actor,
                "project": project.id,
                "selection_digest": header["selection_digest"],
                "ordinal": member["ordinal"],
                "task": task_id,
                "source_snapshot": member["source"]["source_snapshot"],
                "timetable_revision": member["source"]["timetable_revision"],
                "object_identity": json.dumps(member["source"]["object_identity"]),
                "terminal_error": None,
            },
        )
    )
    assert prepared["member"]["state"] == "preparing"
    input_id = prepared["member"]["input"]["input_id"]
    claim = _video_data(
        await db.scalar(
            _CLAIM_INPUT,
            {"worker": uuid.uuid4(), "input": input_id, "lease_seconds": 600},
        )
    )["claim"]
    assert claim is not None
    return actor, project, job_id, selection, header, input_id, claim


def _frame_plan(claim: dict, selection: dict) -> tuple[dict, list[dict]]:
    video = {
        "frame_count": 3,
        "width": 640,
        "height": 360,
        "duration_ms": 1000,
        "video_stream": 0,
    }
    raw = {
        "input_id": claim["input"]["input_id"],
        "job_id": claim["input"]["job_id"],
        "actor_id": claim["input"]["actor_id"],
        "task": claim["input"]["task"],
        "source": claim["input"]["source"],
        "blob": {
            "key": claim["blob_key"],
            "version_id": "retained-v1",
            "etag": "retained-etag",
            "byte_size": claim["source"]["object_identity"]["byte_size"],
            "sha256": "a" * 64,
        },
        "video": video,
        "created_at": claim["input"]["created_at"],
    }
    entries = [
        {"ordinal": 1, "frame_index": 0, "pts_ms": -10, "is_keyframe": True},
        {"ordinal": 2, "frame_index": 1, "pts_ms": 0, "is_keyframe": False},
        {"ordinal": 3, "frame_index": 2, "pts_ms": 0, "is_keyframe": False},
    ]
    fields = (
        "aap-assets-video-inputs/v1",
        raw["input_id"],
        raw["job_id"],
        raw["actor_id"],
        raw["task"]["task_id"],
        raw["task"]["project_id"],
        str(raw["task"]["task_version"]),
        str(raw["task"]["task_source_generation"]),
        raw["task"]["task_source_snapshot"],
        raw["source"]["dataset_id"],
        raw["source"]["dataset_item_id"],
        str(raw["source"]["source_generation"]),
        raw["source"]["source_snapshot"],
        str(raw["source"]["timetable_revision"]),
        raw["blob"]["sha256"],
        str(raw["blob"]["byte_size"]),
        str(video["frame_count"]),
        str(video["width"]),
        str(video["height"]),
        str(video["duration_ms"]),
        str(video["video_stream"]),
        str(selection["sampling"]["max_frames"]),
        str(selection["sampling"]["step"]),
        str(selection["sampling"]["width"]),
        selection["sampling"]["format"],
    )
    seed = ("\n".join(fields) + "\n").encode("ascii")
    suffix = "".join(
        f"{entry['ordinal']}\n{entry['frame_index']}\n{entry['pts_ms']}\n{int(entry['is_keyframe'])}\n"
        for entry in entries
    )
    timebase_digest = hashlib.sha256(
        b"timebase\n" + seed + suffix.encode("ascii")
    ).hexdigest()
    plan_digest = hashlib.sha256(
        b"plan\n" + seed + timebase_digest.encode("ascii") + b"\n"
    ).hexdigest()
    return {
        "input": raw,
        "sampling": selection["sampling"],
        "selected_count": 3,
        "timebase_digest": timebase_digest,
        "plan_digest": plan_digest,
    }, entries


async def test_video_worker_leases_stage_replay_takeover_and_exact_frame_publication(
    db_session,
):
    (
        actor,
        project,
        job_id,
        selection,
        header,
        input_id,
        first_claim,
    ) = await _seed_prepared_video_input(db_session)
    first_generation = first_claim["generation"]
    first_token = first_claim["lease_token"]
    plan, entries = _frame_plan(first_claim, selection)
    _video_error(
        await db_session.scalar(
            _FAIL_INPUT,
            {
                "input": input_id,
                "generation": first_generation,
                "token": first_token,
                "error_code": "input_busy",
            },
        ),
        "invalid_request",
    )
    renewed = _video_data(
        await db_session.scalar(
            _HEARTBEAT_INPUT,
            {
                "input": input_id,
                "generation": first_generation,
                "token": first_token,
                "lease_seconds": 600,
            },
        )
    )
    assert renewed["lease_token"] == first_token

    malformed_page = [entries[0], {**entries[1], "is_keyframe": 0}]
    _video_error(
        await db_session.scalar(
            _STAGE,
            {
                "input": input_id,
                "generation": first_generation,
                "token": first_token,
                "cursor": 0,
                "entries": json.dumps(malformed_page),
            },
        ),
        "invalid_request",
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.asset_accepted_video_times WHERE input_id=:input"
            ),
            {"input": input_id},
        )
        == 0
    )

    first_page = [entries[0]]
    for _ in range(2):
        staged = _video_data(
            await db_session.scalar(
                _STAGE,
                {
                    "input": input_id,
                    "generation": first_generation,
                    "token": first_token,
                    "cursor": 0,
                    "entries": json.dumps(first_page),
                },
            )
        )
        assert staged["next_cursor"] == 1
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_inputs SET lease_expires_at=pg_catalog.clock_timestamp()-INTERVAL '1 second' "
            "WHERE input_id=:input"
        ),
        {"input": input_id},
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_input_attempts SET lease_expires_at=pg_catalog.clock_timestamp()-INTERVAL '1 second' "
            "WHERE input_id=:input AND generation=:generation"
        ),
        {"input": input_id, "generation": first_generation},
    )
    next_claim = _video_data(
        await db_session.scalar(
            _CLAIM_INPUT,
            {"worker": uuid.uuid4(), "input": input_id, "lease_seconds": 600},
        )
    )["claim"]
    assert next_claim is not None
    assert next_claim["generation"] == first_generation + 1
    assert next_claim["lease_token"] != first_token
    _video_error(
        await db_session.scalar(
            _HEARTBEAT_INPUT,
            {
                "input": input_id,
                "generation": first_generation,
                "token": first_token,
                "lease_seconds": 600,
            },
        ),
        "lease_lost",
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.asset_accepted_video_times WHERE input_id=:input AND generation=:generation"
            ),
            {"input": input_id, "generation": first_generation},
        )
        == 1
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT staged_count FROM public.asset_accepted_video_inputs WHERE input_id=:input"
            ),
            {"input": input_id},
        )
        == 0
    )
    _video_error(
        await db_session.scalar(
            _STAGE,
            {
                "input": input_id,
                "generation": first_generation,
                "token": first_token,
                "cursor": 1,
                "entries": json.dumps(entries[1:]),
            },
        ),
        "lease_lost",
    )

    staged = _video_data(
        await db_session.scalar(
            _STAGE,
            {
                "input": input_id,
                "generation": next_claim["generation"],
                "token": next_claim["lease_token"],
                "cursor": 0,
                "entries": json.dumps(entries),
            },
        )
    )
    assert staged["next_cursor"] == 3
    plan, _ = _frame_plan(next_claim, selection)
    changed_plan = json.loads(json.dumps(plan))
    changed_plan["timebase_digest"] = "f" * 64
    _video_error(
        await db_session.scalar(
            _FINISH_INPUT,
            {
                "input": input_id,
                "generation": next_claim["generation"],
                "token": next_claim["lease_token"],
                "plan": json.dumps(changed_plan),
            },
        ),
        "input_corrupt",
    )
    missing_version = json.loads(json.dumps(plan))
    missing_version["input"]["blob"]["version_id"] = "null"
    _video_error(
        await db_session.scalar(
            _FINISH_INPUT,
            {
                "input": input_id,
                "generation": next_claim["generation"],
                "token": next_claim["lease_token"],
                "plan": json.dumps(missing_version),
            },
        ),
        "versioning_required",
    )
    wrong_created_at = json.loads(json.dumps(plan))
    wrong_created_at["input"]["created_at"] = "2000-01-01T00:00:00Z"
    _video_error(
        await db_session.scalar(
            _FINISH_INPUT,
            {
                "input": input_id,
                "generation": next_claim["generation"],
                "token": next_claim["lease_token"],
                "plan": json.dumps(wrong_created_at),
            },
        ),
        "input_scope_mismatch",
    )
    finished = _video_data(
        await db_session.scalar(
            _FINISH_INPUT,
            {
                "input": input_id,
                "generation": next_claim["generation"],
                "token": next_claim["lease_token"],
                "plan": json.dumps(plan),
            },
        )
    )
    assert finished["status"] == "ready"
    assert (
        _video_data(
            await db_session.scalar(
                _FINISH_INPUT,
                {
                    "input": input_id,
                    "generation": next_claim["generation"],
                    "token": next_claim["lease_token"],
                    "plan": json.dumps(plan),
                },
            )
        )
        == finished
    )
    sealed = _video_data(
        await db_session.scalar(
            _SEAL,
            {
                "job": job_id,
                "actor": actor,
                "project": project.id,
                "selection_digest": header["selection_digest"],
            },
        )
    )
    assert sealed["status"] == "sealed"
    await db_session.execute(
        text("UPDATE public.async_jobs SET status='completed' WHERE id=:job"),
        {"job": job_id},
    )
    assert (
        _video_data(
            await db_session.scalar(
                _FINISH_INPUT,
                {
                    "input": input_id,
                    "generation": next_claim["generation"],
                    "token": next_claim["lease_token"],
                    "plan": json.dumps(plan),
                },
            )
        )
        == finished
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT pg_catalog.count(*) FROM public.asset_accepted_video_frames WHERE input_id=:input"
            ),
            {"input": input_id},
        )
        == 0
    )
    required = _video_data(
        await db_session.scalar(
            _REQUIRE_FRAME,
            {
                "actor": actor,
                "job": job_id,
                "task": finished["task"]["task_id"],
                "input": input_id,
                "plan": plan["plan_digest"],
            },
        )
    )
    frame_id = required["frame_id"]
    frame_claim = _video_data(
        await db_session.scalar(
            _CLAIM_FRAME,
            {"worker": uuid.uuid4(), "frame": frame_id, "lease_seconds": 600},
        )
    )["claim"]
    assert frame_claim is not None
    _video_error(
        await db_session.scalar(
            _FAIL_FRAME,
            {
                "frame": frame_id,
                "generation": frame_claim["generation"],
                "token": frame_claim["lease_token"],
                "error_code": "frame_busy",
            },
        ),
        "invalid_request",
    )
    frame_heartbeat = _video_data(
        await db_session.scalar(
            _HEARTBEAT_FRAME,
            {
                "frame": frame_id,
                "generation": frame_claim["generation"],
                "token": frame_claim["lease_token"],
                "lease_seconds": 600,
            },
        )
    )
    assert frame_heartbeat["lease_token"] == frame_claim["lease_token"]
    blob = {
        "key": frame_claim["blob_key"],
        "version_id": "frame-v1",
        "etag": "frame-etag",
        "byte_size": 17,
        "sha256": "b" * 64,
    }
    missing_frame_version = {**blob, "version_id": "null"}
    _video_error(
        await db_session.scalar(
            _FINISH_FRAME,
            {
                "frame": frame_id,
                "generation": frame_claim["generation"],
                "token": frame_claim["lease_token"],
                "blob": json.dumps(missing_frame_version),
            },
        ),
        "versioning_required",
    )
    frame_record = _video_data(
        await db_session.scalar(
            _FINISH_FRAME,
            {
                "frame": frame_id,
                "generation": frame_claim["generation"],
                "token": frame_claim["lease_token"],
                "blob": json.dumps(blob),
            },
        )
    )
    assert frame_record["status"] == "ready"
    assert frame_record["blob"] == blob
    assert (
        _video_data(
            await db_session.scalar(
                _FINISH_FRAME,
                {
                    "frame": frame_id,
                    "generation": frame_claim["generation"],
                    "token": frame_claim["lease_token"],
                    "blob": json.dumps(blob),
                },
            )
        )
        == frame_record
    )


async def test_video_worker_claim_stops_cancelled_job_and_source_aba(db_session):
    _, _, job_id, _, _, input_id, active_claim = await _seed_prepared_video_input(
        db_session
    )
    await db_session.execute(
        text("UPDATE public.async_jobs SET status='cancelled' WHERE id=:job"),
        {"job": job_id},
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_inputs SET lease_expires_at=pg_catalog.clock_timestamp()-INTERVAL '1 second' "
            "WHERE input_id=:input"
        ),
        {"input": input_id},
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_input_attempts SET lease_expires_at=pg_catalog.clock_timestamp()-INTERVAL '1 second' "
            "WHERE input_id=:input AND generation=:generation"
        ),
        {"input": input_id, "generation": active_claim["generation"]},
    )
    claimed = _video_data(
        await db_session.scalar(
            _CLAIM_INPUT,
            {"worker": uuid.uuid4(), "input": input_id, "lease_seconds": 600},
        )
    )
    assert claimed["claim"] is None
    row = await db_session.execute(
        text(
            "SELECT status,error_code FROM public.asset_accepted_video_inputs WHERE input_id=:input"
        ),
        {"input": input_id},
    )
    assert row.one() == ("failed", "job_not_active")

    actor2, project2, _, _, task2 = await _seed_video_task(db_session)
    item2 = await db_session.scalar(
        text("SELECT dataset_item_id FROM public.tasks WHERE id=:task"), {"task": task2}
    )
    await db_session.execute(
        text(
            "UPDATE public.dataset_items SET media_source_generation=1,media_timetable_revision=0,"
            "media_object_identity=CAST(:identity AS jsonb),media_source_drifted=false,file_size=128 WHERE id=:item"
        ),
        {
            "identity": json.dumps(
                {"etag": "origin-etag", "byte_size": 128, "version_id": "origin-v1"}
            ),
            "item": item2,
        },
    )
    selection2 = _selection(task_ids=[str(task2)])
    job2 = await _seed_video_job(
        db_session, actor_id=actor2, project_id=project2.id, selection=selection2
    )
    header2 = _video_data(
        await db_session.scalar(
            _CAPTURE,
            {
                "job": job2,
                "actor": actor2,
                "project": project2.id,
                "selection": json.dumps(selection2),
            },
        )
    )
    await db_session.execute(
        text(
            "UPDATE public.async_jobs SET payload=pg_catalog.jsonb_set(payload,"
            "'{accepted_video_selection_digest}',pg_catalog.to_jsonb(CAST(:digest AS text)),true) WHERE id=:job"
        ),
        {"digest": header2["selection_digest"], "job": job2},
    )
    page = _video_data(
        await db_session.scalar(
            _PAGE,
            {
                "job": job2,
                "actor": actor2,
                "project": project2.id,
                "selection_digest": header2["selection_digest"],
                "sealed_digest": None,
                "cursor": 0,
                "page_size": 10,
            },
        )
    )
    member = page["members"][0]
    prepared = _video_data(
        await db_session.scalar(
            _PREPARE,
            {
                "job": job2,
                "actor": actor2,
                "project": project2.id,
                "selection_digest": header2["selection_digest"],
                "ordinal": 1,
                "task": task2,
                "source_snapshot": member["source"]["source_snapshot"],
                "timetable_revision": member["source"]["timetable_revision"],
                "object_identity": json.dumps(member["source"]["object_identity"]),
                "terminal_error": None,
            },
        )
    )
    input2 = prepared["member"]["input"]["input_id"]
    path = await db_session.scalar(
        text("SELECT file_path FROM public.tasks WHERE id=:task"), {"task": task2}
    )
    await db_session.execute(
        text("UPDATE public.tasks SET file_path='temporary/aba.mp4' WHERE id=:task"),
        {"task": task2},
    )
    await db_session.execute(
        text("UPDATE public.tasks SET file_path=:path WHERE id=:task"),
        {"path": path, "task": task2},
    )
    stopped = _video_data(
        await db_session.scalar(
            _CLAIM_INPUT,
            {"worker": uuid.uuid4(), "input": input2, "lease_seconds": 600},
        )
    )
    assert stopped["claim"] is None
    row = await db_session.execute(
        text(
            "SELECT status,error_code FROM public.asset_accepted_video_inputs WHERE input_id=:input"
        ),
        {"input": input2},
    )
    status, error = row.one()
    assert status == "failed"
    assert error in {"source_changed", "task_scope_changed"}
