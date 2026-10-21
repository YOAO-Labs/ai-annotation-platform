"""Read current organization IDs through Identity's fixed capability."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text


async def list_actor_organization_ids(db, actor_id: uuid.UUID) -> list[uuid.UUID]:
    if db.autoflush:
        await db.flush()
    raw = await db.scalar(
        text(
            "SELECT aap_identity_v1.workspace_template_query_org_ids(CAST(:actor AS uuid))"
        ),
        {"actor": actor_id},
    )
    if isinstance(raw, str):
        raw = json.loads(raw)
    if (
        type(raw) is not dict
        or set(raw) != {"schema", "actor_id", "organization_ids"}
        or raw["schema"] != "aap-identity-workspace-template-query/v1"
        or raw["actor_id"] != str(actor_id)
        or type(raw["organization_ids"]) is not list
    ):
        raise RuntimeError("Invalid Identity organization facts")
    return [uuid.UUID(value) for value in raw["organization_ids"]]
