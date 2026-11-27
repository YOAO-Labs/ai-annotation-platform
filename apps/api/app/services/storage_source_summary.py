"""Bounded navigation over visible sources and their retained job attempts."""

from sqlalchemy import func, select, text

from app.services import dataset_source_access as access
from app.services import storage_location
from app.services.assets_dataset_data import visible_sources
from app.services.jobs_domain_query_data import JobsDomainQueryPostgresData
from app.services.storage_source_diagnostics import diagnostics
from app.services.storage_source_usage import read_usage
from app.services.dataset_scan_data import external_protection


async def summary(db, location_id, actor, *, limit, offset, jobs_limit, jobs_offset):
    actor = await access.require_manager(db, actor.id)
    location = await storage_location.get_visible(db, location_id, actor)
    usage = await read_usage(db, location, actor)
    sources = visible_sources(actor.id, location_id=location_id)
    totals = (
        await db.execute(
            select(func.count(), func.count(func.distinct(sources.c.dataset_id)))
        )
    ).one()
    rows = (
        await db.execute(
            select(sources)
            .order_by(sources.c.created_at.desc(), sources.c.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    dataset_ids = {source.dataset_id for source in rows if source.dataset_id}
    names = dict(
        (
            await db.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.directory_dataset_names("
                    "CAST(:actor_id AS uuid), CAST(:location_id AS uuid), CAST(:ids AS uuid[]))"
                ),
                {
                    "actor_id": actor.id,
                    "location_id": location_id,
                    "ids": list(dataset_ids),
                },
            )
        ).all()
    )
    evidence = await diagnostics(db, [source.id for source in rows], actor)
    jobs = JobsDomainQueryPostgresData(db)
    job_total = await jobs.directory_job_total(actor.id, location_id)
    job_rows = await jobs.directory_job_page(
        actor.id, location_id, jobs_limit, jobs_offset
    )
    return {
        "location_id": location_id,
        "usage": usage,
        "items": [
            {
                "source_id": source.id,
                "source_name": source.name,
                "dataset_id": source.dataset_id,
                "dataset_name": names.get(source.dataset_id),
                "enabled": source.enabled,
                "unregistered_at": source.unregistered_at,
                "external_protection": external_protection(source),
                "diagnostics": evidence[source.id],
            }
            for source in rows
        ],
        "total": totals[0],
        "dataset_count": totals[1],
        "jobs": {"items": list(job_rows), "total": job_total},
    }
