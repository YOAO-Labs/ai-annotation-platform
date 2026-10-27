"""Add retained native state and bounded contracts for connector Dataset imports.

Revision ID: bo0214
Revises: bo0213
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0214"
down_revision = "bo0213"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_UPGRADE_ARTIFACTS = (
    "0214_dataset_import_delivery.sql",
    "0214_dataset_import_support.sql",
    "0214_dataset_import_core.sql",
)


def upgrade() -> None:
    active_legacy_imports = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT count(*)
            FROM public.async_jobs
            WHERE kind = 'dataset_import'
              AND status IN ('pending', 'running')
            """
            )
        )
        .scalar_one()
    )
    if active_legacy_imports:
        raise RuntimeError(
            "Cannot apply Dataset import ownership migration while "
            f"{active_legacy_imports} legacy dataset_import job(s) are pending or running; "
            "drain them before retrying 0214."
        )

    for filename in _UPGRADE_ARTIFACTS:
        artifact = _SQL_DIR / filename
        for statement in artifact.read_text(encoding="utf-8").split(
            _STATEMENT_SEPARATOR
        ):
            if statement.strip():
                op.execute(sa.text(statement.strip()))


def downgrade() -> None:
    active_operations, active_jobs, pending_deliveries = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT
                (SELECT count(*) FROM public.dataset_import_operations
                 WHERE stage IN ('enumerating', 'importing')),
                (SELECT count(*) FROM public.async_jobs
                 WHERE kind = 'dataset_import' AND status IN ('pending', 'running')),
                (SELECT count(*) FROM public.dataset_import_deliveries
                 WHERE delivered_at IS NULL)
            """
            )
        )
        .one()
    )
    if active_operations or active_jobs or pending_deliveries:
        raise RuntimeError(
            "Cannot downgrade active Dataset import ownership state: "
            f"{active_operations} operation(s), {active_jobs} job(s), and "
            f"{pending_deliveries} pending delivery record(s); drain them first."
        )

    for signature in (
        "aap_assets_v1.finish_dataset_import(uuid, uuid, text)",
        "aap_assets_v1.cancel_dataset_import(uuid, uuid)",
        "aap_assets_v1._dataset_import_result(uuid, boolean)",
        "aap_assets_v1.complete_dataset_import_entry(uuid, uuid, bigint, uuid, jsonb)",
        "aap_assets_v1.next_dataset_import_entry(uuid, uuid, uuid)",
        "aap_assets_v1.save_dataset_import_manifest(uuid, uuid, jsonb)",
        "aap_assets_v1.read_dataset_import_connection(uuid, uuid, jsonb)",
        "aap_assets_v1.heartbeat_dataset_import(uuid, uuid, integer)",
        "aap_assets_v1.claim_dataset_import(uuid, integer, uuid)",
        "aap_assets_v1.accept_dataset_import(uuid, uuid, uuid, uuid, jsonb, jsonb, jsonb)",
        "aap_assets_v1.prepare_dataset_import(uuid, uuid, uuid)",
        "aap_assets_v1._dataset_import_job_active(uuid, uuid)",
        "aap_assets_v1._dataset_import_request_valid(jsonb, jsonb)",
        "aap_assets_v1._dataset_import_connection_usable(uuid, text, text, uuid)",
        "aap_assets_v1._dataset_import_actor(uuid)",
        "aap_assets_v1._dataset_import_file_type(text)",
        "aap_assets_v1._dataset_import_destination_path(text, text, text)",
        "aap_assets_v1._dataset_import_source_valid(jsonb)",
        "aap_assets_v1._dataset_import_relative_path_valid(text, integer, integer)",
        "aap_assets_v1._dataset_import_connection_snapshot(uuid)",
        "aap_assets_v1._dataset_import_connection_config_valid(text, jsonb)",
        "aap_assets_v1._dataset_import_state(uuid)",
        "aap_assets_v1._dataset_import_reply(jsonb, text)",
    ):
        op.execute(f"DROP FUNCTION {signature} RESTRICT")

    op.drop_table("dataset_import_entries", schema="public")
    op.drop_table("dataset_import_operations", schema="public")

    for signature in (
        "aap_audit_v1.record_dataset_import(uuid, uuid, uuid, jsonb, jsonb)",
        "aap_jobs_v1.cancel_dataset_import_job(uuid, uuid, jsonb)",
        "aap_jobs_v1.finish_dataset_import_job(uuid, text, jsonb, text)",
        "aap_jobs_v1._dataset_import_notification_payload(public.async_jobs, text, jsonb)",
        "aap_jobs_v1._dataset_import_result_valid(jsonb)",
        "aap_jobs_v1.update_dataset_import_job_progress(uuid, text, integer, integer, bigint)",
        "aap_jobs_v1.claim_dataset_import_job(uuid)",
        "aap_jobs_v1.lock_dataset_import_job(uuid)",
        "aap_jobs_v1.create_dataset_import_job(uuid, uuid, jsonb)",
        "aap_jobs_v1._dataset_import_job_reply(jsonb, text)",
        "aap_notifications_v1.create_job_terminal_notification(uuid, uuid, text, jsonb)",
        "aap_settings_v1.resolve_connector_host_allowlist(jsonb)",
    ):
        op.execute(f"DROP FUNCTION {signature} RESTRICT")

    op.execute("DROP SCHEMA aap_notifications_v1 RESTRICT")
    op.execute("DROP SCHEMA aap_settings_v1 RESTRICT")

    for signature in (
        "aap_assets_v1.complete_dataset_import_delivery(uuid, uuid)",
        "aap_assets_v1.claim_dataset_import_delivery(uuid, integer)",
        "aap_assets_v1.dataset_import_delivery_target_is_current(text, uuid)",
        "aap_assets_v1.queue_dataset_import_delivery(uuid, text, uuid)",
        "aap_assets_v1._dataset_import_delivery_reply(jsonb, text)",
    ):
        op.execute(f"DROP FUNCTION {signature} RESTRICT")
    op.drop_table("dataset_import_deliveries", schema="public")
