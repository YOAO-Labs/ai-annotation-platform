"""Bounded navigation over visible sources and their retained job attempts."""

from sqlalchemy import func, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset
from app.db.models.dataset_source import DatasetSource
from app.services import dataset_source_access as access
from app.services import storage_location


async def summary(db, location_id, actor, *, limit, offset, jobs_limit, jobs_offset):
    actor = await access.require_manager(db, actor.id)
    await storage_location.get_visible(db, location_id, actor)
    scope = (
        DatasetSource.location_id == location_id,
        access.source_scope_clause(actor),
    )
    source_ids = select(DatasetSource.id).where(*scope)
    total = await db.scalar(
        select(func.count()).select_from(DatasetSource).where(*scope)
    )
    rows = await db.execute(
        select(DatasetSource, Dataset.name)
        .outerjoin(Dataset, Dataset.id == DatasetSource.dataset_id)
        .where(*scope)
        .order_by(DatasetSource.created_at.desc(), DatasetSource.id)
        .limit(limit)
        .offset(offset)
    )
    jobs = select(AsyncJob).where(access.job_scope_clause(actor, source_ids))
    job_total = await db.scalar(select(func.count()).select_from(jobs.subquery()))
    job_rows = await db.scalars(
        jobs.order_by(AsyncJob.created_at.desc(), AsyncJob.id)
        .limit(jobs_limit)
        .offset(jobs_offset)
    )
    return {
        "location_id": location_id,
        "items": [
            {
                "source_id": source.id,
                "source_name": source.name,
                "dataset_id": source.dataset_id,
                "dataset_name": name,
                "enabled": source.enabled,
                "unregistered_at": source.unregistered_at,
            }
            for source, name in rows
        ],
        "total": total,
        "jobs": {"items": list(job_rows), "total": job_total},
    }
