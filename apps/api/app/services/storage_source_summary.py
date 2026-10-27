"""Bounded navigation over visible sources and their retained job attempts."""

from sqlalchemy import func, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset
from app.db.models.dataset_source import DatasetSource
from app.services import dataset_source_access as access
from app.services import storage_location
from app.services.storage_source_diagnostics import diagnostics
from app.services.storage_source_usage import read_usage
from app.services.dataset_source_protection import declaration


async def summary(db, location_id, actor, *, limit, offset, jobs_limit, jobs_offset):
    actor = await access.require_manager(db, actor.id)
    location = await storage_location.get_visible(db, location_id, actor)
    usage = await read_usage(db, location, actor)
    scope = (
        DatasetSource.location_id == location_id,
        access.source_scope_clause(actor),
    )
    source_ids = select(DatasetSource.id).where(*scope)
    total = await db.scalar(
        select(func.count()).select_from(DatasetSource).where(*scope)
    )
    dataset_count = await db.scalar(
        select(func.count(func.distinct(DatasetSource.dataset_id))).where(*scope)
    )
    rows = (
        await db.execute(
            select(DatasetSource, Dataset.name)
            .outerjoin(Dataset, Dataset.id == DatasetSource.dataset_id)
            .where(*scope)
            .order_by(DatasetSource.created_at.desc(), DatasetSource.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    evidence = await diagnostics(db, [source.id for source, _ in rows], actor)
    jobs = select(AsyncJob).where(access.job_scope_clause(actor, source_ids))
    job_total = await db.scalar(select(func.count()).select_from(jobs.subquery()))
    job_rows = await db.scalars(
        jobs.order_by(AsyncJob.created_at.desc(), AsyncJob.id)
        .limit(jobs_limit)
        .offset(jobs_offset)
    )
    return {
        "location_id": location_id,
        "usage": usage,
        "items": [
            {
                "source_id": source.id,
                "source_name": source.name,
                "dataset_id": source.dataset_id,
                "dataset_name": name,
                "enabled": source.enabled,
                "unregistered_at": source.unregistered_at,
                "external_protection": declaration(source),
                "diagnostics": evidence[source.id],
            }
            for source, name in rows
        ],
        "total": total,
        "dataset_count": dataset_count,
        "jobs": {"items": list(job_rows), "total": job_total},
    }
