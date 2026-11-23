"""Expose fixed Learning model return and evaluation capabilities."""

from pathlib import Path
import re

import sqlalchemy as sa
from alembic import op

revision = "bo0492"
down_revision = "bo0491"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql" / "0492_learning_models.sql"
_FUNCTION = re.compile(r"CREATE FUNCTION (yoao_learning_v1\.\w+)\(([^)]*)\)")
_VIEW = re.compile(r"CREATE VIEW (yoao_learning_v1\.\w+) AS")


def _objects():
    for block in _SQL.read_text(encoding="utf-8").split("-- aap-statement-break"):
        statement = block.strip()
        if match := _FUNCTION.match(statement):
            name, args = match.groups()
            types = ",".join(arg.split()[1] for arg in args.split(",") if arg.strip())
            yield statement, "FUNCTION", f"{name}({types})"
        elif match := _VIEW.match(statement):
            yield statement, "VIEW", match[1]
        else:
            raise ValueError("Unrecognized Learning capability declaration")


def upgrade() -> None:
    for statement, kind, identity in _objects():
        op.execute(
            sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
        )
        acl_kind = "TABLE" if kind == "VIEW" else kind
        op.execute(f"REVOKE ALL ON {acl_kind} {identity} FROM PUBLIC")


def downgrade() -> None:
    for _, kind, identity in reversed(list(_objects())):
        op.execute(f"DROP {kind} {identity}")
