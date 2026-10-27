"""Add the Assets storage-connection native boundary and immutable receipts.

Revision ID: bo0217
Revises: bo0216
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0217"
down_revision = "bo0216"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_UPGRADE_ARTIFACTS = (
    "0217_assets_connections_support.sql",
    "0217_assets_connections.sql",
)


def _run_artifact(filename: str) -> None:
    artifact = _SQL_DIR / filename
    for statement in artifact.read_text(encoding="utf-8").split(_STATEMENT_SEPARATOR):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def upgrade() -> None:
    for filename in _UPGRADE_ARTIFACTS:
        _run_artifact(filename)


def downgrade() -> None:
    receipt_count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM public.storage_connection_operations"))
        .scalar_one()
    )
    if receipt_count:
        raise RuntimeError(
            "Cannot downgrade persisted storage connection operation receipts; "
            f"{receipt_count} retained receipt(s) exist."
        )

    for signature in _DOWNGRADE_SIGNATURES:
        op.execute(f"DROP FUNCTION {signature} RESTRICT")
    op.drop_table("storage_connection_operations", schema="public")
    op.execute(
        "DROP FUNCTION aap_settings_v1.lock_connector_host_allowlist(jsonb) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_audit_v1.record_storage_connection_change(uuid, uuid, text, uuid, jsonb, jsonb) RESTRICT"
    )


_DOWNGRADE_SIGNATURES: tuple[str, ...] = (
    "aap_assets_v1.record_storage_connection_test(uuid, uuid, uuid, text, text, jsonb, jsonb, jsonb)",
    "aap_assets_v1.delete_storage_connection(uuid, uuid, uuid, jsonb)",
    "aap_assets_v1.update_storage_connection(uuid, uuid, uuid, jsonb, jsonb, text, text, text, jsonb, jsonb)",
    "aap_assets_v1.create_storage_connection(uuid, uuid, jsonb, jsonb, text, text, jsonb, jsonb)",
    "aap_assets_v1.read_storage_connection_receipt(uuid, uuid, text, uuid, jsonb)",
    "aap_assets_v1.capture_storage_connection(uuid, uuid, text)",
    "aap_assets_v1.read_storage_connection_policy(uuid, jsonb)",
    "aap_assets_v1.read_storage_connection(uuid, uuid)",
    "aap_assets_v1.list_storage_connections(uuid, timestamptz, uuid, integer)",
    "aap_assets_v1._storage_connection_operation_lock(uuid)",
    "aap_assets_v1._storage_connection_policy(jsonb, boolean)",
    "aap_assets_v1._storage_connection_ciphertext_valid(text)",
    "aap_assets_v1._storage_connection_intent_valid(text, jsonb)",
    "aap_assets_v1._storage_connection_config_valid(text, jsonb)",
    "aap_assets_v1._storage_connection_snapshot(public.storage_connections)",
    "aap_assets_v1._storage_connection_facts(public.storage_connections)",
    "aap_assets_v1._storage_connection_actor(uuid, boolean)",
    "aap_assets_v1._storage_connection_access(uuid, text, text, uuid, text)",
    "aap_assets_v1._storage_connection_reply(jsonb, text)",
)
