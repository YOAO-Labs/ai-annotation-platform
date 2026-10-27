"""Carry the merged guide-assets revision through Project snapshots."""

from pathlib import Path

from alembic import op

revision = "bo0377"
down_revision = "bo0376"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_SQL = _SQL_DIR / "0377_workspace_project_snapshots.sql"


def _execute(statement: str) -> None:
    # Frozen view bodies contain clock-format colons; send them verbatim.
    op.get_bind().exec_driver_sql(statement)


def _has_sql(statement: str) -> bool:
    return any(
        line.strip() and not line.lstrip().startswith("--")
        for line in statement.splitlines()
    )


def _definition(
    filename: str,
    marker: str,
    *,
    rename: tuple[str, str] | None = None,
) -> str:
    source = (_SQL_DIR / filename).read_text(encoding="utf-8")
    start = source.index(marker)
    end = source.index("$function$;", start) + len("$function$;")
    definition = source[start:end]
    if definition.startswith("CREATE FUNCTION"):
        definition = definition.replace(
            "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
        )
    if rename is not None:
        old, new = rename
        if definition.count(old) != 1:
            raise RuntimeError(f"Expected one {old} definition in {filename}")
        definition = definition.replace(old, new, 1)
    return definition


def _view(filename: str, marker: str) -> str:
    source = (_SQL_DIR / filename).read_text(encoding="utf-8")
    start = source.index(marker)
    end = source.index("FROM public.projects AS project;", start) + len(
        "FROM public.projects AS project;"
    )
    return source[start:end].replace("CREATE VIEW", "CREATE OR REPLACE VIEW", 1)


_IMPL_RENAME = (
    "aap_workspace_project_query_v1.project_query_step(",
    "aap_workspace_project_query_v1._project_query_step_impl(",
)


def upgrade() -> None:
    for statement in _SQL.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if _has_sql(statement):
            _execute(statement.strip())


def downgrade() -> None:
    _execute(
        _definition(
            "0261_workspace_project_queries.sql",
            "CREATE FUNCTION aap_workspace_project_query_v1._project_row_json(",
        )
    )
    _execute(
        _definition(
            "0261_workspace_project_queries.sql",
            "CREATE FUNCTION aap_workspace_project_query_v1.project_query_step(",
            rename=_IMPL_RENAME,
        )
    )
    _execute(
        _definition(
            "0269_workspace_guide_assets.sql",
            "CREATE OR REPLACE FUNCTION "
            "aap_workspace_configuration_v1._guide_asset_read_project_row(",
        )
    )
    _execute(
        _definition(
            "0290_workspace_project_lock.sql",
            "CREATE OR REPLACE FUNCTION "
            "aap_workspace_configuration_v1._class_rename_project_row(",
        )
    )
    _execute(
        _view(
            "0212_asset_association_reads.sql",
            "CREATE VIEW aap_workspace_v1.project_catalog_configs",
        )
    )

    old_facts_view = _view(
        "0298_workspace_project_facts.sql",
        "CREATE VIEW aap_workspace_v1.project_facts",
    ).replace("CREATE OR REPLACE VIEW", "CREATE VIEW", 1)
    if (
        "$restore_view$" in old_facts_view
        or "$downgrade_project_facts$" in old_facts_view
    ):
        raise RuntimeError("Frozen Project facts view uses the restore delimiter")
    _execute(
        f"""
DO $downgrade_project_facts$
DECLARE
    v_owner name;
    v_relacl aclitem[];
    v_commands text[] := ARRAY[]::text[];
    v_entry record;
    v_column record;
    v_command text;
    v_relation regclass := 'aap_workspace_v1.project_facts'::regclass;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(relation.relowner),
           COALESCE(
               relation.relacl,
               pg_catalog.acldefault('r', relation.relowner)
           )
      INTO v_owner, v_relacl
    FROM pg_catalog.pg_class AS relation
    WHERE relation.oid = v_relation;

    FOR v_entry IN
        SELECT entry.grantee,
               entry.privilege_type,
               entry.is_grantable
        FROM pg_catalog.aclexplode(v_relacl) AS entry
    LOOP
        v_commands := v_commands || pg_catalog.format(
            'GRANT %s ON aap_workspace_v1.project_facts TO %s%s',
            v_entry.privilege_type,
            CASE
                WHEN v_entry.grantee = 0 THEN 'PUBLIC'
                ELSE pg_catalog.quote_ident(
                    pg_catalog.pg_get_userbyid(v_entry.grantee)
                )
            END,
            CASE
                WHEN v_entry.is_grantable THEN ' WITH GRANT OPTION'
                ELSE ''
            END
        );
    END LOOP;

    FOR v_column IN
        SELECT attribute.attname, attribute.attacl
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_relation
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
          AND attribute.attname <> 'guide_assets_revision'
          AND attribute.attacl IS NOT NULL
    LOOP
        FOR v_entry IN
            SELECT entry.grantee,
                   entry.privilege_type,
                   entry.is_grantable
            FROM pg_catalog.aclexplode(v_column.attacl) AS entry
        LOOP
            v_commands := v_commands || pg_catalog.format(
                'GRANT %s (%I) ON aap_workspace_v1.project_facts TO %s%s',
                v_entry.privilege_type,
                v_column.attname,
                CASE
                    WHEN v_entry.grantee = 0 THEN 'PUBLIC'
                    ELSE pg_catalog.quote_ident(
                        pg_catalog.pg_get_userbyid(v_entry.grantee)
                    )
                END,
                CASE
                    WHEN v_entry.is_grantable THEN ' WITH GRANT OPTION'
                    ELSE ''
                END
            );
        END LOOP;
    END LOOP;

    EXECUTE 'DROP VIEW aap_workspace_v1.project_facts RESTRICT';
    EXECUTE $restore_view${old_facts_view}$restore_view$;
    FOR v_entry IN
        SELECT entry.grantee
        FROM pg_catalog.pg_class AS relation
        CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) AS entry
        WHERE relation.oid = 'aap_workspace_v1.project_facts'::regclass
          AND entry.grantee <> relation.relowner
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON aap_workspace_v1.project_facts FROM %s RESTRICT',
            CASE
                WHEN v_entry.grantee = 0 THEN 'PUBLIC'
                ELSE pg_catalog.quote_ident(
                    pg_catalog.pg_get_userbyid(v_entry.grantee)
                )
            END
        );
    END LOOP;
    EXECUTE pg_catalog.format(
        'ALTER VIEW aap_workspace_v1.project_facts OWNER TO %I',
        v_owner
    );
    EXECUTE 'REVOKE ALL ON aap_workspace_v1.project_facts FROM PUBLIC';
    FOREACH v_command IN ARRAY v_commands LOOP
        EXECUTE v_command;
    END LOOP;
END;
$downgrade_project_facts$;
"""
    )
