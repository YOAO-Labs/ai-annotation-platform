"""Move remaining ordinary Jobs ledger access to fixed owner capabilities."""

from importlib.resources import files

from alembic import op
import sqlalchemy as sa

revision = "bo0502"
down_revision = "bo0501"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        files("yoao_jobs")
        .joinpath("sql/0502_jobs_ordinary_ledger.sql")
        .read_text(encoding="utf-8")
    )
    for statement in source.split("-- aap-statement-break"):
        op.execute(
            sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
        )


def downgrade() -> None:
    raise NotImplementedError("Jobs private ledger access is forward-only")
