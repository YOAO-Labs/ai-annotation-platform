"""Allow contributor evidence alongside shared batch readers."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0427"
down_revision = "bo0426"
branch_labels = depends_on = None


def _apply(name: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / name).read_text(
        encoding="utf-8"
    )
    for statement in sql.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def upgrade() -> None:
    _apply("0427_labeling_actor_batch_share.sql")


def downgrade() -> None:
    _apply("0427_labeling_actor_batch_share_down.sql")
