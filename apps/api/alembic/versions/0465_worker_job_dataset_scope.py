"""Record verifiable dataset scope at retry and cross-frame Job birth."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0465"
down_revision = "bo0464"
branch_labels = depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1] / "sql"
    for name in (
        "0465_worker_job_dataset_receipts.sql",
        "0465_retry_born_dataset_scope.sql",
        "0465_cross_frame_born_dataset_scope.sql",
        "0465_worker_job_dataset_scope.sql",
    ):
        source = (sql / name).read_text(encoding="utf-8")
        for statement in source.split("-- aap-statement-break"):
            if statement.strip():
                op.execute(
                    sa.text(statement.strip().replace(":", r"\:")).execution_options(
                        no_parameters=True
                    )
                )


def downgrade() -> None:
    raise NotImplementedError("Born Job dataset scope is forward-only")
