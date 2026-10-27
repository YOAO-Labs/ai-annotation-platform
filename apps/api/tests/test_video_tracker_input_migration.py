"""Migration guards and reversible DDL for retained tracker video inputs."""

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "apps/api/alembic/sql"
MIGRATION = ROOT / "apps/api/alembic/versions/0228_video_tracker_inputs.py"


def _migration():
    spec = importlib.util.spec_from_file_location(
        "video_tracker_input_migration", MIGRATION
    )
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
        "drop_index",
        lambda *args, **kwargs: emitted.append(("drop_index", args, kwargs)),
    )
    monkeypatch.setattr(
        module.op,
        "drop_table",
        lambda *args, **kwargs: emitted.append(("drop_table", args, kwargs)),
    )
    monkeypatch.setattr(
        module.op,
        "drop_constraint",
        lambda *args, **kwargs: emitted.append(("drop_constraint", args, kwargs)),
    )
    monkeypatch.setattr(
        module.op,
        "drop_column",
        lambda *args, **kwargs: emitted.append(("drop_column", args, kwargs)),
    )
    return emitted


def _created_function_signature(statement):
    match = re.search(
        r"(?is)\bCREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+([\w.]+)\s*"
        r"\((.*?)\)\s*RETURNS\b",
        statement,
    )
    if match is None:
        return None
    argument_types = [
        " ".join(argument.split()[1:])
        for argument in match.group(2).split(",")
        if argument.strip()
    ]
    return f"{match.group(1)}({','.join(argument_types)})"


def test_revision_installs_only_tracker_input_artifact():
    module = _migration()

    assert module._UPGRADE_ARTIFACTS == ("0228_video_tracker_inputs.sql",)
    assert set(module._REPLACED_FUNCTIONS) == {
        "aap_assets_v1.claim_accepted_video_input(uuid,uuid,integer)",
        "aap_assets_v1.sweep_accepted_video_inputs(integer)",
    }


def test_upgrade_refuses_live_or_ambiguous_legacy_tracker_work(monkeypatch):
    module = _migration()
    bind = _Bind({module._LEGACY_TRACKER_RESPONSIBILITY_COUNT: 2})
    emitted = _record_operations(monkeypatch, module)
    monkeypatch.setattr(module.op, "get_bind", lambda: bind)

    with pytest.raises(RuntimeError, match="legacy tracker work"):
        module.upgrade()

    assert bind.queries == [module._LEGACY_TRACKER_RESPONSIBILITY_COUNT]
    assert emitted == []


def test_upgrade_guard_distinguishes_empty_candidates_and_reviewable_legacy_work():
    module = _migration()
    query = " ".join(module._LEGACY_TRACKER_RESPONSIBILITY_COUNT.split())

    assert "job.status IN ('queued', 'running')" in query
    assert "job.status IN ('pending_review', 'partially_reviewed')" in query
    assert "job.job_kind = 'tracking'" in query
    assert "job.status = 'cancelled'" in query
    assert "interval '24 hours'" in query
    assert "job.staged_result = '{}'::jsonb" in query
    assert "job.staged_result <> '[]'::jsonb" in query
    assert "jsonb_array_length( job.staged_result -> 'results' ) > 0" in query
    assert "status NOT IN" in query


def test_upgrade_runs_artifact_only_after_legacy_work_drains(monkeypatch):
    module = _migration()
    bind = _Bind({module._LEGACY_TRACKER_RESPONSIBILITY_COUNT: 0})
    emitted = _record_operations(monkeypatch, module)
    monkeypatch.setattr(module.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        module, "_statements", lambda _name: ["CREATE TABLE test_install (id int)"]
    )

    module.upgrade()

    assert len(bind.queries) == 1
    assert str(emitted[0]) == "CREATE TABLE test_install (id int)"


def test_downgrade_refuses_each_kind_of_retained_responsibility(monkeypatch):
    module = _migration()

    for retained_name, query in module._DOWNGRADE_GUARDS.items():
        bind = _Bind({query: 1})
        emitted = _record_operations(monkeypatch, module)
        monkeypatch.setattr(module.op, "get_bind", lambda bind=bind: bind)

        with pytest.raises(RuntimeError, match=retained_name):
            module.downgrade()

        assert query in bind.queries
        assert emitted == []


def test_downgrade_restores_frozen_bodies_then_drops_only_new_ddl(monkeypatch):
    module = _migration()
    bind = _Bind({})
    emitted = _record_operations(monkeypatch, module)
    monkeypatch.setattr(module.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        module,
        "_new_function_signatures",
        lambda: [
            "aap_assets_v1._video_tracker_input_current_source("
            "public.asset_accepted_video_inputs,boolean,boolean)",
            "aap_assets_v1._video_tracker_input_record("
            "uuid,jsonb,timestamptz,text,public.asset_accepted_video_inputs)",
            "aap_assets_v1.capture_video_tracker_input(uuid,uuid,integer)",
        ],
    )

    module.downgrade()

    statements = [str(item) for item in emitted if not isinstance(item, tuple)]
    old_definitions = [
        item
        for item in statements
        if re.search(r"(?is)\bCREATE OR REPLACE FUNCTION aap_assets_v1\.", item)
    ]
    function_drops = [item for item in statements if item.startswith("DROP FUNCTION")]
    assert len(old_definitions) == len(module._REPLACED_FUNCTIONS)
    assert len(function_drops) == 3
    assert all("RESTRICT" in item for item in function_drops)
    assert all(
        not any(_function_name in item for _function_name in module._REPLACED_FUNCTIONS)
        for item in function_drops
    )
    assert max(
        i
        for i, item in enumerate(statements)
        if re.search(r"(?is)\bCREATE OR REPLACE FUNCTION\b", item)
    ) < min(i for i, item in enumerate(statements) if item.startswith("DROP FUNCTION"))
    assert [item[0] for item in emitted if isinstance(item, tuple)] == [
        "drop_index",
        "drop_index",
        "drop_table",
        "drop_table",
        "drop_index",
        "drop_constraint",
        "drop_constraint",
        "drop_column",
        "drop_column",
    ]
    assert not any(
        re.match(r"(?is)^\s*(DELETE|TRUNCATE)\b", item) for item in statements
    )


def test_replaced_functions_restore_from_the_frozen_0225_and_0226_artifacts():
    module = _migration()
    restores = module._restore_replaced_functions()

    assert len(restores) == 2 * len(module._REPLACED_FUNCTIONS)
    for index, signature in enumerate(module._REPLACED_FUNCTIONS):
        definition, revoke = restores[index * 2 : index * 2 + 2]
        assert re.search(r"(?is)\bCREATE OR REPLACE FUNCTION\b", definition)
        assert module._function_name(signature) in definition.lower()
        assert module._canonical_signature(module._function_signature(revoke)) == (
            module._canonical_signature(signature)
        )
    assert (
        module._REPLACED_FUNCTIONS[
            "aap_assets_v1.claim_accepted_video_input(uuid,uuid,integer)"
        ]
        == "0226_video_input_claim_authority.sql"
    )
    assert all(
        filename
        in {"0225_video_inputs_execution.sql", "0225_video_inputs_retention.sql"}
        for signature, filename in module._REPLACED_FUNCTIONS.items()
        if not signature.startswith("aap_assets_v1.claim_accepted_video_input(")
    )


def test_new_sql_artifact_has_exact_revokes_for_every_new_function():
    artifact = SQL / "0228_video_tracker_inputs.sql"
    if not artifact.exists():
        pytest.skip("native 0228 SQL artifact is integrated by the parent worktree")

    module = _migration()
    statements = module._statements(artifact.name)
    replaced_names = {
        module._function_name(signature) for signature in module._REPLACED_FUNCTIONS
    }
    replaced_signatures = {
        module._canonical_signature(signature)
        for statement in statements
        if re.search(r"(?is)\bCREATE OR REPLACE FUNCTION\b", statement)
        and (signature := _created_function_signature(statement))
        and module._function_name(signature) in replaced_names
    }
    created_signatures = {
        module._canonical_signature(signature)
        for statement in statements
        if (signature := _created_function_signature(statement))
        and module._function_name(signature)
        not in {module._function_name(s) for s in module._REPLACED_FUNCTIONS}
    }
    revoked_signatures = {
        module._canonical_signature(s) for s in module._new_function_signatures()
    }

    assert created_signatures == revoked_signatures
    assert replaced_signatures == {
        module._canonical_signature(signature)
        for signature in module._REPLACED_FUNCTIONS
    }
    assert not re.search(
        r"(?is)\bUPDATE\s+public\.video_tracker_jobs\b", artifact.read_text()
    )
    assert module._new_function_signatures()
