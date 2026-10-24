"""Borrowed-session binding for the Jobs owner's fixed domain queries."""

from sqlalchemy import select, text

from app.db.models.async_job import AsyncJob


class JobsDomainQueryPostgresData:
    def __init__(self, db):
        self.db = db

    async def lock_cross_frame_singleflight(self, actor_id, singleflight_key):
        await self.db.execute(
            text(
                "SELECT aap_jobs_v1.lock_cross_frame_singleflight("
                "CAST(:actor_id AS pg_catalog.uuid), "
                "CAST(:singleflight_key AS pg_catalog.text))"
            ),
            {"actor_id": actor_id, "singleflight_key": singleflight_key},
        )

    async def active_cross_frame_job(self, actor_id, singleflight_key):
        result = await self.db.execute(
            select(AsyncJob).from_statement(
                text(
                    "SELECT * FROM aap_jobs_v1.active_cross_frame_job("
                    "CAST(:actor_id AS pg_catalog.uuid), "
                    "CAST(:singleflight_key AS pg_catalog.text))"
                )
            ),
            {"actor_id": actor_id, "singleflight_key": singleflight_key},
        )
        return result.scalar_one_or_none()

    async def cross_frame_page(self, actor_id, scene_id, limit):
        result = await self.db.execute(
            select(AsyncJob).from_statement(
                text(
                    "SELECT * FROM aap_jobs_v1.cross_frame_job_page("
                    "CAST(:actor_id AS pg_catalog.uuid), "
                    "CAST(:scene_id AS pg_catalog.text), "
                    "CAST(:limit AS pg_catalog.int4))"
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
                "SELECT aap_jobs_v1.cross_frame_job_count("
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
                "SELECT * FROM aap_jobs_v1.latest_batch_prediction_times("
                "CAST(:project_ids AS pg_catalog.uuid[]))"
            ),
            {"project_ids": project_ids},
        )
        return result.all()
