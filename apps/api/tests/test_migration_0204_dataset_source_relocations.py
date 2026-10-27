"""Locator ordering backfill preserves evidence and prevents unsafe downgrade."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text

from tests.test_dataset_content import content_world as content_world
from tests.test_dataset_source_relocations import moved_candidate
from app.services.dataset_references import register_entry
from app.services import dataset_source_revisions as revisions


def module(sync, monkeypatch):
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/0204_dataset_source_relocation_order.py"
    )
    spec = importlib.util.spec_from_file_location(path.stem, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.setattr(
        migration, "op", Operations(MigrationContext.configure(sync.connection()))
    )
    return migration


async def test_empty_relocation_schema_roundtrip(db_session, monkeypatch):
    def verify(sync):
        migration = module(sync, monkeypatch)
        migration.downgrade()
        migration.upgrade()
        assert sync.scalar(text("SELECT count(*) FROM dataset_source_revisions")) == 0

    await db_session.run_sync(verify)


async def test_order_backfill_and_relocation_rollback_fence(
    db_session, content_world, monkeypatch
):
    db, world = db_session, content_world
    old = await register_entry(db, world[6].id, world[7].path_digest, world[2])

    def verify(sync):
        migration = module(sync, monkeypatch)
        facts = sync.execute(
            text(
                "SELECT id,run_id,item_id_at_accept,asset_id_at_accept,accepted_at FROM dataset_source_revisions"
            )
        ).all()
        migration.downgrade()
        migration.upgrade()
        assert (
            sync.execute(
                text(
                    "SELECT id,run_id,item_id_at_accept,asset_id_at_accept,accepted_at FROM dataset_source_revisions"
                )
            ).all()
            == facts
        )
        assert (
            sync.scalar(
                text("SELECT min(acceptance_order) FROM dataset_source_revisions")
            )
            > 0
        )

    await db.run_sync(verify)
    world, run, _, target, payload = await moved_candidate(db, world)
    result = await revisions.accept(
        db, run.id, target["path_digest"], payload, world[2], relocate=True
    )
    assert result["item_id"] == old["item_id"]

    def blocked(sync):
        with pytest.raises(RuntimeError, match="current reader"):
            module(sync, monkeypatch).downgrade()

    await db.run_sync(blocked)


async def test_cross_path_predecessor_rejects_changed_identity_and_source(
    db_session, content_world
):
    import uuid
    from sqlalchemy.exc import IntegrityError
    from app.db.models.dataset_source import DatasetSourceRevision

    db, world = db_session, content_world
    await register_entry(db, world[6].id, world[7].path_digest, world[2])
    world, run, old, target, payload = await moved_candidate(db, world)
    previous = await db.get(DatasetSourceRevision, old["before_revision_id"])
    facts = dict(
        source_id=previous.source_id,
        path_digest=target["path_digest"],
        run_id=world[6].id,
        revision_number=1,
        item_id_at_accept=previous.item_id_at_accept,
        asset_id_at_accept=previous.asset_id_at_accept,
        dataset_item_id=previous.dataset_item_id,
        asset_id=previous.asset_id,
        content_sha256=previous.content_sha256,
        ownership_mode=previous.ownership_mode,
        owns_item=previous.owns_item,
        previous_revision_id=previous.id,
    )
    for field, value, error in [
        ("source_id", uuid.uuid4(), "predecessor source differs"),
        ("item_id_at_accept", uuid.uuid4(), "preserve business identity"),
        ("asset_id_at_accept", uuid.uuid4(), "preserve business identity"),
        ("content_sha256", "f" * 64, "preserve business identity"),
        ("ownership_mode", "managed_copy", "preserve business identity"),
        ("owns_item", False, "preserve business identity"),
    ]:
        with pytest.raises(IntegrityError, match=error):
            async with db.begin_nested():
                await db.execute(
                    DatasetSourceRevision.__table__.insert().values(
                        id=uuid.uuid4(), **{**facts, field: value}
                    )
                )
