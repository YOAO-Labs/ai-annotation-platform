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
    AttachRepairRollbackJob,
    DecideMaskQCIssue,
    FailRepairRollback,
    QUALITY_DATA_FUNCTIONS,
    QUALITY_DATA_PROTOCOL,
    ReadMaskRepairBatchById,
    ReadMaskRepairBatchForSource,
    ReconcileRepairBatch,
    RecordRepairCandidate,
    RecordRepairRollbackShard,
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


async def test_rollback_shard_checkpoint_binds_to_the_caller_transaction(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    batch_ids: list[uuid.UUID] = []
    job_ids: list[uuid.UUID] = []
    active_batch_id = uuid.uuid4()
    terminal_batch_id = uuid.uuid4()
    shard_id = uuid.uuid4().hex[:24]
    unknown_shard_id = uuid.uuid4().hex[:24]
    plan = {
        "items": [],
        "shards": [
            {
                "id": shard_id,
                "task_id": str(uuid.uuid4()),
                "item_indexes": [],
                "status": "pending",
                "operation_ids": [],
            }
        ],
        "summary": {},
    }
    preserved = {
        "shards": {"completed-repair-shard": {"status": "completed"}},
        "candidates": {"0": {"status": "completed"}},
        "note": "keep",
    }
    checkpoint = {
        "status": "completed",
        "mutation_count": 2,
        "versions": {str(uuid.uuid4()): 3},
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "annotation_facts": {},
    }
    conflicting = {**checkpoint, "mutation_count": 99}
    connection = transaction = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"mask-repair-rollback-native-{uuid.uuid4().hex}@example.test",
                "Mask repair rollback native owner",
            )
            actor_ids.append(actor.id)
            project = await create_project(db, owner_id=actor.id)
            project_ids.append(project.id)
            rollback_job = await create_job(
                db,
                kind=AsyncJobKind.MASK_REPAIR_ROLLBACK.value,
                user_id=actor.id,
                project_id=project.id,
                payload={"mask_repair_batch_id": str(active_batch_id)},
            )
            stale_job = await create_job(
                db,
                kind=AsyncJobKind.MASK_REPAIR_ROLLBACK.value,
                user_id=actor.id,
                project_id=project.id,
                payload={"mask_repair_batch_id": str(active_batch_id)},
            )
            retry_job = await create_job(
                db,
                kind=AsyncJobKind.MASK_REPAIR_ROLLBACK.value,
                user_id=actor.id,
                project_id=project.id,
                payload={"mask_repair_batch_id": str(active_batch_id)},
            )
            job_ids.extend([rollback_job.id, stale_job.id, retry_job.id])
            active_batch = MaskRepairBatch(
                id=active_batch_id,
                project_id=project.id,
                requested_by_id=actor.id,
                rollback_async_job_id=rollback_job.id,
                token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                status="rolling_back",
                plan_digest=uuid.uuid4().hex + uuid.uuid4().hex,
                request_json={},
                plan_json=plan,
                result_json=preserved,
                receipt_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            terminal_batch = MaskRepairBatch(
                id=terminal_batch_id,
                project_id=project.id,
                requested_by_id=actor.id,
                rollback_async_job_id=stale_job.id,
                token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                status="rolled_back",
                plan_digest=uuid.uuid4().hex + uuid.uuid4().hex,
                request_json={},
                plan_json=plan,
                result_json={
                    **preserved,
                    "rollback": {
                        "status": "completed",
                        "mutation_count": 1,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
                receipt_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            batch_ids.extend([active_batch_id, terminal_batch_id])
            db.add_all([active_batch, terminal_batch])

        connection = await asyncpg.connect(
            _dsn(
                test_engine,
                "AAP_MODULE_DATA_LABELING_MASK_QUALITY_URL",
                _ROLE,
            ),
            timeout=5,
            command_timeout=10,
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch(
                "SELECT result_json FROM public.mask_repair_batches LIMIT 1"
            )

        async def execute(function: str, payload: str):
            return await connection.fetchval(
                f"SELECT aap_labeling_v1.{function}($1::jsonb)", payload
            )

        def rollback_shard(
            batch_id: uuid.UUID,
            job_id: uuid.UUID,
            shard: str,
            result_value: dict[str, object],
        ) -> RecordRepairRollbackShard:
            return RecordRepairRollbackShard(
                schema=QUALITY_DATA_PROTOCOL,
                operation="record_rollback_shard",
                batch_id=batch_id,
                rollback_job_id=job_id,
                shard_id=shard,
                result_value=result_value,
            )

        client = QualityDataClient(execute)
        transaction = connection.transaction()
        await transaction.start()
        result = await client.command(
            rollback_shard(active_batch_id, rollback_job.id, shard_id, checkpoint)
        )
        assert result["result_json"]["rollback"]["status"] == "running"
        assert result["result_json"]["rollback"]["shards"][shard_id] == checkpoint
        assert result["result_json"]["shards"] == preserved["shards"]
        assert result["result_json"]["candidates"] == preserved["candidates"]
        assert result["result_json"]["note"] == "keep"

        replay = await client.command(
            rollback_shard(active_batch_id, rollback_job.id, shard_id, checkpoint)
        )
        assert replay["result_json"]["rollback"]["shards"][shard_id] == checkpoint

        conflict = await client.command(
            rollback_shard(active_batch_id, rollback_job.id, shard_id, conflicting)
        )
        assert conflict == {"changed": False}
        stale = await client.command(
            rollback_shard(active_batch_id, stale_job.id, shard_id, checkpoint)
        )
        assert stale == {"changed": False}
        terminal = await client.command(
            rollback_shard(terminal_batch_id, stale_job.id, shard_id, checkpoint)
        )
        assert terminal == {"changed": False}

        with pytest.raises(asyncpg.PostgresError):
            async with connection.transaction():
                await client.command(
                    rollback_shard(
                        active_batch_id, rollback_job.id, unknown_shard_id, checkpoint
                    )
                )
        with pytest.raises(asyncpg.PostgresError):
            async with connection.transaction():
                await client.command(
                    rollback_shard(
                        active_batch_id,
                        rollback_job.id,
                        shard_id,
                        {"status": "completed", "mutation_count": 1},
                    )
                )

        stored = await client.command(
            ReadMaskRepairBatchById(
                schema=QUALITY_DATA_PROTOCOL,
                operation="read_batch_by_id",
                batch_id=active_batch_id,
                lock=False,
            )
        )
        assert stored["status"] == "rolling_back"
        assert stored["result_json"]["rollback"]["shards"][shard_id] == checkpoint
        assert stored["result_json"]["shards"] == preserved["shards"]
        assert stored["result_json"]["candidates"] == preserved["candidates"]
        failed = await client.command(
            FailRepairRollback(
                schema=QUALITY_DATA_PROTOCOL,
                operation="fail_rollback",
                batch_id=active_batch_id,
                rollback_job_id=rollback_job.id,
                error_message="later shard failed",
                result_json=stored["result_json"],
                failed_at=datetime.now(timezone.utc),
            )
        )
        assert failed["status"] == "rollback_failed"
        resumed = await client.command(
            AttachRepairRollbackJob(
                schema=QUALITY_DATA_PROTOCOL,
                operation="attach_rollback_job",
                batch_id=active_batch_id,
                rollback_job_id=retry_job.id,
            )
        )
        assert resumed["status"] == "rolling_back"
        assert resumed["rollback_async_job_id"] == str(retry_job.id)
        assert resumed["result_json"] == stored["result_json"]
        fenced = await client.command(
            rollback_shard(active_batch_id, rollback_job.id, shard_id, checkpoint)
        )
        assert fenced == {"changed": False}
        replay_after_resume = await client.command(
            rollback_shard(active_batch_id, retry_job.id, shard_id, checkpoint)
        )
        assert replay_after_resume["result_json"] == stored["result_json"]
        cancelled = await client.command(
            ReconcileRepairBatch(
                schema=QUALITY_DATA_PROTOCOL,
                operation="reconcile_terminal",
                job_id=retry_job.id,
                terminal_kind="mask_repair_rollback",
                terminal_cancelled=True,
                status="rollback_failed",
                completed_at=datetime.now(timezone.utc),
            )
        )
        assert cancelled["status"] == "rollback_failed"
        assert cancelled["result_json"] == stored["result_json"]
        already_terminal = await client.command(
            ReconcileRepairBatch(
                schema=QUALITY_DATA_PROTOCOL,
                operation="reconcile_terminal",
                job_id=stale_job.id,
                terminal_kind="mask_repair_rollback",
                terminal_cancelled=True,
                status="rollback_failed",
                completed_at=datetime.now(timezone.utc),
            )
        )
        assert already_terminal is None
        await transaction.rollback()
        transaction = None

        async with maker() as db:
            active_stored = await db.get(MaskRepairBatch, active_batch_id)
            terminal_stored = await db.get(MaskRepairBatch, terminal_batch_id)
            assert active_stored is not None and active_stored.result_json == preserved
            assert active_stored.status == "rolling_back"
            assert terminal_stored is not None
            assert terminal_stored.result_json["rollback"]["status"] == "completed"
            assert terminal_stored.status == "rolled_back"
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
        await _cleanup_owned(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            task_ids=[],
        )


async def test_source_read_shares_the_batch_and_never_queues(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    batch_ids: list[uuid.UUID] = []
    batch_id = uuid.uuid4()
    reader = holder = None
    holder_transaction = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"mask-repair-source-native-{uuid.uuid4().hex}@example.test",
                "Mask repair source read native owner",
            )
            actor_ids.append(actor.id)
            project = await create_project(db, owner_id=actor.id)
            project_ids.append(project.id)
            batch = MaskRepairBatch(
                id=batch_id,
                project_id=project.id,
                requested_by_id=actor.id,
                token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                status="running",
                plan_digest=uuid.uuid4().hex + uuid.uuid4().hex,
                request_json={},
                plan_json={"items": [], "shards": [], "summary": {}},
                result_json={},
                receipt_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            batch_ids.append(batch_id)
            db.add(batch)

        reader = await asyncpg.connect(
            _dsn(test_engine, "AAP_MODULE_DATA_LABELING_MASK_QUALITY_URL", _ROLE),
            timeout=5,
            command_timeout=10,
        )
        holder = await asyncpg.connect(
            _dsn(test_engine, "AAP_MODULE_DATA_LABELING_MASK_QUALITY_URL", _ROLE),
            timeout=5,
            command_timeout=10,
        )

        def client(connection) -> QualityDataClient:
            async def execute(function: str, payload: str):
                return await connection.fetchval(
                    f"SELECT aap_labeling_v1.{function}($1::jsonb)", payload
                )

            return QualityDataClient(execute)

        def read_by_id(lock: bool):
            return ReadMaskRepairBatchById(
                schema=QUALITY_DATA_PROTOCOL,
                operation="read_batch_by_id",
                batch_id=batch_id,
                lock=lock,
            )

        def read_source(lock: bool):
            return ReadMaskRepairBatchForSource(
                schema=QUALITY_DATA_PROTOCOL,
                operation="read_batch_for_source",
                batch_id=batch_id,
                lock=lock,
            )

        reader_client = client(reader)
        holder_client = client(holder)

        missing = await reader_client.command(
            ReadMaskRepairBatchForSource(
                schema=QUALITY_DATA_PROTOCOL,
                operation="read_batch_for_source",
                batch_id=uuid.uuid4(),
                lock=False,
            )
        )
        assert missing is None

        holder_transaction = holder.transaction()
        await holder_transaction.start()
        shared = await holder_client.command(read_source(True))
        assert shared["id"] == str(batch_id)

        ordinary = await reader_client.command(read_source(False))
        by_id = await reader_client.command(read_by_id(False))
        assert set(ordinary) == set(by_id)
        assert ordinary == by_id

        peer_shared = await reader_client.command(read_source(True))
        assert peer_shared["id"] == str(batch_id)

        await holder_transaction.rollback()
        holder_transaction = holder.transaction()
        await holder_transaction.start()
        updated = await holder_client.command(read_by_id(True))
        assert updated["id"] == str(batch_id)

        with pytest.raises(asyncpg.PostgresError) as locked:
            await reader_client.command(read_source(True))
        assert locked.value.sqlstate == "55P03"

        assert (await reader_client.command(read_source(False)))["id"] == str(batch_id)

        await holder_transaction.rollback()
        holder_transaction = None
        assert (await reader_client.command(read_source(True)))["id"] == str(batch_id)
    finally:
        if holder_transaction is not None:
            await holder_transaction.rollback()
        if reader is not None:
            await reader.close()
        if holder is not None:
            await holder.close()
        async with maker.begin() as db:
            if batch_ids:
                await db.execute(
                    delete(MaskRepairBatch).where(MaskRepairBatch.id.in_(batch_ids))
                )
        await _cleanup_owned(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            task_ids=[],
        )
