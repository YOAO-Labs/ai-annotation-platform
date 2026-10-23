from __future__ import annotations

import ast
from pathlib import Path


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
