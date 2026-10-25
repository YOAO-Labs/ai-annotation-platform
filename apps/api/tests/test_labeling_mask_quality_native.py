"""Saved function-only Mask QC and repair transaction acceptance."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.quality_data_contract import (
    DecideMaskQCIssue,
    QUALITY_DATA_FUNCTIONS,
    QUALITY_DATA_PROTOCOL,
    RecordRepairCandidate,
)
from aap_labeling.quality_data_data import QualityDataClient
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.async_job import AsyncJob, AsyncJobKind
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.services.async_job import create_job
from tests.factory import create_project, create_task, create_user
from tests.test_labeling_class_usage_reads_legacy import (
    _cleanup_owned,
    _create_annotation,
)
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified disposable Mask QC owner role",
    ),
]

_ROLE = "labeling_mask_quality_client"


async def test_qc_decision_and_repair_result_share_the_caller_transaction(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    batch_ids: list[uuid.UUID] = []
    job_ids: list[uuid.UUID] = []
    issue_id = uuid.uuid4()
    connection = transaction = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"mask-quality-native-{uuid.uuid4().hex}@example.test",
                "Mask QC native owner",
            )
            actor_ids.append(actor.id)
            project = await create_project(db, owner_id=actor.id)
            project_ids.append(project.id)
            task = await create_task(db, project_id=project.id)
            task_ids.append(task.id)
            annotation = await _create_annotation(
                db,
                project_id=project.id,
                task_id=task.id,
                user_id=actor.id,
                class_name="car",
                attributes={},
            )
            issue = MaskQCIssue(
                id=issue_id,
                project_id=project.id,
                task_id=task.id,
                annotation_id=annotation.id,
                annotation_version=int(annotation.version or 1),
                related_annotation_ids=[annotation.id],
                source_versions={str(annotation.id): int(annotation.version or 1)},
                code="small_island",
                severity="warning",
                severity_rank=1,
                status="open",
                metric={},
                threshold={},
                dedupe_key=uuid.uuid4().hex + uuid.uuid4().hex,
                source={},
            )
            job = await create_job(
                db,
                kind=AsyncJobKind.MASK_REPAIR.value,
                user_id=actor.id,
                project_id=project.id,
                payload={"mask_repair_batch_id": "pending"},
            )
            job_ids.append(job.id)
            batch = MaskRepairBatch(
                id=uuid.uuid4(),
                project_id=project.id,
                requested_by_id=actor.id,
                async_job_id=job.id,
                token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                status="running",
                plan_digest=uuid.uuid4().hex + uuid.uuid4().hex,
                request_json={},
                plan_json={"items": [], "shards": []},
                result_json={},
                receipt_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            batch_ids.append(batch.id)
            db.add_all([issue, batch])

        connection = await asyncpg.connect(
            _dsn(
                test_engine,
                "AAP_MODULE_DATA_LABELING_MASK_QUALITY_URL",
                _ROLE,
            ),
            timeout=5,
            command_timeout=10,
        )
        assert await connection.fetchval("SELECT current_user") == _ROLE
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions={
                (signature.rsplit(".", 1)[1].split("(", 1)[0], "jsonb")
                for signature in QUALITY_DATA_FUNCTIONS
            },
            role=_ROLE,
        )
        for table_name in (
            "mask_qc_issues",
            "mask_repair_batches",
            "mask_review_scopes",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetch(f"SELECT id FROM public.{table_name} LIMIT 1")

        async def execute(function: str, payload: str):
            return await connection.fetchval(
                f"SELECT aap_labeling_v1.{function}($1::jsonb)", payload
            )

        client = QualityDataClient(execute)
        transaction = connection.transaction()
        await transaction.start()
        issue_result = await client.command(
            DecideMaskQCIssue(
                schema=QUALITY_DATA_PROTOCOL,
                operation="decide_issue",
                issue_id=issue_id,
                expected_task_id=task_ids[0],
                actor_id=actor_ids[0],
                status="resolved",
                decided_at=datetime.now(timezone.utc),
            )
        )
        assert issue_result["changed"] is True
        batch_result = await client.command(
            RecordRepairCandidate(
                schema=QUALITY_DATA_PROTOCOL,
                operation="record_candidate",
                batch_id=batch_ids[0],
                async_job_id=job_ids[0],
                item_index=0,
                result_value={"status": "completed", "candidate_id": "native"},
            )
        )
        assert batch_result["result_json"]["candidates"]["0"]["status"] == "completed"
        await transaction.rollback()
        transaction = None

        async with maker() as db:
            issue = await db.get(MaskQCIssue, issue_id)
            batch = await db.get(MaskRepairBatch, batch_ids[0])
            assert issue is not None and issue.status == "open"
            assert batch is not None and batch.result_json == {}
    finally:
        if transaction is not None:
            await transaction.rollback()
        if connection is not None:
            await connection.close()
        async with maker.begin() as db:
            if batch_ids:
                await db.execute(
                    delete(MaskRepairBatch).where(MaskRepairBatch.id.in_(batch_ids))
                )
            if job_ids:
                await db.execute(delete(AsyncJob).where(AsyncJob.id.in_(job_ids)))
            if issue_id is not None:
                await db.execute(delete(MaskQCIssue).where(MaskQCIssue.id == issue_id))
        await _cleanup_owned(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            task_ids=task_ids,
        )
