"""Directory-owned derivatives never enter the legacy presigned media path."""

from sqlalchemy import case, select

from app.db.models.dataset_source import DatasetSourceRevision


def effective_cache_policy(source, location):
    return (
        "preview"
        if source.cache_policy == "preview"
        and location.config.get("cache_policy", "preview") == "preview"
        else "none"
    )


async def owns_media(db, item_id):
    return (
        bool(
            await db.scalar(
                select(DatasetSourceRevision.source_id)
                .where(
                    DatasetSourceRevision.dataset_item_id == item_id,
                    DatasetSourceRevision.owns_item.is_(True),
                )
                .limit(1)
            )
        )
        if item_id
        else False
    )


def legacy_media_column(column):
    from app.db.models.dataset import DatasetItem

    owned = (
        select(DatasetSourceRevision.source_id)
        .where(
            DatasetSourceRevision.dataset_item_id == DatasetItem.id,
            DatasetSourceRevision.owns_item.is_(True),
        )
        .correlate(DatasetItem)
        .exists()
    )
    return case((owned, None), else_=column).label(column.key)
