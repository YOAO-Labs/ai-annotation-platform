"""Preview and fence lifecycle changes without deleting business identities."""

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import text

from app.services.assets_dataset_data import dataset_metadata
from app.services.dataset_source_access import require_source
from app.services import dataset_scans as scans
from app.services import dataset_registration as registration
from app.services.dataset_content import key_authority


async def active_attempts(db, actor_id, source_id):
    return (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.directory_source_active_attempts("
                "CAST(:actor_id AS uuid), CAST(:source_id AS uuid))"
            ),
            {"actor_id": actor_id, "source_id": source_id},
        )
    ).all()


async def impact(db, source, actor):
    await scans.require_actor(db, actor.id, source)
    row = (
        (
            await db.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.directory_source_impact("
                    "CAST(:actor_id AS uuid), CAST(:source_id AS uuid))"
                ),
                {"actor_id": actor.id, "source_id": source.id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "Dataset source not found")
    counts = {name: int(value) for name, value in row.items()}
    attempts = [
        list(map(str, (attempt.id, attempt.async_job_id, attempt.generation)))
        for attempt in await active_attempts(db, actor.id, source.id)
    ]
    counts.update(
        source_id=str(source.id),
        expected_revision=source.config_revision,
        active_attempt_count=len(attempts),
    )
    dataset = (
        await dataset_metadata(db, source.dataset_id) if source.dataset_id else None
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
        for attempt in await active_attempts(db, actor.id, source.id):
            context, finish = (
                (scans.run_context, scans.finish)
                if attempt.kind == "scan"
                else (registration.context, registration.finish)
            )
            _, _, run, job = await context(db, attempt.id)
            if run.status in scans.ACTIVE:
                await finish(db, run, job, "cancelled", "source_lifecycle_changed")
    changed = await db.scalar(
        text(
            "SELECT yoao_assets_v1.change_directory_source_lifecycle("
            "CAST(:actor_id AS uuid), CAST(:source_id AS uuid), "
            ":revision, :generation, :action, :unregistered_at)"
        ),
        {
            "actor_id": actor.id,
            "source_id": source.id,
            "revision": source.config_revision,
            "generation": source.generation,
            "action": payload.action,
            "unregistered_at": await scans.now(db)
            if payload.action == "unregister"
            else None,
        },
    )
    if not changed:
        raise HTTPException(409, detail={"code": "source_impact_changed"})
    return await require_source(db, source_id, actor)
