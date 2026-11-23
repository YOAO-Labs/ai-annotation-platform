"""Review accepted-path selection changes without relocating business content."""

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import text

from app.services.dataset_source_access import require_source
from app.services.dataset_source_lifecycle import active_attempts
from app.services import dataset_scans as scans, source_selection as selection
from app.services.dataset_content import key_authority
from app.services.audit import AuditService


async def preview(
    db, source_id, payload, actor, *, limit=20, offset=0, api_key_id=None
):
    original_proof = await key_authority(db, actor, api_key_id, "datasets:write")
    location, source = await scans.source_context(db, source_id)
    await scans.require_actor(db, actor.id, source)
    dataset = await scans.require_dataset_write(db, source)
    if original_proof != await key_authority(db, actor, api_key_id, "datasets:write"):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    if source.unregistered_at or source.config_revision != payload.expected_revision:
        raise HTTPException(409, detail={"code": "rules_impact_changed"})
    rows = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.directory_source_selection_heads("
                "CAST(:actor_id AS uuid), CAST(:source_id AS uuid))"
            ),
            {"actor_id": actor.id, "source_id": source.id},
        )
    ).all()
    if len(rows) > 50000:
        raise HTTPException(409, detail={"code": "comparison_budget_exceeded"})
    attempts = [
        (str(row.id), row.generation, row.status)
        for row in await active_attempts(db, actor.id, source.id)
    ]
    if any(path is None for _, _, path in rows):
        raise HTTPException(409, detail={"code": "content_binding_changed"})
    rules = payload.model_dump(include=selection.RULE_KEYS)
    candidate = {**source.config, **rules}
    counts = {
        "within_scope": 0,
        "leaving_scope": 0,
        "entering_scope": 0,
        "outside_scope": 0,
    }
    items = []
    for digest, revision_id, path in rows:
        before = selection.selected_file(source.config, path, location.kind)
        after = selection.selected_file(candidate, path, location.kind)
        impact = (
            ("within_scope" if before else "entering_scope")
            if after
            else ("leaving_scope" if before else "outside_scope")
        )
        counts[impact] += 1
        if offset <= sum(counts.values()) - 1 < offset + limit:
            items.append(
                {"path_digest": digest, "relative_path": path, "impact": impact}
            )
    proof = hashlib.sha256(
        json.dumps(
            {
                "source": str(source.id),
                "source_revision": source.config_revision,
                "location_revision": location.config_revision,
                "dataset_lifecycle_revision": dataset.lifecycle_revision,
                "config": source.config,
                "rules": rules,
                "heads": [(d, str(r)) for d, r, _ in rows],
                "attempts": attempts,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {
        "source_id": source.id,
        "expected_revision": source.config_revision,
        "proof": proof,
        "counts": counts,
        "items": items,
        "total": len(rows),
        "active_attempt_count": len(attempts),
    }


async def apply(db, source_id, payload, actor, api_key_id=None):
    original_proof = await key_authority(db, actor, api_key_id, "datasets:write")
    impact = await preview(db, source_id, payload, actor, api_key_id=api_key_id)
    if impact["proof"] != payload.impact_proof:
        raise HTTPException(409, detail={"code": "rules_impact_changed"})
    if impact["active_attempt_count"]:
        raise HTTPException(409, detail={"code": "source_busy"})
    _, source = await scans.source_context(db, source_id)
    await scans.require_actor(db, actor.id, source)
    await scans.require_dataset_write(db, source)
    if original_proof != await key_authority(db, actor, api_key_id, "datasets:write"):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    rules = payload.model_dump(include=selection.RULE_KEYS)
    old = {key: source.config[key] for key in selection.RULE_KEYS}
    if old == rules:
        return source
    changed = await db.scalar(
        text(
            "SELECT yoao_assets_v1.apply_directory_source_selection("
            "CAST(:actor_id AS uuid), CAST(:source_id AS uuid), "
            ":revision, :generation, CAST(:rules AS jsonb))"
        ),
        {
            "actor_id": actor.id,
            "source_id": source.id,
            "revision": source.config_revision,
            "generation": source.generation,
            "rules": json.dumps(rules, separators=(",", ":")),
        },
    )
    if not changed:
        raise HTTPException(409, detail={"code": "rules_impact_changed"})
    await AuditService.log(
        db,
        actor=actor,
        action="dataset_source.rules_update",
        target_type="dataset_source",
        target_id=str(source.id),
        detail={"before": old, "after": rules, "counts": impact["counts"]},
        status_code=200,
    )
    return await require_source(db, source_id, actor)
