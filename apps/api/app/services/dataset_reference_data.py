"""Read saved directory-reference facts for physical ownership checks."""

from sqlalchemy import text


async def directory_reference_reclaim_facts(
    db, item_id, *, run_id=None, path_digest=None
):
    if db.autoflush:
        await db.flush()
    return (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.directory_reference_reclaim_facts("
                "CAST(:item AS uuid), CAST(:run AS uuid), :digest)"
            ),
            {"item": item_id, "run": run_id, "digest": path_digest},
        )
    ).one_or_none()
