"""PostgreSQL client for Jobs-owned initial task creation commands."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.task_creation_contract import (
    CreationJobClaim,
    CreationJobCreated,
    CreationJobDiscovery,
    CreationJobFinished,
    decode_creation_reply,
)


class TaskCreationJobs:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        *,
        operation_id: UUID,
        actor_id: UUID,
        dataset_id: UUID,
        project_id: UUID,
        association_id: UUID,
    ) -> CreationJobCreated:
        await self.db.flush()
        payload = await self.db.scalar(
            text(
                "SELECT aap_jobs_v1.create_task_creation_job("
                "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
                "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid), "
                "CAST(:association_id AS uuid))"
            ),
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "dataset_id": dataset_id,
                "project_id": project_id,
                "association_id": association_id,
            },
        )
        result = decode_creation_reply(
            payload, "aap-task-creation-job-command/v1", CreationJobCreated
        )
        if result.job_id != operation_id:
            raise ValueError("Creation job does not match its operation")
        return result

    async def find(
        self,
        *,
        actor_id: UUID,
        dataset_id: UUID,
        project_id: UUID,
        association_id: UUID,
    ) -> CreationJobDiscovery:
        payload = await self.db.scalar(
            text(
                "SELECT aap_jobs_v1.find_task_creation_job("
                "CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid), "
                "CAST(:project_id AS uuid), CAST(:association_id AS uuid))"
            ),
            {
                "actor_id": actor_id,
                "dataset_id": dataset_id,
                "project_id": project_id,
                "association_id": association_id,
            },
        )
        return decode_creation_reply(
            payload, "aap-task-creation-job-discovery/v1", CreationJobDiscovery
        )

    async def claim(self, job_id: UUID, execution_id: str | None) -> CreationJobClaim:
        payload = await self.db.scalar(
            text(
                "SELECT aap_jobs_v1.claim_task_creation_job("
                "CAST(:job_id AS uuid), :execution_id)"
            ),
            {"job_id": job_id, "execution_id": execution_id},
        )
        result = decode_creation_reply(
            payload, "aap-task-creation-job-claim/v1", CreationJobClaim
        )
        if result.job_id != job_id:
            raise ValueError("Claimed creation job does not match its request")
        return result

    async def finish(self, job_id: UUID, operation_id: UUID) -> CreationJobFinished:
        payload = await self.db.scalar(
            text(
                "SELECT aap_jobs_v1.finish_task_creation_job("
                "CAST(:job_id AS uuid), CAST(:operation_id AS uuid))"
            ),
            {"job_id": job_id, "operation_id": operation_id},
        )
        return decode_creation_reply(
            payload, "aap-task-creation-job-finish/v1", CreationJobFinished
        )

    async def fail(
        self, job_id: UUID, operation_id: UUID | None, code: str
    ) -> CreationJobFinished:
        payload = await self.db.scalar(
            text(
                "SELECT aap_jobs_v1.fail_task_creation_job("
                "CAST(:job_id AS uuid), CAST(:operation_id AS uuid), :code)"
            ),
            {"job_id": job_id, "operation_id": operation_id, "code": code},
        )
        return decode_creation_reply(
            payload, "aap-task-creation-job-finish/v1", CreationJobFinished
        )

    async def cancel(self, actor_id: UUID, job_id: UUID) -> CreationJobFinished:
        payload = await self.db.scalar(
            text(
                "SELECT aap_jobs_v1.cancel_task_creation_job("
                "CAST(:actor_id AS uuid), CAST(:job_id AS uuid))"
            ),
            {"actor_id": actor_id, "job_id": job_id},
        )
        return decode_creation_reply(
            payload, "aap-task-creation-job-finish/v1", CreationJobFinished
        )
