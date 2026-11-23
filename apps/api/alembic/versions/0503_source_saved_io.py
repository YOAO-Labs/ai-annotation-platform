"""Separate registered Source credentials and fence binding capability probes."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0503"
down_revision = "bo0502"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql" / "0503_source_saved_io.sql"
_FUNCTIONS = (
    "saved_io_allowlist(jsonb)",
    "saved_io_connector(uuid,integer,jsonb)",
    "saved_io_target(uuid,integer,integer,jsonb)",
    "_saved_binding_probe_authority(uuid,uuid,text)",
    "_saved_binding_probe_facts(uuid,text,uuid,integer,integer,integer)",
    "begin_saved_binding_probe(uuid,uuid,uuid,text,text,uuid,integer,integer,integer)",
    "capture_saved_binding_probe(uuid,uuid,boolean)",
)


def upgrade() -> None:
    for statement in _SQL.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    op.execute("""
        DO $guard$
        BEGIN
            IF EXISTS (SELECT FROM yoao_assets_v1.saved_binding_probe) THEN
                RAISE EXCEPTION 'Settle all saved binding probes before downgrade';
            END IF;
        END;
        $guard$;
    """)
    for signature in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION yoao_assets_v1.{signature}")
    op.execute("DROP TABLE yoao_assets_v1.saved_binding_probe")
