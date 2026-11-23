"""Directory-owned derivatives never enter the legacy presigned media path."""

from sqlalchemy import Boolean, String, Uuid, case, cast, column, func, select


def effective_cache_policy(source, location):
    ranks = {"none": 0, "preview": 1, "on_demand": 2}
    return min(
        source.cache_policy,
        location.config.get("cache_policy", "preview"),
        key=lambda value: ranks.get(value, 0),
    )


def permits_preview(value):
    return value in {"preview", "on_demand"}


def preview_policy_clause():
    from sqlalchemy import and_, func
    from app.db.models.dataset_asset import StorageLocation
    from app.db.models.dataset_source import DatasetSource

    return and_(
        DatasetSource.cache_policy.in_(["preview", "on_demand"]),
        func.coalesce(StorageLocation.config["cache_policy"].astext, "preview").in_(
            ["preview", "on_demand"]
        ),
    )


async def owns_media(db, item_id):
    if item_id is None:
        return False
    if db.autoflush:
        await db.flush()
    return bool(
        await db.scalar(
            select(
                func.yoao_assets_v1.dataset_owns_directory_media(cast(item_id, Uuid))
            )
        )
    )


def legacy_cover_facts(task):
    facts = (
        func.yoao_assets_v1.dataset_cover_facts(task.dataset_item_id)
        .table_valued(
            column("file_type", String),
            column("thumbnail_path", String),
            column("blurhash", String),
            column("directory_owned", Boolean),
        )
        .lateral("dataset_cover")
    )
    thumbnail = case(
        (facts.c.directory_owned, None),
        else_=func.coalesce(facts.c.thumbnail_path, task.thumbnail_path),
    ).label("thumbnail_path")
    blurhash = case(
        (facts.c.directory_owned, None),
        else_=func.coalesce(facts.c.blurhash, task.blurhash),
    ).label("blurhash")
    return facts, thumbnail, blurhash
