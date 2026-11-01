"""Fence managed Registry authentication updates against concurrent edits."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0410"
down_revision = "bo0409"
branch_labels = depends_on = None


def upgrade() -> None:
    source = Path(__file__).resolve().parents[1] / "sql" / "0410_inference_managed_auth.sql"
    for statement in source.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_inference_topology_v1.patch_registry_auth_cas(uuid,text,text,bigint)")
    op.execute("DROP FUNCTION aap_inference_topology_v1.registry_auth_snapshot(uuid)")
