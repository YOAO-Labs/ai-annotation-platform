from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_module_data_boundary import (
    READ_RELATION_SPECS,
    _assert_read_relation_structure_matches,
    _normalize_read_relation_observations,
    _installed_function_body_sha256,
)
import verify_module_data_boundary as verifier


def test_installed_body_uses_named_function_in_multi_function_resource(
    tmp_path, monkeypatch
):
    sql_dir = tmp_path / "apps/api/alembic/sql"
    sql_dir.mkdir(parents=True)
    (sql_dir / "queries.sql").write_text(
        "CREATE FUNCTION aap_test.first() RETURNS int LANGUAGE sql "
        "AS $function$SELECT 1$function$;\n"
        "CREATE FUNCTION aap_test.second() RETURNS int LANGUAGE sql "
        "AS $function$SELECT 2$function$;\n"
    )
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    monkeypatch.setattr(verifier, "_psql", lambda *_args, **_kwargs: b"SELECT 2".hex())
    arguments = dict(
        database="owned_test",
        owner_password="fixture-only",
        signature="aap_test.second()",
        sql_name="queries.sql",
    )
    assert _installed_function_body_sha256("id", "name", "identity", **arguments) == (
        hashlib.sha256(b"SELECT 2").hexdigest()
    )
    monkeypatch.setattr(verifier, "_psql", lambda *_args, **_kwargs: b"SELECT 1".hex())
    with pytest.raises(RuntimeError, match="differs from frozen SQL"):
        _installed_function_body_sha256("id", "name", "identity", **arguments)


def test_view_evidence_hashes_catalog_deparser_and_tracks_owner_transfer():
    deparser_definitions = {
        relation_name: f"SELECT source.{index} FROM public.source_{index}".encode()
        for index, relation_name in enumerate(READ_RELATION_SPECS, start=1)
    }

    def observations(*, owners, changed_type: str | None = None):
        result = []
        for index, (relation_name, spec) in enumerate(
            READ_RELATION_SPECS.items(), start=1
        ):
            columns = [
                {"name": name, "type": type_name} for name, type_name in spec["columns"]
            ]
            if changed_type is not None and index == 1:
                columns[0]["type"] = changed_type
            result.append(
                {
                    "relation": relation_name,
                    "deparser_hex": deparser_definitions[relation_name].hex(),
                    "relkind": "v",
                    "owner": owners[relation_name],
                    "reloptions": ["security_barrier=true"],
                    "columns": columns,
                }
            )
        return json.dumps(result)

    migration_owners = {name: "owner" for name in READ_RELATION_SPECS}
    code_owners = {name: spec["owner"] for name, spec in READ_RELATION_SPECS.items()}
    before = _normalize_read_relation_observations(
        observations(owners=migration_owners), expected_owner="owner"
    )
    after = _normalize_read_relation_observations(
        observations(owners=code_owners), expected_owner=code_owners
    )

    _assert_read_relation_structure_matches(
        before, after, phase="module owner transfer"
    )
    for relation_name, evidence in before.items():
        assert (
            evidence["pg_get_viewdef_sha256"]
            == hashlib.sha256(deparser_definitions[relation_name]).hexdigest()
        )
        assert evidence["definition_source"] == "pg_catalog.pg_get_viewdef(oid, true)"
        assert evidence["metadata"]["owner"] == "owner"
        assert "deparser_hex" not in evidence

    with pytest.raises(RuntimeError, match="fixed contract"):
        _normalize_read_relation_observations(
            observations(owners=code_owners, changed_type="text"),
            expected_owner=code_owners,
        )
