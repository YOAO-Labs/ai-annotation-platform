"""Database regressions for immutable bulk image manifest capture and sealing."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.db.models.async_job import AsyncJob
from tests.factory import create_project, create_task
from tests.test_assets_catalog_native import _seed_actor

pytestmark = pytest.mark.asyncio

_CAPTURE = text(
    "SELECT aap_assets_v1.capture_accepted_image_input_manifest("
    "CAST(:job_id AS uuid), CAST(:actor_id AS uuid), CAST(:project_id AS uuid), "
    "CAST(:selection AS jsonb))"
)
_READ = text(
    "SELECT aap_assets_v1.read_accepted_image_input_manifest("
    "CAST(:job_id AS uuid), CAST(:actor_id AS uuid), CAST(:project_id AS uuid))"
)
_PAGE = text(
    "SELECT aap_assets_v1.read_accepted_image_input_manifest_page("
    "CAST(:job_id AS uuid), CAST(:actor_id AS uuid), CAST(:project_id AS uuid), "
    "CAST(:selection_digest AS text), CAST(:sealed_digest AS text), "
    "CAST(:cursor AS bigint), CAST(:page_size AS integer))"
)
_PREPARE = text(
    "SELECT aap_assets_v1.prepare_accepted_image_input_manifest_member("
    "CAST(:job_id AS uuid), CAST(:actor_id AS uuid), CAST(:project_id AS uuid), "
    "CAST(:selection_digest AS text), CAST(:ordinal AS bigint), "
    "CAST(:task_id AS uuid), CAST(:source_snapshot AS text), "
    "CAST(:object_identity AS jsonb), CAST(:terminal_error AS text))"
)
_CLAIM = text(
    "SELECT aap_assets_v1.claim_accepted_image_input("
    "CAST(:worker_id AS uuid), CAST(:input_id AS uuid), :lease_seconds)"
)
_FINISH = text(
    "SELECT aap_assets_v1.finish_accepted_image_input("
    "CAST(:input_id AS uuid), CAST(:generation AS bigint), "
    "CAST(:lease_token AS uuid), CAST(:blob AS jsonb))"
)
_SEAL = text(
    "SELECT aap_assets_v1.seal_accepted_image_input_manifest("
    "CAST(:job_id AS uuid), CAST(:actor_id AS uuid), CAST(:project_id AS uuid), "
    "CAST(:selection_digest AS text))"
)
_INSERT_MANIFEST = text(
    "INSERT INTO public.asset_accepted_image_input_manifests("
    "job_id,actor_id,project_id,selection,selection_digest,candidate_count,"
    "selected_count,prepared_count,status) VALUES ("
    "CAST(:job_id AS uuid),CAST(:actor_id AS uuid),CAST(:project_id AS uuid),"
    "CAST(:selection AS jsonb),:selection_digest,:count,:count,0,'preparing')"
)
_INSERT_MEMBER = text(
    "INSERT INTO public.asset_accepted_image_input_manifest_members("
    "job_id,ordinal,task_id,project_id,task_version,task_source_generation,"
    "task_source_snapshot,file_path,file_type,source,state) VALUES ("
    "CAST(:job_id AS uuid),:ordinal,CAST(:task_id AS uuid),"
    "CAST(:project_id AS uuid),1,1,:task_snapshot,:file_path,'image',"
    "CAST(:source AS jsonb),'unprepared')"
)


def _reply(value):
    return json.loads(value) if isinstance(value, str) else value


def _data(value: object) -> dict:
    reply = _reply(value)
    assert reply["schema"] == "aap-assets-input-manifest/v1", reply
    assert reply["ok"] is True, reply.get("error")
    return reply["data"]


async def _prepare(db_session, *, header, member, task_id, identity=None, error=None):
    return _data(
        await db_session.scalar(
            _PREPARE,
            {
                "job_id": header["job_id"],
                "actor_id": header["actor_id"],
                "project_id": header["project_id"],
                "selection_digest": header["selection_digest"],
                "ordinal": member["ordinal"],
                "task_id": task_id,
                "source_snapshot": (
                    member["source"]["source_snapshot"]
                    if member["source"] is not None
                    else None
                ),
                "object_identity": json.dumps(identity),
                "terminal_error": error,
            },
        )
    )


async def test_capture_replay_pages_and_seal_preserve_selected_bytes_and_failures(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    project = await create_project(db_session, owner_id=actor_id)
    tasks = [
        await create_task(db_session, project_id=project.id),
        await create_task(db_session, project_id=project.id),
    ]
    job_id = uuid4()
    selection = {
        "batch_id": None,
        "task_ids": None,
        "predict_mode": "skip_predicted",
    }
    db_session.add(
        AsyncJob(
            id=job_id,
            kind="batch_predict",
            project_id=project.id,
            user_id=actor_id,
            status="pending",
            celery_task_id=str(job_id),
            payload={
                "batch_id": None,
                "execution_scope": "bulk",
                "accepted_input_manifest_protocol": "aap-assets-input-manifest/v1",
                "accepted_input_manifest_id": str(job_id),
                "input_selection": selection,
            },
            result={},
        )
    )
    await db_session.flush()

    args = {
        "job_id": job_id,
        "actor_id": actor_id,
        "project_id": project.id,
        "selection": json.dumps(selection),
    }
    first = _data(await db_session.scalar(_CAPTURE, args))
    assert first["status"] == "preparing"
    assert first["candidate_count"] == first["selected_count"] == 2
    assert first["prepared_count"] == 0

    # Persist the same digest the API writes in the caller-owned Job transaction.
    job = await db_session.get(AsyncJob, job_id)
    job.payload = {
        **job.payload,
        "accepted_input_manifest_digest": first["selection_digest"],
        "total_tasks": 2,
        "input_candidate_count": 2,
    }
    late_task = await create_task(db_session, project_id=project.id)
    await db_session.flush()

    replay = _data(await db_session.scalar(_CAPTURE, args))
    assert replay == first
    read = _data(
        await db_session.scalar(
            _READ, {key: args[key] for key in ("job_id", "actor_id", "project_id")}
        )
    )
    assert read == first

    first_page = _data(
        await db_session.scalar(
            _PAGE,
            {
                "job_id": job_id,
                "actor_id": actor_id,
                "project_id": project.id,
                "selection_digest": first["selection_digest"],
                "sealed_digest": None,
                "cursor": 0,
                "page_size": 1,
            },
        )
    )
    second_page = _data(
        await db_session.scalar(
            _PAGE,
            {
                "job_id": job_id,
                "actor_id": actor_id,
                "project_id": project.id,
                "selection_digest": first["selection_digest"],
                "sealed_digest": None,
                "cursor": first_page["next_cursor"],
                "page_size": 1,
            },
        )
    )
    selected = first_page["members"] + second_page["members"]
    assert [member["task_id"] for member in selected] == sorted(
        (str(task.id) for task in tasks)
    )
    assert str(late_task.id) not in {member["task_id"] for member in selected}
    assert first_page["next_cursor"] == 1
    assert second_page["next_cursor"] is None
    assert all(member["state"] == "unprepared" for member in selected)

    observed = await _prepare(
        db_session,
        header=first,
        member=selected[0],
        task_id=selected[0]["task_id"],
        identity={"etag": "bulk-source-v1", "byte_size": 128},
    )
    assert observed["member"]["state"] == "preparing"
    assert observed["member"]["input"]["status"] == "pending"

    # A Task workflow/status change after capture becomes one terminal member
    # failure; it cannot replace the frozen selected Task with a live URL.
    tasks_by_id = {str(task.id): task for task in tasks}
    tasks_by_id[selected[1]["task_id"]].status = "in_progress"
    await db_session.flush()
    failed = await _prepare(
        db_session,
        header=first,
        member=selected[1],
        task_id=selected[1]["task_id"],
        error="task_scope_changed",
    )
    assert failed["member"]["state"] == "failed"
    assert failed["member"]["error"] == "task_scope_changed"

    claim = _data(
        await db_session.scalar(
            _CLAIM,
            {
                "worker_id": uuid4(),
                "input_id": observed["member"]["input"]["input_id"],
                "lease_seconds": 300,
            },
        )
    )["claim"]
    assert claim is not None
    blob = {
        "key": claim["blob_key"],
        "version_id": "accepted-input-version-1",
        "etag": "accepted-input-etag",
        "byte_size": 128,
        "sha256": "a" * 64,
    }
    finished = _reply(
        await db_session.scalar(
            _FINISH,
            {
                "input_id": claim["input"]["input_id"],
                "generation": claim["generation"],
                "lease_token": claim["lease_token"],
                "blob": json.dumps(blob),
            },
        )
    )
    assert finished["ok"] is True, finished

    sealed = _data(
        await db_session.scalar(
            _SEAL,
            {
                "job_id": job_id,
                "actor_id": actor_id,
                "project_id": project.id,
                "selection_digest": first["selection_digest"],
            },
        )
    )
    assert sealed["status"] == "sealed"
    assert sealed["prepared_count"] == sealed["selected_count"] == 2
    assert (sealed["ready_count"], sealed["failed_count"]) == (1, 1)
    assert sealed["sealed_digest"]

    sealed_page = _data(
        await db_session.scalar(
            _PAGE,
            {
                "job_id": job_id,
                "actor_id": actor_id,
                "project_id": project.id,
                "selection_digest": first["selection_digest"],
                "sealed_digest": sealed["sealed_digest"],
                "cursor": 0,
                "page_size": 100,
            },
        )
    )
    assert [member["state"] for member in sealed_page["members"]] == [
        "ready",
        "failed",
    ]
    assert sealed_page["manifest"] == sealed


async def test_page_stops_before_reply_budget_and_returns_a_resumable_cursor(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    project = await create_project(db_session, owner_id=actor_id)
    job_id = uuid4()
    selection = {"batch_id": None, "task_ids": None, "predict_mode": "append"}
    db_session.add(
        AsyncJob(
            id=job_id,
            kind="batch_predict",
            project_id=project.id,
            user_id=actor_id,
            status="pending",
            celery_task_id=str(job_id),
            payload={
                "batch_id": None,
                "execution_scope": "bulk",
                "accepted_input_manifest_protocol": "aap-assets-input-manifest/v1",
                "accepted_input_manifest_id": str(job_id),
                "input_selection": selection,
            },
            result={},
        )
    )
    await db_session.flush()

    await db_session.execute(
        _INSERT_MANIFEST,
        {
            "job_id": job_id,
            "actor_id": actor_id,
            "project_id": project.id,
            "selection": json.dumps(selection),
            "selection_digest": "b" * 64,
            "count": 100,
        },
    )
    control = chr(1)
    for ordinal in range(1, 101):
        task_id = uuid4()
        path = "a" + control * 999
        source = {
            "owner": {"kind": "task", "id": str(task_id)},
            "task_id": str(task_id),
            "project_id": str(project.id),
            "dataset_id": None,
            "item_id": None,
            "source_generation": 1,
            "source_snapshot": f"{ordinal:064x}",
            "object_identity": None,
            "source_drifted": False,
            "upload_ready": True,
            "file_path": path,
            "file_name": control * 500,
            "file_type": "image",
            "bucket_kind": "legacy",
            "file_size": None,
            "content_hash": None,
            "width": None,
            "height": None,
            "thumbnail_path": None,
            "blurhash": None,
            "thumbnail_source_generation": None,
            "thumbnail_error": control * 2000,
        }
        await db_session.execute(
            _INSERT_MEMBER,
            {
                "job_id": job_id,
                "ordinal": ordinal,
                "task_id": task_id,
                "project_id": project.id,
                "task_snapshot": f"{ordinal:064x}",
                "file_path": path,
                "source": json.dumps(source),
            },
        )

    page_reply = _reply(
        await db_session.scalar(
            _PAGE,
            {
                "job_id": job_id,
                "actor_id": actor_id,
                "project_id": project.id,
                "selection_digest": "b" * 64,
                "sealed_digest": None,
                "cursor": 0,
                "page_size": 100,
            },
        )
    )
    assert page_reply["ok"] is True, page_reply.get("error")
    page = page_reply["data"]
    encoded = json.dumps(page_reply, ensure_ascii=False).encode("utf-8")
    assert len(page["members"]) < 100
    assert page["next_cursor"] == len(page["members"])
    assert len(encoded) <= 2 * 1024 * 1024

    resumed = _data(
        await db_session.scalar(
            _PAGE,
            {
                "job_id": job_id,
                "actor_id": actor_id,
                "project_id": project.id,
                "selection_digest": "b" * 64,
                "sealed_digest": None,
                "cursor": page["next_cursor"],
                "page_size": 100,
            },
        )
    )
    assert resumed["cursor"] == page["next_cursor"]
    assert resumed["members"][0]["ordinal"] == page["next_cursor"] + 1
