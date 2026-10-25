"""Additive import facts survive rollback and prevent destructive downgrade."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.mask_format_upload import MaskFormatUpload
from app.db.models.prediction_input import PredictionInputLease
from app.services import import_storage
from tests.test_export_media_bundle_models import _migration


@pytest.fixture
async def isolated_import_db(test_engine):
    schema = "import_migration_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            for name in (
                "prediction_input_leases",
                "managed_original_creations",
                "mask_format_imports",
                "mask_format_uploads",
            ):
                await connection.execute(
                    text(
                        f'CREATE TABLE "{name}" (LIKE public."{name}" INCLUDING DEFAULTS INCLUDING CONSTRAINTS)'
                    )
                )
            for name, table, column in (
                (
                    "ix_prediction_input_location",
                    "prediction_input_leases",
                    "storage_ref",
                ),
                (
                    "ix_managed_creation_staging_location",
                    "managed_original_creations",
                    "staging_ref",
                ),
                (
                    "ix_mask_format_import_location",
                    "mask_format_imports",
                    "staging_ref",
                ),
                (
                    "ix_mask_format_upload_location",
                    "mask_format_uploads",
                    "storage_ref",
                ),
            ):
                await connection.execute(
                    text(f"CREATE INDEX {name} ON {table} (({column}->>'location_id'))")
                )
            await connection.execute(
                text(
                    "CREATE FUNCTION guard_mask_format_import_target() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
                )
            )
            for table, trigger in (
                ("mask_format_uploads", "trg_mask_format_upload_target"),
                ("mask_format_imports", "trg_mask_format_import_target"),
            ):
                await connection.execute(
                    text(
                        f"CREATE TRIGGER {trigger} BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION guard_mask_format_import_target()"
                    )
                )
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db, schema
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


async def test_empty_import_migration_roundtrip_preserves_legacy_rows(
    isolated_import_db,
):
    db, schema = isolated_import_db
    connection = await db.connection()

    def roundtrip(sync):
        migration = _migration(sync, "0250_import_storage_targets")
        migration.downgrade()
        assert (
            sync.scalar(
                text("SELECT to_regclass(:name)"),
                {"name": schema + ".mask_format_uploads"},
            )
            is None
        )
        migration.upgrade()
        for name in (
            "ix_mask_format_upload_location",
            "ix_mask_format_import_location",
            "ix_managed_creation_staging_location",
            "ix_prediction_input_location",
        ):
            assert sync.scalar(
                text("SELECT to_regclass(:name)"), {"name": schema + "." + name}
            )

    await connection.run_sync(roundtrip)


@pytest.mark.parametrize("kind", ["upload", "lease_schema2", "lease_frame"])
async def test_import_facts_refuse_destructive_downgrade(isolated_import_db, kind):
    db_session, _ = isolated_import_db
    identifier, started = uuid.uuid4(), datetime.now(timezone.utc)
    if kind == "upload":
        project, actor = uuid.uuid4(), uuid.uuid4()
        prefix = f"mask-formats/{project}/{actor}/{identifier}/"
        await db_session.execute(
            insert(MaskFormatUpload).values(
                id=identifier,
                project_id=project,
                uploader_id=actor,
                logical_object_key=prefix + "input.json",
                content_type="application/json",
                storage_ref=import_storage.legacy_ref(prefix),
                created_at=started,
                upload_expires_at=started + timedelta(seconds=900),
            )
        )
    else:
        ref = import_storage.legacy_ref(f"prediction-inputs/{identifier}/")
        if kind == "lease_schema2":
            ref.update(schema=2, binding_generation=2)
        await db_session.execute(
            insert(PredictionInputLease).values(
                id=identifier,
                owner_id=uuid.uuid4(),
                task_id=uuid.uuid4(),
                owner_kind="frame_predict" if kind == "lease_frame" else "predict_test",
                contract_version=2,
                storage_ref=ref,
                maintenance_revision=1,
                inventory={"schema": 1, "objects": {}},
                bucket=ref["bucket"],
                storage_identity=ref["storage_identity"],
                created_at=started,
                expires_at=started + timedelta(hours=2),
                retain_until=started + timedelta(hours=2, days=7),
                next_cleanup_at=started + timedelta(hours=2),
                state="active",
                uncertain_write=False,
            )
        )
    connection = await db_session.connection()
    async with connection.begin_nested() as savepoint:
        with pytest.raises(Exception, match="Saved import responsibility"):
            await connection.run_sync(
                lambda sync: _migration(sync, "0250_import_storage_targets").downgrade()
            )
        await savepoint.rollback()
    assert await db_session.scalar(text("SELECT to_regclass('mask_format_uploads')"))
