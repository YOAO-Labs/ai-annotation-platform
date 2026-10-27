"""Checkpoint Mask repair rollback shards through the Labeling owner."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0385"
down_revision = "bo0384"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACT = "0385_labeling_repair_rollback_shard.sql"
_FROZEN = "0353_labeling_mask_quality.sql"
_FUNCTION = "aap_labeling_v1.mask_repair_command"


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def _frozen_function_definition() -> str:
    source = (_SQL_DIR / _FROZEN).read_text(encoding="utf-8")
    prefix = f"CREATE FUNCTION {_FUNCTION}("
    if source.count(prefix) != 1:
        raise RuntimeError(f"Expected one frozen definition of {_FUNCTION}")
    start = source.index(prefix)
    end = source.index("$function$;", start) + len("$function$;")
    return source[start:end].replace(
        prefix, f"CREATE OR REPLACE FUNCTION {_FUNCTION}(", 1
    )


def upgrade() -> None:
    for statement in (
        (_SQL_DIR / _ARTIFACT)
        .read_text(encoding="utf-8")
        .split("-- aap-statement-break")
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    # CREATE OR REPLACE keeps the Labeling owner and its granted ACL.
    _execute(_frozen_function_definition())
