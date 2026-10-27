"""Retire the pre-managed upload Task capabilities."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0380"
down_revision = "bo0379"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_SQL = _SQL_DIR / "0380_labeling_upload_task_retirement.sql"
_PREVIOUS_CREATE = _SQL_DIR / "0375_original_authority_receipts.sql"
_LEGACY = _SQL_DIR / "0359_labeling_upload_tasks.sql"
_CREATE_FUNCTION = "aap_labeling_v1.create_uploading_task("


def _execute(source: str) -> None:
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def _create_definition() -> str:
    source = _PREVIOUS_CREATE.read_text(encoding="utf-8")
    start = source.index(_CREATE_FUNCTION)
    start = source.rindex("CREATE FUNCTION", 0, start)
    end = source.index("$function$;", start) + len("$function$;")
    return source[start:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def upgrade() -> None:
    _execute(_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    # Restore the exact frozen predecessor contracts: the narrow managed create
    # capability and the retired upload pair with its PUBLIC revocation.
    op.execute(
        sa.text(_create_definition().replace(":", r"\:")).execution_options(
            no_parameters=True
        )
    )
    _execute(_LEGACY.read_text(encoding="utf-8"))
