"""Honor exact presentation retirement in an open fixture run."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0441"
down_revision = "bo0440"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def _execute(source: str) -> None:
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def upgrade() -> None:
    _execute(
        (_SQL_DIR / "0441_fixture_presentation_dataset_guard.sql").read_text(
            encoding="utf-8"
        )
    )


def downgrade() -> None:
    frozen = (_SQL_DIR / "0432_fixture_dataset_jobs_anchor.sql").read_text(
        encoding="utf-8"
    )
    declaration = frozen.split("-- aap-statement-break")[0]
    _execute(declaration.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
