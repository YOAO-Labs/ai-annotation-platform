"""Actual LOGIN-role and transaction binding for the portable Jobs adapter."""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from aap_preannotation_results.video_execution import (
    ClaimRequest,
    GuardRequest,
    VideoExecutionFailure,
)
from aap_preannotation_results_runtime.video_execution_database import (
    VideoExecutionPostgresData,
)
from aap_preannotation_results.video_execution_journal import (
    ReadRequest,
    VideoJournalFailure,
)
from aap_preannotation_results_runtime.video_journal_database import (
    VideoJournalPostgresData,
)
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_video_tracker_input_native import _capture, _seed_tracker_job
from tests.test_legacy_image_runtime_live import _apply_lan_grants

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def test_video_owner_login_has_no_raw_writes_and_fence_cannot_escape_transaction(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as owner:
        actor, project, item, task_id, job = await _seed_tracker_job(owner)
        project_id = project.id
        job_id = job.id
        dataset = await owner.scalar(
            text("SELECT dataset_id FROM public.dataset_items WHERE id=:item"),
            {"item": item},
        )
        await _capture(owner, actor, job_id)
        await owner.commit()
    connection = await asyncpg.connect(
        os.environ["AAP_MODULE_DATA_VIDEO_EXECUTION_URL"].replace(
            "postgresql+asyncpg://", "postgresql://", 1
        ),
        timeout=5,
        command_timeout=10,
    )
    try:
        assert await connection.fetchval("SELECT current_user = session_user") is True
        assert (
            await connection.fetchval("SELECT current_user") == "video_execution_client"
        )
        async with connection.transaction():
            data = await VideoExecutionPostgresData.bind(connection)
            claim = (
                await data.step("claim", ClaimRequest(job_id=job_id, worker_id=uuid4()))
            )["execution"]
            journal = await VideoJournalPostgresData.bind(connection)
            read_plan = ReadRequest(**claim.proof().model_dump())
            assert await journal.step("read_plan", read_plan) == {"plan": None}
            guard = GuardRequest(**claim.proof().model_dump(), purpose="start")
            assert await data.step("guard", guard) == {"guarded": True}
            for statement in (
                "SELECT * FROM aap_jobs_v1.video_executions",
                "SELECT * FROM aap_jobs_v1.video_execution_journals",
                "SELECT aap_jobs_v1._video_execution_secret()",
                "CREATE TEMP TABLE forbidden_video_owner(value integer)",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with connection.transaction():
                        await connection.execute(statement)
        with pytest.raises(VideoExecutionFailure, match="^storage_unavailable$"):
            await data.step("guard", guard)
        with pytest.raises(VideoJournalFailure, match="^storage_unavailable$"):
            await journal.step("read_plan", read_plan)
        async with sessions() as owner:
            await owner.execute(
                text(
                    "GRANT EXECUTE ON FUNCTION aap_jobs_v1._video_execution_secret() TO video_execution_client"
                )
            )
            await owner.commit()
        try:
            async with connection.transaction():
                with pytest.raises(
                    VideoExecutionFailure, match="^storage_unavailable$"
                ):
                    await VideoExecutionPostgresData.bind(connection)
        finally:
            async with sessions() as owner:
                await owner.execute(
                    text(
                        "REVOKE ALL ON FUNCTION aap_jobs_v1._video_execution_secret() FROM video_execution_client"
                    )
                )
                await owner.commit()
    finally:
        await connection.close()
        async with sessions() as owner:
            for statement in (
                "DELETE FROM aap_jobs_v1.video_executions WHERE job_id=:id",
                "DELETE FROM public.video_tracker_input_scopes WHERE tracker_job_id=:id",
                "DELETE FROM public.video_tracker_jobs WHERE id=:id",
            ):
                await owner.execute(text(statement), {"id": job_id})
            await owner.execute(
                text("DELETE FROM public.tasks WHERE id=:id"), {"id": task_id}
            )
            await owner.execute(
                text("DELETE FROM public.projects WHERE id=:id"), {"id": project_id}
            )
            await owner.execute(
                text("DELETE FROM public.datasets WHERE id=:id"), {"id": dataset}
            )
            await owner.execute(
                text("DELETE FROM public.users WHERE id=:id"), {"id": actor}
            )
            await owner.commit()


@pytest.mark.parametrize("grant_profile", ("worktree", "lan"))
async def test_compatibility_worker_can_claim_and_fence_without_private_jobs_access(
    test_engine, monkeypatch, grant_profile
):
    await assert_owned_test_database(test_engine)
    from app.services.video_tracking.execution import (
        execution_proof,
        fence_tracker_write,
        tracker_execution,
    )

    functions = (
        "aap_jobs_v1.video_execution_step(text,text)",
        "aap_jobs_v1.video_execution_checkpoint_step(text,text)",
        "aap_labeling_v1.capture_video_tracker_input(uuid,uuid,integer)",
        "aap_labeling_v1.lock_video_tracker_input_for_stage(uuid,uuid,uuid,uuid,text,text)",
        "aap_labeling_v1.lock_video_tracker_input_for_accept(uuid,uuid,uuid,uuid,text,text)",
        "aap_labeling_v1.attach_annotation_video_tracker_input_source(uuid,uuid,uuid,jsonb)",
        "aap_labeling_v1.copy_annotation_video_tracker_input_sources(uuid,uuid,uuid)",
        "aap_inference_routing_v1.routing_policy()",
        "aap_inference_routing_v1.routing_step(text,text)",
        "aap_inference_routing_v1.scope_retirement_step(text,text)",
        "aap_inference_routing_v1.video_execution_scope_step(text,text)",
    )
    owners = async_sessionmaker(test_engine, expire_on_commit=False)
    if grant_profile == "lan":
        # Prove the deployment script grants these capabilities by itself.
        async with owners.begin() as db:
            await db.execute(
                text(
                    "REVOKE ALL ON FUNCTION "
                    + ",".join(functions)
                    + " FROM legacy_runtime"
                )
            )
        await _apply_lan_grants(test_engine, monkeypatch)
    async with owners.begin() as db:
        actor, _, _, _, job = await _seed_tracker_job(db)
        job_id = job.id
        await _capture(db, actor, job_id)
    runtime = create_async_engine(
        os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"],
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        sessions = async_sessionmaker(runtime, expire_on_commit=False)
        async with sessions() as db:
            assert await db.scalar(text("SELECT current_user")) == "legacy_runtime"
            for signature in functions:
                assert await db.scalar(
                    text("SELECT has_function_privilege(current_user,:fn,'EXECUTE')"),
                    {"fn": signature},
                ), signature
            assert not await db.scalar(
                text(
                    "SELECT has_table_privilege(current_user,"
                    "'aap_jobs_v1.video_executions','SELECT,INSERT,UPDATE,DELETE')"
                )
            )
            async with tracker_execution(db, job_id, session_factory=sessions):
                assert execution_proof(job_id).generation == 1
                await fence_tracker_write(db, job_id, "start")
                await db.execute(
                    text(
                        "UPDATE public.video_tracker_jobs SET status='running' WHERE id=:job"
                    ),
                    {"job": job_id},
                )
                await db.commit()
    finally:
        await runtime.dispose()
