"""ADR-0050 · migration 0132 (ML Backend service pools) tests.

Validates the singleton-backfill, dual-id, JSONB-remap, and conditional-downgrade
contracts frozen in ADR-0050 §5 / plan appendix §B / §C.3.

These tests load the migration module directly and exercise upgrade()/downgrade()
logic via mocked ``op``, covering the machine-readable invariants that are hard to
assert through the full app test stack:

- singleton backfill: every registry → exactly one pool + one active member
- registry→pool mapping uniqueness
- downgrade fail-closed when any pool has multiple members (forward-only guard)
- downgrade succeeds when all pools are singletons
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock


def _load_migration_0132():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0132_ml_backend_service_pools.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0132", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _collected_sql(op_mock: MagicMock) -> list[str]:
    """All raw SQL strings passed to op.execute(...), in order."""
    return [c.args[0] for c in op_mock.execute.call_args_list if c.args]


def _has_alter(op_mock: MagicMock, table: str, old_col: str, new_col: str) -> bool:
    """True if alter_column(table, old_col, new_column_name=new_col) was called (loose kwargs)."""
    for c in op_mock.alter_column.call_args_list:
        args, kwargs = c.args, c.kwargs
        if len(args) >= 2 and args[0] == table and args[1] == old_col:
            if kwargs.get("new_column_name") == new_col:
                return True
    return False


def _add_column_targets(op_mock: MagicMock) -> list[str]:
    """Table names passed to op.add_column(...)."""
    return [c.args[0] for c in op_mock.add_column.call_args_list if c.args]


def _create_fk_names(op_mock: MagicMock) -> list[str]:
    """Constraint names passed to op.create_foreign_key(...)."""
    names = []
    for c in op_mock.create_foreign_key.call_args_list:
        if c.args:
            names.append(c.args[0])
        elif "constraint_name" in c.kwargs:
            names.append(c.kwargs["constraint_name"])
    return names


def _has_create_fk(
    op_mock: MagicMock,
    name: str,
    table: str,
    ref_table: str,
    cols: list[str],
    ondelete: str,
) -> bool:
    """True if create_foreign_key(name, table, ref_table, cols, ..., ondelete=ondelete) called."""
    for c in op_mock.create_foreign_key.call_args_list:
        args, kwargs = c.args, c.kwargs
        if not args:
            continue
        if (
            args[0] == name
            and len(args) >= 4
            and args[1] == table
            and args[2] == ref_table
            and list(args[3]) == cols
            and kwargs.get("ondelete") == ondelete
        ):
            return True
    return False


def test_upgrade_creates_pool_and_member_tables() -> None:
    migration = _load_migration_0132()
    migration.op = MagicMock()
    migration.upgrade()

    created = [c.args[0] for c in migration.op.create_table.call_args_list]
    assert "ml_backend_service_pools" in created
    assert "ml_backend_pool_members" in created
    migration.op.create_index.assert_any_call(
        "ix_ml_backend_pool_members_pool_id", "ml_backend_pool_members", ["pool_id"]
    )


def test_upgrade_singleton_backfill_uses_project_enablement_for_pool_enabled() -> None:
    """Pool enabled flag follows whether the registry is enabled in any project (off-mode parity)."""
    migration = _load_migration_0132()
    migration.op = MagicMock()
    migration.upgrade()

    sqls = _collected_sql(migration.op)
    pool_inserts = [s for s in sqls if "INSERT INTO ml_backend_service_pools" in s]
    assert pool_inserts, "singleton pool backfill INSERT missing"
    assert "project_ml_backend" in pool_inserts[0]
    assert "enabled = true" in pool_inserts[0] or "enabled=true" in pool_inserts[0]
    member_inserts = [s for s in sqls if "INSERT INTO ml_backend_pool_members" in s]
    assert member_inserts
    assert "'active'" in member_inserts[0]
    assert "1" in member_inserts[0]


def test_upgrade_builds_machine_readable_registry_to_pool_map() -> None:
    """The remapping must be driven by an explicit _pool_map temp table, not name/URL recompute."""
    migration = _load_migration_0132()
    migration.op = MagicMock()
    migration.upgrade()

    sqls = _collected_sql(migration.op)
    map_sqls = [s for s in sqls if "_pool_map" in s and "CREATE TEMP TABLE" in s]
    assert map_sqls, "_pool_map temp table must drive all remapping"
    assert "legacy_instance_id" in map_sqls[0]


def test_upgrade_renames_project_binding_table_and_column() -> None:
    migration = _load_migration_0132()
    migration.op = MagicMock()
    migration.upgrade()

    assert _has_alter(migration.op, "project_ml_backend", "registry_id", "pool_id")
    migration.op.rename_table.assert_any_call(
        "project_ml_backend", "project_ml_backend_pool"
    )
    assert _has_create_fk(
        migration.op,
        "project_ml_backend_pool_pool_id_fkey",
        "project_ml_backend",
        "ml_backend_service_pools",
        ["pool_id"],
        "CASCADE",
    )


def test_upgrade_renames_project_main_column_without_dropping_nonexistent_fk() -> None:
    """projects.ml_backend_id had NO DB FK historically; upgrade must not hard-drop it.

    The migration uses conditional ``ALTER TABLE ... DROP CONSTRAINT IF EXISTS`` so a
    historically-absent FK does not abort the upgrade (ADR-0050 §5.3 / inventory D.4).
    """
    migration = _load_migration_0132()
    migration.op = MagicMock()
    migration.upgrade()

    assert _has_alter(migration.op, "projects", "ml_backend_id", "ml_backend_pool_id")
    sqls = _collected_sql(migration.op)
    assert any(
        "ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_ml_backend_id_fkey"
        in s
        for s in sqls
    ), "projects FK drop must be conditional (FK may not exist historically)"
    assert _has_create_fk(
        migration.op,
        "projects_ml_backend_pool_id_fkey",
        "projects",
        "ml_backend_service_pools",
        ["ml_backend_pool_id"],
        "SET NULL",
    )


def test_upgrade_adds_pool_id_to_partitioned_predictions_and_failed_predictions() -> (
    None
):
    migration = _load_migration_0132()
    migration.op = MagicMock()
    migration.upgrade()

    add_targets = _add_column_targets(migration.op)
    assert "predictions" in add_targets
    assert "failed_predictions" in add_targets
    assert _has_create_fk(
        migration.op,
        "predictions_ml_backend_pool_id_fkey",
        "predictions",
        "ml_backend_service_pools",
        ["ml_backend_pool_id"],
        "SET NULL",
    )
    assert _has_create_fk(
        migration.op,
        "failed_predictions_ml_backend_pool_id_fkey",
        "failed_predictions",
        "ml_backend_service_pools",
        ["ml_backend_pool_id"],
        "SET NULL",
    )


def test_upgrade_remaps_all_seven_jsonb_classes() -> None:
    """All 7 JSONB reference classes from inventory §A.5 must be remapped."""
    migration = _load_migration_0132()
    migration.op = MagicMock()
    migration.upgrade()

    sqls = _collected_sql(migration.op)
    blob = "\n".join(sqls)
    assert "preannotate_pipeline" in blob and "ml_backend_pool_id" in blob
    assert "projects" in blob and "default_variants" in blob
    assert "project_ml_backend" in blob
    assert "params_by_backend" in blob
    assert "model_by_backend" in blob
    assert "interactive_backend_by_project" in blob
    assert "secondary_by_model" in blob
    assert "async_jobs" in blob and "ml_backend_pool_id" in blob


def test_upgrade_runs_orphan_and_cardinality_guards() -> None:
    """upgrade must end with a DO $$ guard: no multi-member pools, no orphan bindings."""
    migration = _load_migration_0132()
    migration.op = MagicMock()
    migration.upgrade()

    sqls = _collected_sql(migration.op)
    guard = [s for s in sqls if "RAISE EXCEPTION" in s and "backfill" in s]
    assert guard, "upgrade must run orphan/cardinality invariant guards"
    guard_blob = "\n".join(guard)
    assert "multi_member_pools" in guard_blob or "HAVING count(*) > 1" in guard_blob
    assert "unmapped_bindings" in guard_blob
    assert "unmapped_projects" in guard_blob


def test_downgrade_fail_closed_when_any_pool_has_multiple_members() -> None:
    """ADR-0050 D18 / §6.3: downgrade must RAISE when any pool has >1 member."""
    migration = _load_migration_0132()
    migration.op = MagicMock()
    migration.downgrade()

    sqls = _collected_sql(migration.op)
    guard = [s for s in sqls if "RAISE EXCEPTION" in s and "forward-only" in s]
    assert guard, "downgrade must fail-closed on multi-member pools"
    assert "multiple members" in guard[0] or "multi_member" in guard[0]


def test_downgrade_reverses_column_and_table_renames_when_singleton() -> None:
    """When the forward-only guard passes (mocked), downgrade reverses the rename dance."""
    migration = _load_migration_0132()
    migration.op = MagicMock()
    migration.downgrade()

    assert _has_alter(migration.op, "project_ml_backend_pool", "pool_id", "registry_id")
    migration.op.rename_table.assert_any_call(
        "project_ml_backend_pool", "project_ml_backend"
    )
    assert _has_alter(migration.op, "projects", "ml_backend_pool_id", "ml_backend_id")


def test_downgrade_does_not_recreate_historically_absent_projects_fk() -> None:
    """downgrade must NOT recreate projects_ml_backend_id_fkey (it never existed)."""
    migration = _load_migration_0132()
    migration.op = MagicMock()
    migration.downgrade()

    fk_names = _create_fk_names(migration.op)
    assert "projects_ml_backend_id_fkey" not in fk_names, (
        "downgrade must not recreate the historically-absent projects.ml_backend_id FK"
    )
    assert "project_ml_backend_registry_id_fkey" in fk_names


def test_downgrade_reverses_jsonb_remapping_via_legacy_instance_map() -> None:
    """downgrade builds _pool_map_down (pool→legacy registry) to reverse JSONB keys."""
    migration = _load_migration_0132()
    migration.op = MagicMock()
    migration.downgrade()

    sqls = _collected_sql(migration.op)
    blob = "\n".join(sqls)
    assert "_pool_map_down" in blob
    assert "legacy_instance_id" in blob
    for key in (
        "preannotate_pipeline",
        "default_variants",
        "params_by_backend",
        "model_by_backend",
        "interactive_backend_by_project",
        "secondary_by_model",
    ):
        assert key in blob
