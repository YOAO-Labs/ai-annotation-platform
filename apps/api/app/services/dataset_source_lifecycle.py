"""Preview and fence lifecycle changes without deleting business identities."""

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.dataset_source import (
    DatasetSourceRevision,
    DatasetSourcePreview,
    DatasetScanRun,
    DatasetRegistrationRun,
)
from app.services import dataset_scans as scans
from app.services import dataset_registration as registration
from app.services.dataset_content import key_authority


async def impact(db, source, actor):
    await scans.require_actor(db, actor.id, source)
    items = select(DatasetSourceRevision.dataset_item_id).where(
        DatasetSourceRevision.source_id == source.id,
        DatasetSourceRevision.dataset_item_id.is_not(None),
    )
    tasks = select(Task.id).where(Task.dataset_item_id.in_(items))

    def count(stmt):
        return select(func.count()).select_from(stmt.subquery()).scalar_subquery()

    ready_previews = (
        select(DatasetSourcePreview)
        .where(
            DatasetSourcePreview.source_id == source.id,
            DatasetSourcePreview.state == "ready",
        )
        .subquery()
    )
    count_queries = {
        "item_count": count(items.distinct()),
        "project_count": count(
            select(Task.project_id).where(Task.id.in_(tasks)).distinct()
        ),
        "task_count": count(tasks),
        "annotation_count": count(
            select(Annotation.id).where(Annotation.task_id.in_(tasks))
        ),
        "preview_count": select(func.count())
        .select_from(ready_previews)
        .scalar_subquery(),
        "preview_bytes": select(
            func.coalesce(func.sum(ready_previews.c.size_bytes), 0)
        ).scalar_subquery(),
    }
    counts = dict(
        (
            await db.execute(
                select(*(q.label(name) for name, q in count_queries.items()))
            )
        )
        .mappings()
        .one()
    )
    counts = {name: int(value) for name, value in counts.items()}
    attempts = []
    for model in (DatasetScanRun, DatasetRegistrationRun):
        rows = await db.execute(
            select(model.id, model.async_job_id, model.generation)
            .where(model.source_id == source.id, model.status.in_(scans.ACTIVE))
            .order_by(model.id)
        )
        attempts.extend([list(map(str, row)) for row in rows])
    counts.update(
        source_id=str(source.id),
        expected_revision=source.config_revision,
        active_attempt_count=len(attempts),
    )
    from app.db.models.dataset import Dataset

    dataset = (
        await db.get(Dataset, source.dataset_id, populate_existing=True)
        if source.dataset_id
        else None
    )
    proof = hashlib.sha256(
        json.dumps(
            {
                **counts,
                "attempts": attempts,
                "dataset_lifecycle_revision": dataset.lifecycle_revision
                if dataset
                else None,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {**counts, "proof": proof, "measured_at": await scans.now(db)}


async def change(db, source_id, payload, actor, api_key_id=None):
    proof = await key_authority(db, actor, api_key_id, "datasets:write")
    _, source = await scans.source_context(db, source_id)
    await scans.require_actor(db, actor.id, source)
    await scans.require_dataset_write(db, source)
    current = await impact(db, source, actor)
    if proof != await key_authority(db, actor, api_key_id, "datasets:write"):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    if (
        source.config_revision != payload.expected_revision
        or current["proof"] != payload.impact_proof
    ):
        raise HTTPException(409, detail={"code": "source_impact_changed"})
    if source.unregistered_at:
        raise HTTPException(409, detail={"code": "source_unregistered"})
    if (payload.action == "enable" and source.enabled) or (
        payload.action == "pause" and not source.enabled
    ):
        return source
    if payload.action != "enable":
        for model, context, finish in (
            (DatasetScanRun, scans.run_context, scans.finish),
            (DatasetRegistrationRun, registration.context, registration.finish),
        ):
            ids = list(
                await db.scalars(
                    select(model.id)
                    .where(model.source_id == source.id, model.status.in_(scans.ACTIVE))
                    .order_by(model.id)
                )
            )
            for run_id in ids:
                _, _, run, job = await context(db, run_id)
                if run.status in scans.ACTIVE:
                    await finish(db, run, job, "cancelled", "source_lifecycle_changed")
    source.enabled = payload.action == "enable"
    if payload.action == "unregister":
        source.unregistered_at = await scans.now(db)
    source.config_revision += 1
    source.generation += 1
    await db.flush()
    return source
