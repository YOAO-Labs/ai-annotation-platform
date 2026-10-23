"""Directory-owned derivatives never enter the legacy presigned media path."""

from sqlalchemy import case, select

from app.db.models.dataset_source import DatasetSourceRevision


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
