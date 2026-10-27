"""Add Assets-owned Dataset upload intents.

Revision ID: bo0207
Revises: bo0206
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0207"
down_revision = "bo0206"
branch_labels = depends_on = None

_ARTIFACT = Path(__file__).resolve().parents[1] / "sql/0207_dataset_upload_intents.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def upgrade() -> None:
    for statement in _ARTIFACT.read_text(encoding="utf-8").split(_STATEMENT_SEPARATOR):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_assets_v1.discard_dataset_upload(uuid, uuid, uuid, text) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1.complete_dataset_upload(uuid, uuid, uuid, uuid, bigint, text, integer, integer) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1.find_dataset_upload_duplicate(uuid, uuid, uuid, text) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1.lock_dataset_upload(uuid, uuid, uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1.reserve_dataset_upload(uuid, uuid, uuid, text, text) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1._finish_dataset_upload(uuid, uuid, uuid, uuid, bigint) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1._accept_dataset_upload(uuid, uuid, uuid, uuid, bigint, text, integer, integer) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1._dataset_upload_facts(public.dataset_upload_intents) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_assets_v1._dataset_upload_actor(uuid) RESTRICT")
    op.execute("DROP FUNCTION aap_assets_v1._dataset_upload_success(jsonb) RESTRICT")
    op.execute("DROP FUNCTION aap_assets_v1._dataset_upload_failure(text) RESTRICT")
    op.drop_table("dataset_upload_intents")
