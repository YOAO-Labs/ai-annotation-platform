"""Empty schemas can roll back; retired locator history cannot be discarded."""

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, text

from app.db.models.managed_original_retirement import ManagedOriginalRetirement
from tests.migration_helpers import bind, historical_schema, load


def migration(sync, monkeypatch, filename="0216_managed_original_retirements.py"):
    path = Path(__file__).parents[1] / "alembic/versions" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module, "op", Operations(MigrationContext.configure(sync.connection()))
    )
    return module


async def test_empty_managed_retirement_schema_roundtrip(test_engine):
    async with historical_schema(test_engine, "0219") as (_schema, engine):
        async with engine.begin() as connection:

            def verify(sync):
                module = bind(load("0216_managed_original_retirements.py"), sync)
                retention = bind(load("0217_managed_original_retention.py"), sync)
                verification = bind(
                    load("0218_managed_original_verifications.py"), sync
                )
                creation = bind(load("0219_managed_original_creations.py"), sync)
                assert module.down_revision == "0215"
                creation.downgrade()
                verification.downgrade()
                retention.downgrade()
                module.downgrade()
                assert "managed_original_retirements" not in inspect(
                    sync
                ).get_table_names(schema=_schema)
                module.upgrade()
                actual = inspect(sync)
                assert not actual.get_foreign_keys("managed_original_retirements")
                assert {
                    column["name"]
                    for column in actual.get_columns("managed_original_retirements")
                } == set(ManagedOriginalRetirement.__table__.columns.keys()) - {
                    "creation_id"
                }
                assert {
                    index["name"]
                    for index in actual.get_indexes("managed_original_retirements")
                } >= {
                    "ix_managed_retirement_location",
                    "ix_managed_retirement_asset",
                }
                assert any(
                    unique["column_names"] == ["assertion_digest"]
                    for unique in actual.get_unique_constraints(
                        "managed_original_retirements"
                    )
                )
                retention.upgrade()
                verification.upgrade()
                creation.upgrade()

            await connection.run_sync(verify)


async def test_retirement_assertions_block_destructive_downgrade(
    db_session, monkeypatch
):
    record = ManagedOriginalRetirement(
        id=uuid.uuid4(),
        assertion_digest="a" * 64,
        dataset_id=uuid.uuid4(),
        dataset_item_id=uuid.uuid4(),
        asset_id=uuid.uuid4(),
        location_id=uuid.uuid4(),
        bucket="historical-datasets",
        object_key="private/original",
        role="managed_original",
        identity_state="unknown",
        identity_error_code="platform_location_unproven",
        asset_verification_state="unverified",
        reference_state="unverified",
        dataset_lifecycle_revision=1,
    )
    db_session.add(record)
    await db_session.flush()

    def verify(sync):
        module = migration(sync, monkeypatch)
        with pytest.raises(RuntimeError, match="retirement assertions"):
            module.downgrade()
        assert sync.execute(
            text(
                "SELECT assertion_digest, retain_until FROM managed_original_retirements WHERE id=:id"
            ),
            {"id": record.id},
        ).one() == ("a" * 64, None)
        assert all(
            foreign_key["referred_table"] == "managed_original_creations"
            for foreign_key in inspect(sync.connection()).get_foreign_keys(
                "managed_original_retirements"
            )
        )

    await db_session.run_sync(verify)
