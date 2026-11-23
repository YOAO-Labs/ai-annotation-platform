"""Use Assets capabilities on the caller's dataset transaction."""

from sqlalchemy import String, Uuid, cast, column, func, select, text

from app.db.models.dataset_source import DatasetSource


async def dataset_metadata(
    db, dataset_id, *, lock_nowait=False, lock_share_nowait=False
):
    if db.autoflush:
        await db.flush()
    return (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.dataset_metadata("
                "CAST(:dataset_id AS uuid), :lock_nowait, :lock_share_nowait)"
            ),
            {
                "dataset_id": dataset_id,
                "lock_nowait": lock_nowait,
                "lock_share_nowait": lock_share_nowait,
            },
        )
    ).one_or_none()


async def project_dataset_exists(db, project_id, dataset_id):
    if db.autoflush:
        await db.flush()
    return await db.scalar(
        select(
            func.yoao_assets_v1.project_has_dataset(
                cast(project_id, Uuid), cast(dataset_id, Uuid)
            )
        )
    )


def dataset_project_ids(dataset_id):
    return func.yoao_assets_v1.dataset_project_ids(cast(dataset_id, Uuid)).table_valued(
        column("project_id", Uuid)
    )


def dataset_scene_choices(dataset_id, scene_id=None):
    return func.yoao_assets_v1.dataset_scene_choices(
        cast(dataset_id, Uuid), cast(scene_id, Uuid)
    ).table_valued(
        column("id", Uuid), column("display_id", String), column("name", String)
    )


def visible_sources(actor_id, *, dataset_id=None, location_id=None, source_id=None):
    return func.yoao_assets_v1.visible_dataset_sources(
        cast(actor_id, Uuid),
        cast(dataset_id, Uuid),
        cast(location_id, Uuid),
        cast(source_id, Uuid),
    ).table_valued(*(column(col.name, col.type) for col in DatasetSource.__table__.c))
