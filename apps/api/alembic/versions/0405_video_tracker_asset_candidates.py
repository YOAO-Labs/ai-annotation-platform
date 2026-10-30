"""Allow approval-filtered candidates in durable video route commands."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0405"
down_revision = "bo0404"
branch_labels = depends_on = None


def _apply(name: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / name).read_text(
        encoding="utf-8"
    )
    for statement in sql.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def upgrade() -> None:
    _apply("0405_video_tracker_asset_candidates.sql")


def downgrade() -> None:
    _apply("0405_video_tracker_asset_candidates_down.sql")
