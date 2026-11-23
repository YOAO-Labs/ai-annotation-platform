"""Keep storage registration, physical permits and site-agent commands with Assets."""

from pathlib import Path
import re

import sqlalchemy as sa
from alembic import op

revision = "bo0494"
down_revision = "bo0493"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_STATEMENT = re.compile(r"(?<=;)\n+(?=CREATE |REVOKE )")


def upgrade() -> None:
    for name in ("0494_storage_access.sql", "0494_storage_maintenance_facts.sql"):
        for statement in _STATEMENT.split((_SQL / name).read_text(encoding="utf-8")):
            op.execute(
                sa.text(statement.replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )
    op.execute("SELECT yoao_assets_storage_v1._adopt_installed_owners()")


def downgrade() -> None:
    op.execute("DROP FUNCTION yoao_audit_v1.archive_location_referenced(uuid)")
    op.execute("DROP FUNCTION yoao_audit_v1.archive_maintenance_targets(boolean)")
    op.execute("DROP SCHEMA yoao_assets_storage_v1 CASCADE")
