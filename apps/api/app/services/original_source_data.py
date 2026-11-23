"""Assets original identities and source locks on the caller's connection."""

from sqlalchemy import select, text

from app.services.assets_spatial_views import labeling_dataset_item_facts

_ITEM_LOCK = text("SELECT * FROM yoao_assets_v1.lock_original_item(:item_id, :wait)")
_ASSET_ITEM = text(
    "SELECT * FROM yoao_assets_v1.original_asset_for_item(:item_id, :lock)"
)
_REPLICAS = text(
    "SELECT * FROM yoao_assets_v1.managed_original_locations(:asset_id, :lock, :bounded)"
)
_TASK_SOURCE = text("SELECT * FROM yoao_assets_v1.task_original_source(:id, :lock)")
_AUX_SOURCE = text("SELECT * FROM yoao_assets_v1.auxiliary_original_source(:id, :lock)")
_AUX_ITEM = text("SELECT * FROM yoao_assets_v1.auxiliary_original_item(:id, :lock)")
_AUX_SCENE = text("SELECT * FROM yoao_assets_v1.auxiliary_original_scene(:id, :lock)")


async def item_source_facts(db, item_id, *, lock=False, wait=False):
    if not lock:
        item = labeling_dataset_item_facts
        statement = select(
            item.id,
            item.dataset_id,
            item.file_path,
            item.file_type,
            item.width,
            item.height,
        ).where(item.id == item_id)
        return (await db.execute(statement)).one_or_none()
    statement = _ITEM_LOCK
    parameters = {"item_id": item_id, "wait": wait}
    if not wait:
        return (await db.execute(statement, parameters)).one_or_none()
    previous = await db.scalar(
        text("SELECT setting FROM pg_catalog.pg_settings WHERE name = 'lock_timeout'")
    )
    if 0 < int(previous) <= 1000:
        return (await db.execute(statement, parameters)).one_or_none()
    await db.execute(text("SET LOCAL lock_timeout = '1s'"))
    result = await db.execute(statement, parameters)
    await db.execute(
        text("SELECT pg_catalog.set_config('lock_timeout', :timeout, true)"),
        {"timeout": previous + "ms"},
    )
    return result.one_or_none()


async def asset_for_item(db, item_id, *, lock=False):
    return (
        await db.execute(_ASSET_ITEM, {"item_id": item_id, "lock": lock})
    ).one_or_none()


async def managed_locations(db, asset_id, *, lock=False, bounded=False):
    return (
        await db.execute(
            _REPLICAS, {"asset_id": asset_id, "lock": lock, "bounded": bounded}
        )
    ).all()


async def task_source(db, identifier, *, lock=False):
    return (
        await db.execute(_TASK_SOURCE, {"id": identifier, "lock": lock})
    ).one_or_none()


async def auxiliary_source(db, identifier, *, lock=False):
    result = await db.execute(_AUX_SOURCE, {"id": identifier, "lock": lock})
    return result.mappings().one_or_none()


async def auxiliary_item(db, identifier, *, lock=False):
    return (await db.execute(_AUX_ITEM, {"id": identifier, "lock": lock})).one_or_none()


async def auxiliary_scene(db, identifier, *, lock=False):
    return (
        await db.execute(_AUX_SCENE, {"id": identifier, "lock": lock})
    ).one_or_none()


async def persist_task_source(connection, creation_id):
    return (
        (
            await connection.execute(
                text("SELECT * FROM yoao_assets_v1.publish_task_original_source(:id)"),
                {"id": creation_id},
            )
        )
        .mappings()
        .one_or_none()
    )


async def persist_auxiliary_source(connection, creation_id):
    return (
        (
            await connection.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.publish_auxiliary_original_source(:id)"
                ),
                {"id": creation_id},
            )
        )
        .mappings()
        .one_or_none()
    )


async def managed_location_ref(db, asset_id, location_id, bucket, object_key):
    return await db.scalar(
        text(
            "SELECT yoao_assets_v1.managed_original_location_ref(:asset, :location, :bucket, :key)"
        ),
        {
            "asset": asset_id,
            "location": location_id,
            "bucket": bucket,
            "key": object_key,
        },
    )
