"""Opt-in process and LOGIN-role acceptance for durable task creation."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data boundary verifier",
    ),
]

_PEER = Path(__file__).parent / "fixtures" / "task_creation_executor_peer.py"
_CONTAINER_LABEL = "org.aap.acceptance"
_CONTAINER_ID_LABEL = "org.aap.acceptance-id"

_ASSOCIATE = text(
    "SELECT aap_assets_v1.associate_project("
    "CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid), "
    "CAST(:project_id AS uuid))"
)
_ACCEPT = text(
    "SELECT aap_labeling_v1.accept_dataset_task_creation("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid), "
    "CAST(:association_id AS uuid), CAST(:job_id AS uuid), "
    "'generic'::text, 2::integer)"
)
_PREPARE = text(
    "SELECT aap_labeling_v1.prepare_dataset_task_creation("
    "CAST(:operation_id AS uuid), NULL::jsonb)"
)
_CREATE_JOB = text(
    "SELECT aap_jobs_v1.create_task_creation_job("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid), "
    "CAST(:association_id AS uuid))"
)
_CLAIM_JOB = text(
    "SELECT aap_jobs_v1.claim_task_creation_job("
    "CAST(:job_id AS uuid), CAST(:execution_id AS text))"
)
_CANCEL_JOB = text(
    "SELECT aap_jobs_v1.cancel_task_creation_job("
    "CAST(:actor_id AS uuid), CAST(:job_id AS uuid))"
)


def _json_value(value: object) -> dict:
    parsed = json.loads(value) if isinstance(value, str) else value
    assert isinstance(parsed, dict)
    return parsed


def _success(payload: object, schema: str) -> dict:
    reply = _json_value(payload)
    assert reply.get("schema") == schema
    assert reply.get("ok") is True
    assert set(reply) == {"schema", "ok", "data"}
    assert isinstance(reply["data"], dict)
    return reply["data"]


def _append_target() -> tuple[URL, str]:
    append_dsn = os.environ.get("AAP_MODULE_DATA_APPEND_URL")
    test_dsn = os.environ.get("TEST_DATABASE_URL")
    if not append_dsn or not test_dsn:
        pytest.fail("the module-data verifier did not provide its fixture URLs")
    append_url, test_url = make_url(append_dsn), make_url(test_dsn)
    if (
        append_url.drivername != "postgresql+asyncpg"
        or append_url.username != "append_client"
        or append_url.host != "127.0.0.1"
        or append_url.port != test_url.port
        or append_url.database != test_url.database
        or not append_url.database
        or not append_url.password
    ):
        pytest.fail("append-client URL does not match the disposable test database")
    asyncpg_url = append_url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    return append_url, asyncpg_url


async def _seed(test_engine, item_count: int) -> dict[str, object]:
    from app.db.models.dataset import Dataset, DatasetItem
    from app.db.models.project import Project
    from app.db.models.user import User

    suffix = uuid.uuid4().hex[:12].upper()
    fixture: dict[str, object] = {
        "actor_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "dataset_id": uuid.uuid4(),
        "item_ids": [uuid.uuid4() for _ in range(item_count)],
        "item_count": item_count,
    }
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as owner:
        owner.add(
            User(
                id=fixture["actor_id"],
                email=f"task-handoff-{suffix}@example.test",
                name="Task creation handoff",
                password_hash="unused-test-hash",
                role="project_admin",
                status="offline",
                is_active=True,
            )
        )
        await owner.flush()
        owner.add_all(
            [
                Project(
                    id=fixture["project_id"],
                    display_id=f"P-TH-{suffix}",
                    name=f"Task creation handoff {suffix}",
                    type_label="Image",
                    type_key="image",
                    data_type="image",
                    scene_mode=False,
                    owner_id=fixture["actor_id"],
                    total_tasks=0,
                ),
                Dataset(
                    id=fixture["dataset_id"],
                    display_id=f"D-TH-{suffix}",
                    name=f"task-handoff-{suffix}",
                    data_type="image",
                    created_by=fixture["actor_id"],
                    file_count=item_count,
                ),
            ]
        )
        await owner.flush()
        owner.add_all(
            DatasetItem(
                id=item_id,
                dataset_id=fixture["dataset_id"],
                file_name=f"asset-{index}.jpg",
                file_path=f"task-handoff/{suffix}/{index}.jpg",
                file_type="image",
            )
            for index, item_id in enumerate(fixture["item_ids"])
        )
        await owner.flush()
        association = _success(
            await owner.scalar(
                _ASSOCIATE,
                {
                    "actor_id": fixture["actor_id"],
                    "dataset_id": fixture["dataset_id"],
                    "project_id": fixture["project_id"],
                },
            ),
            "aap-dataset-association/v1",
        )
        fixture["association_id"] = uuid.UUID(association["link_id"])
        assert association["existing"] is False
    return fixture


async def _accept_and_prepare(
    test_engine,
    fixture: dict[str, object],
    operation_id: uuid.UUID,
    *,
    job_id: uuid.UUID | None = None,
) -> dict:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as owner:
        accepted = _success(
            await owner.scalar(
                _ACCEPT,
                {
                    "operation_id": operation_id,
                    "actor_id": fixture["actor_id"],
                    "dataset_id": fixture["dataset_id"],
                    "project_id": fixture["project_id"],
                    "association_id": fixture["association_id"],
                    "job_id": job_id,
                },
            ),
            "aap-task-creation/v1",
        )
        assert accepted["operation_id"] == str(operation_id)
        assert accepted["job_id"] == (str(job_id) if job_id else None)
        prepared = _success(
            await owner.scalar(_PREPARE, {"operation_id": operation_id}),
            "aap-task-creation/v1",
        )
        assert prepared["state"] == "running"
        assert prepared["total"] == fixture["item_count"]
        assert prepared["next_chunk"] == 0
    return prepared


async def _create_claim_and_prepare_job(
    test_engine, fixture: dict[str, object], operation_id: uuid.UUID
) -> dict:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as owner:
        created = _success(
            await owner.scalar(
                _CREATE_JOB,
                {
                    "operation_id": operation_id,
                    "actor_id": fixture["actor_id"],
                    "dataset_id": fixture["dataset_id"],
                    "project_id": fixture["project_id"],
                    "association_id": fixture["association_id"],
                },
            ),
            "aap-task-creation-job-command/v1",
        )
        assert created["job_id"] == str(operation_id)
        accepted = _success(
            await owner.scalar(
                _ACCEPT,
                {
                    "operation_id": operation_id,
                    "actor_id": fixture["actor_id"],
                    "dataset_id": fixture["dataset_id"],
                    "project_id": fixture["project_id"],
                    "association_id": fixture["association_id"],
                    "job_id": operation_id,
                },
            ),
            "aap-task-creation/v1",
        )
        assert accepted["job_id"] == str(operation_id)
        claimed = _success(
            await owner.scalar(
                _CLAIM_JOB,
                {
                    "job_id": operation_id,
                    "execution_id": f"peer-{operation_id.hex[:20]}",
                },
            ),
            "aap-task-creation-job-claim/v1",
        )
        assert claimed["status"] == "running"
        assert claimed["cancel_requested"] is False
        prepared = _success(
            await owner.scalar(_PREPARE, {"operation_id": operation_id}),
            "aap-task-creation/v1",
        )
        assert prepared["state"] == "running"
        assert prepared["total"] == fixture["item_count"]
    return prepared


def _peer_request(
    fixture: dict[str, object],
    total: int,
    job_id: uuid.UUID | None,
    database_url: str,
) -> dict[str, object]:
    return {
        "database_url": database_url,
        "expected": {
            "actor_id": str(fixture["actor_id"]),
            "dataset_id": str(fixture["dataset_id"]),
            "project_id": str(fixture["project_id"]),
            "association_id": str(fixture["association_id"]),
            "job_id": str(job_id) if job_id else None,
            "chunk_size": 2,
            "total": total,
        },
    }


async def _read_event(process: asyncio.subprocess.Process) -> dict:
    assert process.stdout is not None
    line = await asyncio.wait_for(process.stdout.readline(), timeout=30)
    assert line and len(line) <= 4096, (
        "task-creation peer exited without a bounded event"
    )
    event = json.loads(line)
    assert isinstance(event, dict)
    return event


async def _start_peer(
    fixture: dict[str, object],
    operation_id: uuid.UUID,
    total: int,
    *,
    job_id: uuid.UUID | None = None,
    pause_after_first: bool,
) -> asyncio.subprocess.Process:
    _, database_url = _append_target()
    command = [sys.executable, str(_PEER), "--operation-id", str(operation_id)]
    if pause_after_first:
        command.append("--pause-after-chunk=0")
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=Path(__file__).parents[1],
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
        },
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(
            (
                json.dumps(_peer_request(fixture, total, job_id, database_url)) + "\n"
            ).encode()
        )
        await process.stdin.drain()
        ready = await _read_event(process)
        assert ready == {"type": "ready", "current_user": "append_client"}
        return process
    except BaseException:
        await _stop_peer(process)
        raise


async def _stop_peer(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
    if process.stdin is not None:
        process.stdin.close()
        try:
            await process.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
    if process.stderr is not None:
        await process.stderr.read()


async def _resume_peer(process: asyncio.subprocess.Process) -> None:
    assert process.stdin is not None
    process.stdin.write(b'{"resume":true}\n')
    await process.stdin.drain()


def _inspect_owned_container(append_url: URL) -> str:
    container_id = os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    if not container_id or re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        pytest.fail(
            "the verifier did not provide an exact owned PostgreSQL container id"
        )
    result = subprocess.run(
        ["docker", "inspect", container_id],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail("the verifier-owned PostgreSQL container could not be inspected")
    observed = json.loads(result.stdout)[0]
    labels = observed.get("Config", {}).get("Labels") or {}
    bindings = (observed.get("NetworkSettings", {}).get("Ports") or {}).get(
        "5432/tcp"
    ) or []
    valid = (
        observed.get("Id") == container_id
        and observed.get("Name", "").startswith("/aap-module-data-")
        and observed.get("Config", {}).get("Image") == "postgres:16-alpine"
        and labels.get(_CONTAINER_LABEL) == "module-data-boundary"
        and re.fullmatch(r"[0-9a-f]{24}", labels.get(_CONTAINER_ID_LABEL, ""))
        is not None
        and observed.get("State", {}).get("Running") is True
        and len(bindings) == 1
        and bindings[0].get("HostIp") == "127.0.0.1"
        and int(bindings[0].get("HostPort", 0)) == append_url.port
    )
    if not valid:
        pytest.fail("PostgreSQL container identity or loopback binding did not match")
    return container_id


def _native_psql_advance(
    operation_id: uuid.UUID, expected_project_id: uuid.UUID, expected_total: int
) -> dict:
    append_url, _ = _append_target()
    container_id = _inspect_owned_container(append_url)
    database = append_url.database
    sql = """\
\\set ON_ERROR_STOP on
SELECT current_user || '|' || session_user || '|' || current_database();
BEGIN;
SELECT aap_labeling_v1.read_dataset_task_creation(:'operation_id'::uuid);
SELECT aap_labeling_v1.advance_dataset_task_creation(:'operation_id'::uuid, 1);
SELECT aap_labeling_v1.read_dataset_task_creation(:'operation_id'::uuid);
COMMIT;
"""
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "-e",
            f"PGPASSWORD={append_url.password}",
            container_id,
            "psql",
            "-X",
            "-q",
            "-A",
            "-t",
            "-h",
            "127.0.0.1",
            "-p",
            "5432",
            "-U",
            "append_client",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            f"operation_id={operation_id}",
            "-f",
            "-",
        ],
        input=sql,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail("native append-client psql continuation failed")
    lines = result.stdout.splitlines()
    assert len(lines) == 4
    assert lines[0] == f"append_client|append_client|{database}"
    before = _json_value(lines[1])
    chunk = _json_value(lines[2])
    after = _json_value(lines[3])
    before_data = _success(before, "aap-task-creation/v1")
    chunk_data = _success(chunk, "aap-task-creation-chunk/v1")
    after_data = _success(after, "aap-task-creation/v1")
    assert before_data["next_chunk"] == 1
    assert before_data["total"] == expected_total
    assert chunk_data["operation_id"] == str(operation_id)
    assert chunk_data["project_id"] == str(expected_project_id)
    assert chunk_data["chunk_index"] == 1
    assert chunk_data["next_chunk"] == 2
    assert chunk_data["total"] == expected_total
    assert chunk_data["cumulative_created"] == 4
    assert chunk_data["cumulative_processed"] == 4
    assert chunk_data["project_total"] == 4
    assert chunk_data["complete"] is False
    assert after_data["next_chunk"] == 2
    assert after_data["created"] == 4
    assert after_data["processed"] == chunk_data["cumulative_processed"]
    return chunk_data


async def _database_facts(
    test_engine, fixture: dict[str, object], operation_id: uuid.UUID
):
    async with test_engine.connect() as owner:
        counts = (
            await owner.execute(
                text(
                    "SELECT count(*) AS task_count, "
                    "count(DISTINCT dataset_item_id) AS distinct_items "
                    "FROM public.tasks WHERE project_id = :project_id"
                ),
                {"project_id": fixture["project_id"]},
            )
        ).one()
        operation = (
            await owner.execute(
                text(
                    "SELECT state, total, next_chunk, processed, created "
                    "FROM public.task_creation_operations WHERE id = :id"
                ),
                {"id": operation_id},
            )
        ).one()
        total_tasks = await owner.scalar(
            text("SELECT total_tasks FROM public.projects WHERE id = :id"),
            {"id": fixture["project_id"]},
        )
        chunk_count = await owner.scalar(
            text(
                "SELECT count(*) FROM public.task_creation_chunks "
                "WHERE operation_id = :id"
            ),
            {"id": operation_id},
        )
    return {
        "task_count": counts.task_count,
        "distinct_items": counts.distinct_items,
        "state": operation.state,
        "total": operation.total,
        "next_chunk": operation.next_chunk,
        "processed": operation.processed,
        "created": operation.created,
        "total_tasks": total_tasks,
        "chunk_count": chunk_count,
    }


async def _cleanup(
    test_engine, fixture: dict[str, object], ids: list[uuid.UUID]
) -> None:
    async with test_engine.begin() as owner:
        if ids:
            await owner.execute(
                text(
                    "DELETE FROM public.task_creation_operations "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": ids},
            )
            await owner.execute(
                text(
                    "DELETE FROM public.async_jobs WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": ids},
            )
        for statement in (
            "DELETE FROM public.tasks WHERE project_id = :project_id",
            "DELETE FROM public.project_datasets WHERE dataset_id = :dataset_id",
            "DELETE FROM public.dataset_items WHERE dataset_id = :dataset_id",
            "DELETE FROM public.datasets WHERE id = :dataset_id",
            "DELETE FROM public.projects WHERE id = :project_id",
            "DELETE FROM public.users WHERE id = :actor_id",
        ):
            await owner.execute(
                text(statement),
                {
                    "project_id": fixture["project_id"],
                    "dataset_id": fixture["dataset_id"],
                    "actor_id": fixture["actor_id"],
                },
            )


async def test_killed_and_restarted_peers_resume_after_actor_deactivation_with_psql_chunk(
    test_engine,
):
    fixture = await _seed(test_engine, item_count=5)
    operation_id, denied_operation_id = uuid.uuid4(), uuid.uuid4()
    peer = None
    try:
        prepared = await _accept_and_prepare(test_engine, fixture, operation_id)
        assert prepared["total"] == 5
        peer = await _start_peer(
            fixture,
            operation_id,
            5,
            pause_after_first=True,
        )
        first_chunk = await _read_event(peer)
        assert first_chunk == {
            "type": "chunk_committed",
            "operation_id": str(operation_id),
            "chunk_index": 0,
            "created": 2,
            "processed": 2,
            "cumulative_created": 2,
            "cumulative_processed": 2,
            "total": 5,
            "next_chunk": 1,
            "complete": False,
            "project_total": 2,
        }
        assert peer.returncode is None
        peer.kill()
        killed_status = await asyncio.wait_for(peer.wait(), timeout=10)
        assert killed_status != 0
        await _stop_peer(peer)
        peer = None

        async with test_engine.begin() as owner:
            await owner.execute(
                text("UPDATE public.users SET is_active = false WHERE id = :id"),
                {"id": fixture["actor_id"]},
            )
            denied = _json_value(
                await owner.scalar(
                    _ACCEPT,
                    {
                        "operation_id": denied_operation_id,
                        "actor_id": fixture["actor_id"],
                        "dataset_id": fixture["dataset_id"],
                        "project_id": fixture["project_id"],
                        "association_id": fixture["association_id"],
                        "job_id": None,
                    },
                )
            )
            assert denied["ok"] is False
            assert denied["error"]["code"] == "account_not_active"

        native_chunk = _native_psql_advance(operation_id, fixture["project_id"], 5)
        assert native_chunk["created"] == 2
        assert native_chunk["processed"] == 2

        peer = await _start_peer(
            fixture,
            operation_id,
            5,
            pause_after_first=False,
        )
        events = []
        while True:
            event = await _read_event(peer)
            events.append(event)
            if event["type"] == "complete":
                break
            assert event["type"] == "chunk_committed"
        assert events[0]["chunk_index"] == 2
        assert events[0]["created"] == 1
        assert events[0]["complete"] is True
        assert events[0]["project_total"] == 5
        assert events[-1] == {
            "type": "complete",
            "operation_id": str(operation_id),
            "total": 5,
            "processed": 5,
            "created": 5,
            "next_chunk": 3,
        }
        assert await asyncio.wait_for(peer.wait(), timeout=10) == 0
        await _stop_peer(peer)
        peer = None

        facts = await _database_facts(test_engine, fixture, operation_id)
        assert facts == {
            "task_count": 5,
            "distinct_items": 5,
            "state": "completed",
            "total": 5,
            "next_chunk": 3,
            "processed": 5,
            "created": 5,
            "total_tasks": 5,
            "chunk_count": 3,
        }
        async with test_engine.connect() as owner:
            assert (
                await owner.scalar(
                    text(
                        "SELECT count(*) FROM public.task_creation_operations "
                        "WHERE id = :id"
                    ),
                    {"id": denied_operation_id},
                )
                == 0
            )
    finally:
        if peer is not None:
            await _stop_peer(peer)
        await _cleanup(test_engine, fixture, [operation_id, denied_operation_id])


async def test_unlink_relink_changes_association_generation_before_next_chunk(
    test_engine,
):
    from app.application.dataset_deletion import unlink_dataset_from_project

    fixture = await _seed(test_engine, item_count=4)
    operation_id = uuid.uuid4()
    peer = None
    try:
        await _accept_and_prepare(test_engine, fixture, operation_id)
        peer = await _start_peer(
            fixture,
            operation_id,
            4,
            pause_after_first=True,
        )
        first_chunk = await _read_event(peer)
        assert first_chunk["type"] == "chunk_committed"
        assert first_chunk["chunk_index"] == 0
        assert first_chunk["created"] == 2
        assert peer.returncode is None

        maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with maker.begin() as owner:
            result = await unlink_dataset_from_project(
                owner, fixture["dataset_id"], fixture["project_id"]
            )
            assert result is not None
            assert result["deleted_tasks"] == 2
        async with test_engine.begin() as owner:
            association = _success(
                await owner.scalar(
                    _ASSOCIATE,
                    {
                        "actor_id": fixture["actor_id"],
                        "dataset_id": fixture["dataset_id"],
                        "project_id": fixture["project_id"],
                    },
                ),
                "aap-dataset-association/v1",
            )
        new_association_id = uuid.UUID(association["link_id"])
        assert new_association_id != fixture["association_id"]
        assert association["existing"] is False
        assert association["item_count"] == 4
        fixture["new_association_id"] = new_association_id

        await _resume_peer(peer)
        rejected = await _read_event(peer)
        assert rejected == {"type": "error", "code": "association_changed"}
        assert await asyncio.wait_for(peer.wait(), timeout=10) == 2
        await _stop_peer(peer)
        peer = None

        facts = await _database_facts(test_engine, fixture, operation_id)
        assert facts == {
            "task_count": 0,
            "distinct_items": 0,
            "state": "running",
            "total": 4,
            "next_chunk": 1,
            "processed": 2,
            "created": 2,
            "total_tasks": 0,
            "chunk_count": 1,
        }
        async with test_engine.connect() as owner:
            current_link = await owner.scalar(
                text(
                    "SELECT id FROM public.project_datasets "
                    "WHERE dataset_id = :dataset_id AND project_id = :project_id"
                ),
                {
                    "dataset_id": fixture["dataset_id"],
                    "project_id": fixture["project_id"],
                },
            )
            assert current_link == new_association_id
    finally:
        if peer is not None:
            await _stop_peer(peer)
        await _cleanup(test_engine, fixture, [operation_id])


async def test_cancelled_job_blocks_new_chunk_after_committed_partial_progress(
    test_engine,
):
    fixture = await _seed(test_engine, item_count=4)
    operation_id = uuid.uuid4()
    peer = None
    try:
        prepared = await _create_claim_and_prepare_job(
            test_engine, fixture, operation_id
        )
        assert prepared["total"] == 4
        peer = await _start_peer(
            fixture,
            operation_id,
            4,
            job_id=operation_id,
            pause_after_first=True,
        )
        first_chunk = await _read_event(peer)
        assert first_chunk["type"] == "chunk_committed"
        assert first_chunk["chunk_index"] == 0
        assert first_chunk["created"] == 2
        assert peer.returncode is None
        peer.kill()
        assert await asyncio.wait_for(peer.wait(), timeout=10) != 0
        await _stop_peer(peer)
        peer = None

        async with test_engine.begin() as owner:
            cancelled = _success(
                await owner.scalar(
                    _CANCEL_JOB,
                    {"actor_id": fixture["actor_id"], "job_id": operation_id},
                ),
                "aap-task-creation-job-finish/v1",
            )
            assert cancelled["status"] == "cancelled"

        peer = await _start_peer(
            fixture,
            operation_id,
            4,
            job_id=operation_id,
            pause_after_first=False,
        )
        rejected = await _read_event(peer)
        assert rejected == {"type": "error", "code": "job_cancelled"}
        assert await asyncio.wait_for(peer.wait(), timeout=10) == 2
        await _stop_peer(peer)
        peer = None

        facts = await _database_facts(test_engine, fixture, operation_id)
        assert facts == {
            "task_count": 2,
            "distinct_items": 2,
            "state": "running",
            "total": 4,
            "next_chunk": 1,
            "processed": 2,
            "created": 2,
            "total_tasks": 2,
            "chunk_count": 1,
        }
        async with test_engine.connect() as owner:
            assert (
                await owner.scalar(
                    text("SELECT status FROM public.async_jobs WHERE id = :id"),
                    {"id": operation_id},
                )
                == "cancelled"
            )
    finally:
        if peer is not None:
            await _stop_peer(peer)
        await _cleanup(test_engine, fixture, [operation_id])
