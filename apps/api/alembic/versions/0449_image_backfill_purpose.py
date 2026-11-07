"""Schedule the requested image backfill purpose directly."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0449"
down_revision = "bo0448"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def _execute(source: str) -> None:
    op.execute(sa.text(source.strip().replace(":", r"\:")).execution_options(no_parameters=True))


def upgrade() -> None:
    for statement in (_SQL_DIR / "0449_image_backfill_purpose.sql").read_text(
        encoding="utf-8"
    ).split("-- aap-statement-break"):
        if statement.strip():
            _execute(statement)


def downgrade() -> None:
    source = (_SQL_DIR / "0220_assets_images_execution.sql").read_text(encoding="utf-8")
    for name in ("advance_image_backfill", "fail_image_media"):
        statement = next(
            part for part in source.split("-- aap-statement-break")
            if part.strip().startswith(f"CREATE FUNCTION aap_assets_v1.{name}(")
        )
        _execute(statement.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
