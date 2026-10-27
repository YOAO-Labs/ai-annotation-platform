"""Migration contract for durable native Raster Mask GC authority."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "apps/api/alembic/sql"
MIGRATION = ROOT / "apps/api/alembic/versions/0229_mask_gc.py"


def _migration():
    spec = importlib.util.spec_from_file_location("mask_gc_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, count):
        self.count = count

    def scalar_one(self):
        return self.count


class _Bind:
    def __init__(self, counts):
        self.counts = counts
        self.queries = []

    def execute(self, statement):
        query = str(statement)
        self.queries.append(query)
        return _Result(self.counts.get(query, 0))


def _record_operations(monkeypatch, module):
    emitted = []
    monkeypatch.setattr(
        module.op, "execute", lambda statement: emitted.append(statement)
    )
    monkeypatch.setattr(
        module.op,
        "drop_table",
        lambda *args, **kwargs: emitted.append(("drop_table", args, kwargs)),
    )
    return emitted


def test_revision_uses_one_new_native_artifact():
    module = _migration()

    assert module._UPGRADE_ARTIFACTS == ("0229_mask_gc.sql",)
    assert (SQL / module._UPGRADE_ARTIFACTS[0]).is_file()


def test_current_native_schema_matches_the_portable_gc_contract():
    from aap_labeling_mask_gc.contract import contract_document

    assert (
        json.loads((SQL / "0234_mask_gc.schema.json").read_text())
        == contract_document()
    )


def test_every_native_function_has_an_explicit_public_revoke():
    module = _migration()
    statements = module._statements("0229_mask_gc.sql")
    signatures = module._new_function_signatures()

    assert len(signatures) >= 20
    assert all(
        re.search(r"(?is)^REVOKE ALL ON FUNCTION .* FROM PUBLIC;$", statement)
        for statement in statements
        if statement.startswith("REVOKE ALL ON FUNCTION")
    )
    assert {
        "aap_labeling_v1.read_mask_write_epochs(jsonb)",
        "aap_labeling_v1.begin_mask_content_write(jsonb)",
        "aap_labeling_v1.lock_mask_write_epochs(jsonb)",
        "aap_labeling_v1.claim_mask_gc_sweep(jsonb)",
        "aap_labeling_v1.stage_mask_gc_page(jsonb)",
        "aap_labeling_v1.claim_mask_gc_deletes(jsonb)",
        "aap_labeling_v1.finish_mask_gc_delete(jsonb)",
        "aap_labeling_v1.read_mask_gc_progress(jsonb)",
    } <= set(signatures)


def test_upgrade_runs_artifact_statements_in_order(monkeypatch):
    module = _migration()
    emitted = _record_operations(monkeypatch, module)
    monkeypatch.setattr(
        module, "_statements", lambda _name: ["CREATE TABLE marker (id int)"]
    )

    module.upgrade()

    assert len(emitted) == 1
    assert str(emitted[0]) == "CREATE TABLE marker (id int)"


def test_downgrade_preserves_any_durable_gc_responsibility(monkeypatch):
    module = _migration()

    for guard_name, query in module._DOWNGRADE_GUARDS.items():
        bind = _Bind({query: 1})
        emitted = _record_operations(monkeypatch, module)
        monkeypatch.setattr(module.op, "get_bind", lambda bind=bind: bind)

        with pytest.raises(RuntimeError, match=guard_name):
            module.downgrade()

        assert query in bind.queries
        assert emitted == []


def test_empty_downgrade_removes_triggers_functions_then_native_tables(monkeypatch):
    module = _migration()
    bind = _Bind({})
    emitted = _record_operations(monkeypatch, module)
    monkeypatch.setattr(module.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        module, "_new_function_signatures", lambda: ["aap_labeling_v1._helper()"]
    )
    monkeypatch.setattr(
        module, "_DOWNGRADE_GUARDS", {"epochs": "SELECT count(*) FROM epochs"}
    )

    module.downgrade()

    statements = [str(item) for item in emitted if not isinstance(item, tuple)]
    assert len(statements) == len(module._ROOT_TRIGGERS) + 1
    assert all(
        statement.startswith("DROP TRIGGER IF EXISTS") for statement in statements[:-1]
    )
    assert (
        statements[-1] == "DROP FUNCTION IF EXISTS aap_labeling_v1._helper() RESTRICT"
    )
    dropped_tables = [
        item[1][0]
        for item in emitted
        if isinstance(item, tuple) and item[0] == "drop_table"
    ]
    assert dropped_tables == [
        "mask_gc_roots",
        "mask_gc_write_permit_secrets",
        "mask_gc_sweeps",
        "mask_gc_key_epochs",
        "mask_gc_intents",
    ]
