"""Publish managed guide assets through the Workspace transaction owner."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0374"
down_revision = "bo0373"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    source = (_SQL_DIR / "0374_workspace_guide_publication.sql").read_text(
        encoding="utf-8"
    )
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    source = (_SQL_DIR / "0269_workspace_guide_assets.sql").read_text(encoding="utf-8")
    name = "aap_workspace_configuration_v1.configuration_step"
    prefixes = (f"CREATE FUNCTION {name}(", f"CREATE OR REPLACE FUNCTION {name}(")
    matches = [prefix for prefix in prefixes if prefix in source]
    if len(matches) != 1 or source.count(matches[0]) != 1:
        raise RuntimeError("Expected one frozen Workspace configuration dispatcher")
    start = source.index(matches[0])
    end = source.index("$function$;", start) + len("$function$;")
    definition = source[start:end]
    if definition.startswith("CREATE FUNCTION"):
        definition = definition.replace(
            "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
        )
    _execute(definition)
    _execute(
        "DROP FUNCTION "
        "aap_workspace_configuration_v1.guide_assets_step(jsonb,text,jsonb), "
        "aap_workspace_configuration_v1._guide_failure(jsonb,text,integer,text), "
        "aap_workspace_configuration_v1._guide_storage_gate(uuid,bigint), "
        "aap_workspace_configuration_v1._guide_authority(uuid,uuid), "
        "aap_workspace_configuration_v1._guide_lock_project(uuid,text), "
        "aap_workspace_configuration_v1._guide_entry_matches(jsonb,uuid,uuid,text,text,integer,timestamptz) "
        "RESTRICT"
    )
