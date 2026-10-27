"""Own fixed capabilities for the local scale-seed workflow."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0310"
down_revision = "bo0309"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0310_workspace_scale_seed.sql"
)
_FUNCTIONS = (
    "create_scale_seed_project(uuid,text)",
    "ensure_scale_seed_member(uuid,uuid,uuid)",
    "guard_scale_seed_cleanup(text)",
    "delete_scale_seed_projects(text,uuid[])",
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean) RESTRICT"
    )
    for signature in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION aap_workspace_v1.{signature} RESTRICT")
