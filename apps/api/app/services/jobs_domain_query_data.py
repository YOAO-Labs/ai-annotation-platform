"""Borrowed-session binding for the Jobs owner's fixed domain queries."""

from sqlalchemy import select, text
from yoao_jobs.domain_query_contract import (
    FrozenDeploymentUsage,
    FrozenDeploymentUsageRequest,
)

from app.db.models.async_job import AsyncJob


class JobsDomainQueryPostgresData:
    def __init__(self, db):
        self.db = db

    async def lock_cross_frame_singleflight(self, actor_id, singleflight_key):
        await self.db.execute(
            text(
                "SELECT yoao_jobs_v1.lock_cross_frame_singleflight("
                "CAST(:actor_id AS pg_catalog.uuid), "
                "CAST(:singleflight_key AS pg_catalog.text))"
            ),
            {"actor_id": actor_id, "singleflight_key": singleflight_key},
        )

    async def active_cross_frame_job(self, actor_id, singleflight_key):
        result = await self.db.execute(
            select(AsyncJob).from_statement(
                text(
                    "SELECT job.*, "
                    "yoao_jobs_v1.read_job_execution_snapshot(job.id) "
                    "AS execution_snapshot_id, progress.last_progress_at, "
                    "progress.progress_observation FROM "
                    "yoao_jobs_v1.active_cross_frame_job("
                    "CAST(:actor_id AS pg_catalog.uuid), "
                    "CAST(:singleflight_key AS pg_catalog.text)) AS job "
                    "CROSS JOIN LATERAL "
                    "yoao_jobs_v1.read_job_progress(job.id) AS progress"
                )
            ),
            {"actor_id": actor_id, "singleflight_key": singleflight_key},
        )
        return result.scalar_one_or_none()

    async def cross_frame_page(self, actor_id, scene_id, limit):
        result = await self.db.execute(
            select(AsyncJob).from_statement(
                text(
                    "SELECT job.*, "
                    "yoao_jobs_v1.read_job_execution_snapshot(job.id) "
                    "AS execution_snapshot_id, progress.last_progress_at, "
                    "progress.progress_observation FROM "
                    "yoao_jobs_v1.cross_frame_job_page("
                    "CAST(:actor_id AS pg_catalog.uuid), "
                    "CAST(:scene_id AS pg_catalog.text), "
                    "CAST(:limit AS pg_catalog.int4)) AS job "
                    "CROSS JOIN LATERAL "
                    "yoao_jobs_v1.read_job_progress(job.id) AS progress"
                )
            ),
            {"actor_id": actor_id, "scene_id": scene_id, "limit": limit},
        )
        return list(result.scalars())

    async def cross_frame_count(self, actor_id, scene_id):
        if self.db.autoflush:
            await self.db.flush()
        result = await self.db.execute(
            text(
                "SELECT yoao_jobs_v1.cross_frame_job_count("
                "CAST(:actor_id AS pg_catalog.uuid), "
                "CAST(:scene_id AS pg_catalog.text))"
            ),
            {"actor_id": actor_id, "scene_id": scene_id},
        )
        return result.scalar_one()

    async def latest_batch_prediction_times(self, project_ids):
        if self.db.autoflush:
            await self.db.flush()
        result = await self.db.execute(
            text(
                "SELECT * FROM yoao_jobs_v1.latest_batch_prediction_times("
                "CAST(:project_ids AS pg_catalog.uuid[]))"
            ),
            {"project_ids": project_ids},
        )
        return result.all()

    async def frozen_deployment_usage(
        self, request: FrozenDeploymentUsageRequest
    ) -> FrozenDeploymentUsage:
        """Read owner facts in the caller's fenced transaction."""
        if self.db.autoflush:
            await self.db.flush()
        result = await self.db.execute(
            text(
                "SELECT active_jobs, retryable_failures FROM "
                "yoao_jobs_v1.frozen_deployment_usage("
                "CAST(:deployment_id AS uuid), CAST(:registry_id AS uuid), "
                "CAST(:model_version_id AS uuid))"
            ),
            request.model_dump(),
        )
        return FrozenDeploymentUsage.model_validate(dict(result.mappings().one()))

    async def lock_action_idempotency(self, *, action, project_id, actor_id, key):
        await self.db.execute(
            text(
                "SELECT yoao_jobs_v1.lock_action_idempotency("
                "CAST(:action AS pg_catalog.text), CAST(:project_id AS pg_catalog.uuid), "
                "CAST(:actor_id AS pg_catalog.uuid), CAST(:key AS pg_catalog.text))"
            ),
            {
                "action": action,
                "project_id": project_id,
                "actor_id": actor_id,
                "key": key,
            },
        )

    async def find_action_idempotent_job(self, *, kind, project_id, actor_id, key):
        return await self.db.scalar(
            select(AsyncJob).from_statement(
                text(
                    "SELECT job.*, "
                    "yoao_jobs_v1.read_job_execution_snapshot(job.id) "
                    "AS execution_snapshot_id, progress.last_progress_at, "
                    "progress.progress_observation FROM "
                    "yoao_jobs_v1.find_action_idempotent_jobs("
                    "CAST(:kind AS pg_catalog.text), CAST(:project_id AS pg_catalog.uuid), "
                    "CAST(:actor_id AS pg_catalog.uuid), CAST(:key AS pg_catalog.text)) "
                    "AS job CROSS JOIN LATERAL "
                    "yoao_jobs_v1.read_job_progress(job.id) AS progress"
                )
            ),
            {"kind": kind, "project_id": project_id, "actor_id": actor_id, "key": key},
        )

    async def dataset_job_visible(self, actor_id, job_id):
        if self.db.autoflush:
            await self.db.flush()
        return bool(
            await self.db.scalar(
                text(
                    "SELECT yoao_jobs_v1.dataset_job_visible("
                    "CAST(:actor_id AS pg_catalog.uuid), CAST(:job_id AS pg_catalog.uuid))"
                ),
                {"actor_id": actor_id, "job_id": job_id},
            )
        )

    async def directory_job_page(self, actor_id, location_id, limit, offset):
        if self.db.autoflush:
            await self.db.flush()
        rows = await self.db.execute(
            text(
                "SELECT id, kind, status, created_at, completed_at "
                "FROM yoao_jobs_v1.directory_job_page("
                "CAST(:actor_id AS pg_catalog.uuid), CAST(:location_id AS pg_catalog.uuid), "
                "CAST(:limit AS pg_catalog.int4), CAST(:offset AS pg_catalog.int4)) "
                "ORDER BY created_at DESC, id"
            ),
            {
                "actor_id": actor_id,
                "location_id": location_id,
                "limit": limit,
                "offset": offset,
            },
        )
        return list(rows.mappings())

    async def directory_job_total(self, actor_id, location_id):
        if self.db.autoflush:
            await self.db.flush()
        return await self.db.scalar(
            text(
                "SELECT yoao_jobs_v1.directory_job_total("
                "CAST(:actor_id AS pg_catalog.uuid), CAST(:location_id AS pg_catalog.uuid))"
            ),
            {"actor_id": actor_id, "location_id": location_id},
        )
