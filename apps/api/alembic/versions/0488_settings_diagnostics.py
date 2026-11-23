"""Install fixed owner capabilities for runtime settings and diagnostics."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0488"
down_revision = "bo0487"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "sql" / "0488_settings_diagnostics.sql"
    ).read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise NotImplementedError(
        "Settings and Diagnostics runtime ACL cutover is forward-only"
    )
