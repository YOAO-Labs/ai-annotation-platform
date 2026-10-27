"""Narrow inventory migration: preserve every saved ownership/lifecycle fence."""

from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError

from app.db.models.export_media_bundle import ExportMediaBundle as Bundle
from tests.test_export_media_bundle_models import (
    _migration,
    _values,
    owned_db as owned_db,
)

REVISION = "0236_export_media_empty_raw"


def test_migration_changes_exactly_one_bound_and_retains_all_other_guards():
    migration = _migration(None, REVISION)
    original = (
        "CREATE OR REPLACE FUNCTION test.guard() RETURNS trigger AS $$ BEGIN "
        + migration.OLD_BOUND
        + "; IMMUTABLE; RESTORE_GUARD; LIFECYCLE; END $$"
    )
    changed = migration._definition(original, upgrade=True)
    assert changed == original.replace(migration.OLD_BOUND, migration.NEW_BOUND)
    assert migration._definition(changed, upgrade=False) == original
    assert migration.revision == "0236" and migration.down_revision == "0235"
    for unexpected in (None, "missing", original + original, changed):
        with pytest.raises(RuntimeError, match="Unexpected"):
            migration._definition(unexpected, upgrade=True)


@pytest.mark.parametrize("exists", [True, False])
def test_downgrade_checks_any_retained_zero_responsibility_before_replacing_guard(
    monkeypatch, exists
):
    migration = _migration(None, REVISION)
    definitions = iter([exists, "before " + migration.NEW_BOUND + " after"])
    executed = []
    bind = SimpleNamespace(scalar=lambda statement: next(definitions))
    monkeypatch.setattr(
        migration, "op", SimpleNamespace(get_bind=lambda: bind, execute=executed.append)
    )
    if exists:
        with pytest.raises(RuntimeError, match="zero-byte.*responsibility"):
            migration.downgrade()
        assert executed == ["LOCK TABLE export_media_bundles IN ACCESS EXCLUSIVE MODE"]
    else:
        migration.downgrade()
        assert executed == [
            "LOCK TABLE export_media_bundles IN ACCESS EXCLUSIVE MODE",
            "before " + migration.OLD_BOUND + " after",
        ]


async def test_database_empty_inventory_member_is_immutable_and_prevents_downgrade(
    owned_db,
):
    db = owned_db
    connection = await db.connection()
    query = text(
        "SELECT pg_get_functiondef('guard_export_media_bundle()'::regprocedure)"
    )
    before = await db.scalar(query)
    await connection.run_sync(lambda sync: _migration(sync, REVISION).upgrade())
    assert await db.scalar(query) == before.replace(
        "member_size <= 0", "member_size < 0"
    )
    values = _values()
    entry = next(iter(values["inventory"]["objects"].values()))
    entry.update(size_bytes=0, sha256=hashlib.sha256(b"").hexdigest())
    values["size_bytes"] = 0
    row = Bundle(**values)
    db.add(row)
    await db.flush()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            changed = deepcopy(row.inventory)
            next(iter(changed["objects"].values()))["size_bytes"] = 1
            await db.execute(
                update(Bundle)
                .where(Bundle.id == row.id)
                .values(inventory=changed)
                .execution_options(synchronize_session=False)
            )
    with pytest.raises(RuntimeError, match="zero-byte.*responsibility"):
        await connection.run_sync(lambda sync: _migration(sync, REVISION).downgrade())
    assert await db.scalar(query) == before.replace(
        "member_size <= 0", "member_size < 0"
    )


async def test_database_empty_migration_roundtrip_changes_only_size_lower_bound(
    owned_db,
):
    connection = await owned_db.connection()
    query = text(
        "SELECT pg_get_functiondef('guard_export_media_bundle()'::regprocedure)"
    )
    before = await owned_db.scalar(query)
    await connection.run_sync(lambda sync: _migration(sync, REVISION).upgrade())
    await connection.run_sync(lambda sync: _migration(sync, REVISION).downgrade())
    assert await owned_db.scalar(query) == before
