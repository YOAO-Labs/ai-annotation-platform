"""Prove registered migration bytes with exact durable Source receipts."""

from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "bo0508"
down_revision = "bo0507"
branch_labels = depends_on = None


_FUNCTIONS = (
    "migration_io_reconcile_flights(uuid)",
    "migration_io_close_flight(uuid,uuid)",
    "migration_io_open_flight(uuid,uuid,text)",
    "migration_io_cutover(uuid,integer,integer,integer,uuid,text,integer,uuid,integer,integer)",
    "migration_io_read(uuid,uuid,integer,text,text,integer,integer,boolean)",
    "migration_io_bind_read(uuid,uuid,integer,jsonb)",
    "migration_io_claim_write(uuid,uuid,text,uuid,text,integer,integer,text,integer,text)",
    "migration_io_baseline(uuid,uuid,text,uuid,text,integer,integer,text)",
    "migration_io_copy(uuid,uuid,text,uuid,text,integer,integer,text)",
    "_migration_io_target(uuid,jsonb,integer,integer)",
)


def _execute(statement):
    op.execute(
        text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1] / "sql" / "0508_migration_saved_io.sql"
    for statement in sql.read_text(encoding="utf-8").split("-- aap-statement-break"):
        _execute(statement)


def downgrade() -> None:
    _execute("""
        LOCK TABLE public.storage_migration_io_reads, public.storage_migration_io_writes,
            public.storage_migration_io_probes, public.storage_migration_io_flights
            IN ACCESS EXCLUSIVE MODE
    """)
    _execute("""
        DO $guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.storage_migration_io_reads)
               OR EXISTS (SELECT 1 FROM public.storage_migration_io_probes)
               OR EXISTS (SELECT 1 FROM public.storage_migration_io_flights)
               OR EXISTS (SELECT 1 FROM public.storage_migration_io_writes w
                          JOIN public.storage_migration_copy_attempts a ON a.id = w.attempt_id
                          JOIN public.storage_operations op ON op.id = a.target_operation_id) THEN
                RAISE EXCEPTION 'Settle migration Source physical work before downgrade';
            END IF;
        END
        $guard$;
    """)
    for function in _FUNCTIONS:
        _execute(f"DROP FUNCTION yoao_assets_v1.{function}")
    for table in (
        "storage_migration_io_flights",
        "storage_migration_io_probes",
        "storage_migration_io_writes",
        "storage_migration_io_reads",
    ):
        op.drop_table(table, schema="public")
