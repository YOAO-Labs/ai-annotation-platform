"""Retire exact Jobs publication sets with mixed settled outcomes."""

from importlib.resources import files

from alembic import op
import sqlalchemy as sa

revision = "bo0451"
down_revision = "bo0450"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        files("yoao_jobs")
        .joinpath("sql/0451_jobs_publication_retirement.sql")
        .read_text(encoding="utf-8")
    )
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise RuntimeError("Cannot discard mixed publication retirement evidence")
