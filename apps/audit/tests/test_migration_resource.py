from __future__ import annotations

import ast
import json
from pathlib import Path

from aap_audit.maintenance_contract import audit_maintenance_schema
from aap_audit.read_relation_contract import audit_read_relations_schema
from aap_audit.daily_projection_contract import audit_daily_projection_schema


ROOT = Path(__file__).resolve().parents[3]


def test_alembic_resource_matches_the_installed_owner_package_and_has_one_parent():
    package = ROOT / "apps/audit/src/aap_audit"
    migration_sql = ROOT / "apps/api/alembic/sql/0315_audit_append.sql"
    package_sql = package / "sql/0315_audit_append.sql"
    migration_schema = ROOT / "apps/api/alembic/sql/0315_audit_append.schema.json"
    package_schema = package / "append.schema.json"
    assert migration_sql.read_bytes() == package_sql.read_bytes()
    assert migration_schema.read_bytes() == package_schema.read_bytes()

    migration = ast.parse(
        (ROOT / "apps/api/alembic/versions/0315_audit_append.py").read_text()
    )
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in migration.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}
    }
    assert assignments == {"revision": "0315", "down_revision": "0314"}


def test_query_migration_resources_follow_notifications_read_state():
    package = ROOT / "apps/audit/src/aap_audit"
    migration_sql = ROOT / "apps/api/alembic/sql/0317_audit_query.sql"
    package_sql = package / "sql/0317_audit_query.sql"
    migration_schema = ROOT / "apps/api/alembic/sql/0317_audit_query.schema.json"
    package_schema = package / "query.schema.json"
    assert migration_sql.read_bytes() == package_sql.read_bytes()
    assert migration_schema.read_bytes() == package_schema.read_bytes()

    migration = ast.parse(
        (ROOT / "apps/api/alembic/versions/0317_audit_query.py").read_text()
    )
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in migration.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}
    }
    assert assignments == {"revision": "0317", "down_revision": "0316"}


def test_read_relation_sql_and_schema_resources_match_the_owner_package():
    package = ROOT / "apps/audit/src/aap_audit"
    migration_sql = ROOT / "apps/api/alembic/sql/0320_audit_read_relations.sql"
    package_sql = package / "sql/0320_audit_read_relations.sql"
    migration_schema = (
        ROOT / "apps/api/alembic/sql/0320_audit_read_relations.schema.json"
    )
    package_schema = package / "read_relations.schema.json"

    assert migration_sql.read_bytes() == package_sql.read_bytes()
    assert json.loads(package_schema.read_text()) == audit_read_relations_schema()
    assert json.loads(migration_schema.read_text()) == audit_read_relations_schema()

    migration = ast.parse(
        (ROOT / "apps/api/alembic/versions/0320_audit_read_relations.py").read_text()
    )
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in migration.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}
    }
    assert assignments == {"revision": "0320", "down_revision": "0319"}


def test_daily_projection_sql_and_schema_resources_match_the_owner_package():
    package = ROOT / "apps/audit/src/aap_audit"
    migration_sql = ROOT / "apps/api/alembic/sql/0322_audit_daily_projection.sql"
    package_sql = package / "sql/0322_audit_daily_projection.sql"
    migration_schema = (
        ROOT / "apps/api/alembic/sql/0322_audit_daily_projection.schema.json"
    )
    package_schema = package / "daily_projection.schema.json"

    assert migration_sql.read_bytes() == package_sql.read_bytes()
    assert json.loads(package_schema.read_text()) == audit_daily_projection_schema()
    assert json.loads(migration_schema.read_text()) == audit_daily_projection_schema()

    migration = ast.parse(
        (ROOT / "apps/api/alembic/versions/0322_audit_daily_projection.py").read_text()
    )
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in migration.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}
    }
    assert assignments == {"revision": "0322", "down_revision": "0321"}


def test_fixture_cleanup_migration_resources_match_the_owner_package():
    package = ROOT / "apps/audit/src/aap_audit"
    migration_sql = ROOT / "apps/api/alembic/sql/0366_audit_fixture_cleanup.sql"
    package_sql = package / "sql/0366_audit_fixture_cleanup.sql"
    migration_schema = (
        ROOT / "apps/api/alembic/sql/0366_audit_fixture_cleanup.schema.json"
    )
    package_schema = package / "maintenance.schema.json"

    assert migration_sql.read_bytes() == package_sql.read_bytes()
    assert json.loads(package_schema.read_text()) == audit_maintenance_schema()
    assert json.loads(migration_schema.read_text()) == audit_maintenance_schema()
    sql = package_sql.read_text()
    assert "CREATE OR REPLACE FUNCTION aap_audit_v1.maintenance_command" in sql
    assert not any(token in sql for token in ("DROP FUNCTION", "GRANT ", "REVOKE "))

    migration_source = (
        ROOT / "apps/api/alembic/versions/0366_audit_fixture_cleanup.py"
    ).read_text()
    assert (
        "CREATE OR REPLACE FUNCTION aap_audit_v1.maintenance_command"
        in migration_source
    )
    assert "DROP FUNCTION" not in migration_source
    migration = ast.parse(migration_source)
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in migration.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}
    }
    assert assignments == {"revision": "0366", "down_revision": "0365"}
