"""Install fixed Jobs list page and total query functions."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0326"
down_revision = "bo0325"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0326_jobs_list_query.sql"


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION "
        "aap_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION "
        "aap_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION "
        "aap_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text) RESTRICT"
    )
