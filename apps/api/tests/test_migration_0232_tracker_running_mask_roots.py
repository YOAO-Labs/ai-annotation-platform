"""Static checks for tracker-running Mask root migration and rollback fencing."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "apps/api/alembic/sql"
MIGRATION = ROOT / "apps/api/alembic/versions/0232_tracker_running_mask_roots.py"


def _migration():
    spec = importlib.util.spec_from_file_location(
        "tracker_running_mask_roots_migration", MIGRATION
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _Bind:
    def __init__(self, count):
        self.count = count
        self.queries = []

    def execute(self, statement):
        self.queries.append(str(statement))
        return _Result(self.count)


def test_migration_adds_root_registration_release_and_liveness():
    module = _migration()
    artifact = SQL / "0232_tracker_running_mask_roots.sql"
    statements = module._statements(module._ARTIFACT)
    sql = artifact.read_text(encoding="utf-8")

    assert artifact.is_file()
    assert any(
        "register_tracker_running_mask_root(jsonb)" in item for item in statements
    )
    assert any(
        "_mask_gc_release_tracker_running_outputs()" in item for item in statements
    )
    assert sql.count("REVOKE ALL ON FUNCTION") == 3
    assert "job.status = 'running'" in sql
    assert "v_job_task_id IS DISTINCT FROM v_task_id" in sql
    assert "v_cancel_requested_at IS NOT NULL" in sql
    assert "FOR SHARE NOWAIT" in sql
    assert "WHEN lock_not_available" in sql
    assert "_mask_gc_reply(NULL, 'storage_unavailable')" in sql
    assert "_mask_gc_permit_is_current(v_object_key, v_epoch)" in sql
    assert "root_kind = 'video_tracker_running_outputs'" in sql
    assert "AFTER UPDATE OF status OR DELETE ON public.video_tracker_jobs" in sql


def test_downgrade_refuses_to_remove_existing_running_roots(monkeypatch):
    module = _migration()
    bind = _Bind(1)
    emitted = []
    monkeypatch.setattr(module.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        module.op, "execute", lambda statement: emitted.append(statement)
    )

    with pytest.raises(RuntimeError, match="roots remain"):
        module.downgrade()

    assert bind.queries == [module._DOWNGRADE_GUARD]
    assert emitted == []


def test_downgrade_restores_the_0231_liveness_contract():
    module = _migration()
    restored = module._previous_mask_gc_root_liveness_statements()

    assert len(restored) == 2
    assert "batch_result_payloads" in restored[0]
    assert "video_tracker_running_outputs" not in restored[0]
    assert restored[1].startswith("REVOKE ALL ON FUNCTION")


def test_empty_downgrade_drops_trigger_and_restores_liveness(monkeypatch):
    module = _migration()
    bind = _Bind(0)
    emitted = []
    monkeypatch.setattr(module.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        module.op, "execute", lambda statement: emitted.append(str(statement))
    )

    module.downgrade()

    statements = [
        statement
        for statement in emitted
        if statement.startswith(("DROP", "CREATE", "REVOKE"))
    ]
    assert statements[0].startswith("DROP TRIGGER IF EXISTS")
    assert any(
        statement.startswith("CREATE OR REPLACE FUNCTION") for statement in statements
    )
    assert any(
        statement.startswith("REVOKE ALL ON FUNCTION") for statement in statements
    )
    assert statements[-2:] == [
        "DROP FUNCTION IF EXISTS "
        "aap_labeling_v1.register_tracker_running_mask_root(jsonb) RESTRICT",
        "DROP FUNCTION IF EXISTS "
        "aap_labeling_v1._mask_gc_release_tracker_running_outputs() RESTRICT",
    ]
