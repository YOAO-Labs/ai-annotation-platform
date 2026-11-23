"""Fence physical Dataset import I/O on the accepted lease and original attempt."""

from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "bo0509"
down_revision = "bo0508"
branch_labels = depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1] / "sql" / "0509_import_saved_io.sql"
    for statement in sql.read_text(encoding="utf-8").split("-- aap-statement-break"):
        op.execute(
            text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
        )


def downgrade() -> None:
    op.execute("LOCK TABLE yoao_assets_v1.import_io_attempts IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $block$
        BEGIN
            IF EXISTS (SELECT FROM yoao_assets_v1.import_io_attempts) THEN
                RAISE EXCEPTION 'Settle Dataset import storage operations before downgrade';
            END IF;
        END;
        $block$;
    """)
    for signature in (
        "fail_import_original(jsonb,text,text,jsonb)",
        "record_import_original_written(jsonb,jsonb)",
        "mark_import_original_writing(jsonb)",
        "record_import_original_prepared(jsonb,jsonb)",
        "_import_builtin_original(jsonb,text,jsonb)",
        "import_saved_io_action(jsonb,text,jsonb,jsonb)",
        "_import_io_attempt(jsonb,boolean)",
        "open_dataset_import_original(uuid,uuid,bigint,uuid,uuid)",
        "_import_io_target(public.managed_original_creations)",
        "begin_dataset_import_original(uuid,uuid,jsonb)",
        "_import_io_authority(public.dataset_import_entries)",
        "_import_io_entry(uuid,bigint,uuid)",
        "save_observed_dataset_import_manifest(uuid,uuid,jsonb)",
        "import_saved_io_list(uuid,uuid,jsonb,jsonb)",
        "_import_io_transport(uuid,jsonb)",
        "_import_io_job(uuid,uuid,text)",
    ):
        op.execute(f"DROP FUNCTION yoao_assets_v1.{signature}")
    op.drop_table("import_io_attempts", schema="yoao_assets_v1")
    op.drop_table("import_io_manifests", schema="yoao_assets_v1")
