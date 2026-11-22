"""Full-head delivery guards bind each ZIP to its saved media profile."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from tests import test_export_media_bundle_models as models
from tests.test_video_export_media_domain import video_inputs


@pytest.mark.parametrize("video", [False, True], ids=["legacy-image", "video"])
async def test_full_head_delivery_pair_uses_saved_media_profile(test_engine, video):
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            async with async_sessionmaker(
                connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )() as db:
                values = models._values()
                if video:
                    project_id, snapshot, _ = video_inputs(tasks=1, size=11)
                    values.update(project_id=project_id, input_snapshot=snapshot)
                    key = next(iter(values["inventory"]["objects"]))
                    values["logical_paths"]["targets"] = {"mot": {"clip.mp4": key}}
                bundle = await models._new(db, **values)
                await models._constraints(db)
                media = "video" if video else "image"
                with pytest.raises(IntegrityError) as mismatch:
                    async with db.begin_nested():
                        await models._zip(
                            db,
                            bundle,
                            media="image" if video else "video",
                            created_at=bundle.created_at,
                        )
                        await models._constraints(db)
                assert mismatch.value.orig.sqlstate == "23514"
                assert (
                    mismatch.value.orig.__cause__.constraint_name
                    == "ck_export_media_pair"
                )

                output = await models._zip(
                    db,
                    bundle,
                    media=media,
                    created_at=bundle.created_at,
                    format="mot" if video else "pointmask",
                )
                await models._constraints(db)
                await models._publish(db, bundle, output)
                await db.commit()
                assert (
                    await db.scalar(
                        select(ExportMediaBundle.state).where(
                            ExportMediaBundle.id == bundle.id
                        )
                    )
                    == "ready"
                )
                assert (
                    await db.scalar(
                        select(ExportOutput.state).where(ExportOutput.id == output.id)
                    )
                    == "ready"
                )
        finally:
            await transaction.rollback()
