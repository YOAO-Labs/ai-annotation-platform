"""Add trusted Assets operator registration capabilities and receipts.

Revision ID: bo0218
Revises: bo0217
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0218"
down_revision = "bo0217"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def upgrade() -> None:
    artifact = _SQL_DIR / "0218_assets_operator.sql"
    for statement in artifact.read_text(encoding="utf-8").split(_STATEMENT_SEPARATOR):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def downgrade() -> None:
    receipt_count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM public.dataset_operator_operations"))
        .scalar_one()
    )
    if receipt_count:
        raise RuntimeError(
            "Cannot downgrade persisted trusted operator operation receipts; "
            f"{receipt_count} retained receipt(s) exist."
        )

    for signature in _DOWNGRADE_SIGNATURES:
        op.execute(f"DROP FUNCTION {signature} RESTRICT")
    op.drop_table("dataset_operator_operations", schema="public")


_DOWNGRADE_SIGNATURES: tuple[str, ...] = (
    "aap_assets_v1.read_operator_items(uuid, uuid, integer)",
    "aap_assets_v1.recount_operator_items(uuid, uuid)",
    "aap_assets_v1.patch_operator_items(uuid, uuid, jsonb)",
    "aap_assets_v1.append_operator_items(uuid, uuid, jsonb)",
    "aap_assets_v1.ensure_operator_nuscenes_dataset(uuid, uuid, jsonb)",
    "aap_assets_v1.create_operator_image_dataset(uuid, uuid, text, text)",
    "aap_assets_v1._operator_path_valid(text)",
    "aap_assets_v1._operator_item_json(public.dataset_items, text)",
    "aap_assets_v1._operator_source_snapshot(public.dataset_items)",
    "aap_assets_v1._operator_dataset_json(public.datasets)",
    "aap_assets_v1._operator_source_patch_valid(jsonb)",
    "aap_assets_v1._operator_source_entry_valid(jsonb)",
    "aap_assets_v1._operator_uuid_text_valid(text)",
    "aap_assets_v1._operator_user_exists(uuid)",
    "aap_assets_v1._operator_write_receipt(uuid, text, jsonb, jsonb)",
    "aap_assets_v1._operator_read_receipt(uuid, text, jsonb)",
    "aap_assets_v1._operator_operation_lock(uuid)",
    "aap_assets_v1._operator_reply(jsonb, text)",
)
