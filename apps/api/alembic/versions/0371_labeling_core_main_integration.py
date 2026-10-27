"""Publish immutable-original and execution-snapshot facts through core reads."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0371"
down_revision = "bo0370"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_VIEWS = ("task_rows", "failed_prediction_rows")
_FUNCTIONS = {
    "lock_task_for_core_read(uuid,text,boolean,uuid)": "0352_labeling_core_read.sql",
    "lock_task_rows_for_core_read(uuid[],uuid,uuid)": "0352_labeling_core_read.sql",
    "lock_active_learning_feedback_task_rows(uuid,uuid[])": "0361_labeling_compatibility_reads.sql",
}


def _statements(filename: str) -> list[str]:
    return [
        statement.strip()
        for statement in (_SQL_DIR / filename)
        .read_text(encoding="utf-8")
        .split("-- aap-statement-break")
        if statement.strip()
    ]


def _execute(statement: str) -> None:
    op.execute(sa.text(statement).execution_options(no_parameters=True))


def upgrade() -> None:
    for statement in _statements("0371_labeling_core_main_integration.sql"):
        _execute(statement)


def _acl_statements(kind: str, name: str) -> tuple[str, list[str]]:
    """Keep existing owners and capabilities when removing appended columns."""
    bind = op.get_bind()
    catalog, owner_column, acl_column, resolver = (
        ("pg_class", "relowner", "relacl", "to_regclass")
        if kind == "TABLE"
        else ("pg_proc", "proowner", "proacl", "to_regprocedure")
    )
    parameters = {"name": name, "kind": kind}
    owner = bind.execute(
        sa.text(
            f"SELECT pg_catalog.format('ALTER %s %s OWNER TO %I', "
            f"CAST(:kind AS text), CAST(:name AS text), pg_catalog.pg_get_userbyid(object.{owner_column})) "
            f"FROM pg_catalog.{catalog} AS object "
            f"WHERE object.oid = pg_catalog.{resolver}(:name)"
        ),
        parameters,
    ).scalar_one()
    grants = list(
        bind.execute(
            sa.text(
                "SELECT pg_catalog.format('GRANT %s ON %s %s TO %s%s', "
                "privilege.privilege_type, CAST(:kind AS text), CAST(:name AS text), "
                "CASE WHEN privilege.grantee = 0 THEN 'PUBLIC' "
                "ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(privilege.grantee)) END, "
                "CASE WHEN privilege.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END) "
                f"FROM pg_catalog.{catalog} AS object "
                f"CROSS JOIN LATERAL pg_catalog.aclexplode(object.{acl_column}) AS privilege "
                f"WHERE object.oid = pg_catalog.{resolver}(:name) "
                f"AND privilege.grantee <> object.{owner_column} "
                "ORDER BY privilege.grantor, privilege.grantee, privilege.privilege_type"
            ),
            parameters,
        ).scalars()
    )
    if kind == "TABLE":
        grants.extend(
            bind.execute(
                sa.text(
                    "SELECT pg_catalog.format('GRANT %s (%I) ON TABLE %s TO %s%s', "
                    "privilege.privilege_type, attribute.attname, CAST(:name AS text), "
                    "CASE WHEN privilege.grantee = 0 THEN 'PUBLIC' "
                    "ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(privilege.grantee)) END, "
                    "CASE WHEN privilege.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END) "
                    "FROM pg_catalog.pg_attribute AS attribute "
                    "CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS privilege "
                    "WHERE attribute.attrelid = pg_catalog.to_regclass(:name) "
                    "AND attribute.attnum > 0 AND NOT attribute.attisdropped "
                    "AND attribute.attname NOT IN "
                    "('thumbnail_output_id', 'original_id', 'execution_snapshot_id') "
                    "ORDER BY privilege.grantor, privilege.grantee, attribute.attnum"
                ),
                {"name": name},
            ).scalars()
        )
    return owner, grants


def _restore_acl(kind: str, name: str, saved: tuple[str, list[str]]) -> None:
    # Replacement objects must not inherit migration-role default grants.
    catalog, owner_column, acl_column, resolver = (
        ("pg_class", "relowner", "relacl", "to_regclass")
        if kind == "TABLE"
        else ("pg_proc", "proowner", "proacl", "to_regprocedure")
    )
    bind = op.get_bind()
    revokes = (
        bind.execute(
            sa.text(
                "SELECT DISTINCT pg_catalog.format('REVOKE ALL ON %s %s FROM %s CASCADE', "
                "CAST(:kind AS text), CAST(:name AS text), CASE WHEN privilege.grantee = 0 THEN 'PUBLIC' "
                "ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(privilege.grantee)) END) "
                f"FROM pg_catalog.{catalog} AS object "
                f"CROSS JOIN LATERAL pg_catalog.aclexplode(object.{acl_column}) AS privilege "
                f"WHERE object.oid = pg_catalog.{resolver}(:name) "
                f"AND privilege.grantee <> object.{owner_column}"
            ),
            {"kind": kind, "name": name},
        )
        .scalars()
        .all()
    )
    for statement in revokes:
        bind.exec_driver_sql(statement)
    owner, grants = saved
    bind.exec_driver_sql(owner)
    for statement in grants:
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    objects = [
        *(("TABLE", f"aap_labeling_v1.{name}") for name in _VIEWS),
        *(("FUNCTION", f"aap_labeling_v1.{name}") for name in _FUNCTIONS),
    ]
    acls = {key: _acl_statements(*key) for key in objects}
    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION aap_labeling_v1.{signature} RESTRICT")
    for name in _VIEWS:
        _execute(f"DROP VIEW aap_labeling_v1.{name} RESTRICT")
    original = _statements("0352_labeling_core_read.sql")
    for name in _VIEWS:
        _execute(
            next(
                s
                for s in original
                if s.startswith(f"CREATE VIEW aap_labeling_v1.{name}\n")
            )
        )
    for signature, filename in _FUNCTIONS.items():
        name = signature.split("(", 1)[0]
        _execute(
            next(
                s
                for s in _statements(filename)
                if s.startswith(f"CREATE FUNCTION aap_labeling_v1.{name}(")
            )
        )
    for key in objects:
        _restore_acl(*key, acls[key])
