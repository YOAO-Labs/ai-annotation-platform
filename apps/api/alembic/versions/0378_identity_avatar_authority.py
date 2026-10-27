"""Recheck the accepted API key's exact frozen authority before publication."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0378"
down_revision = "bo0377"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_SQL = _SQL_DIR / "0378_identity_avatar_authority.sql"
_AVATAR_SQL = _SQL_DIR / "0371_identity_avatar_account_command.sql"
_FUNCTION = "aap_identity_accounts_v1.apply_account_command(jsonb)"


def upgrade() -> None:
    for statement in _SQL.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    # Restoring the forward definition preserves the function owner and ACLs.
    source = _AVATAR_SQL.read_text(encoding="utf-8")
    prefixes = (
        "CREATE FUNCTION aap_identity_accounts_v1.apply_account_command(",
        "CREATE OR REPLACE FUNCTION aap_identity_accounts_v1.apply_account_command(",
    )
    matches = [prefix for prefix in prefixes if prefix in source]
    if len(matches) != 1 or source.count(matches[0]) != 1:
        raise RuntimeError(f"Expected one frozen definition of {_FUNCTION}")
    start = source.index(matches[0])
    end = source.index("$function$;", start) + len("$function$;")
    definition = source[start:end]
    if definition.startswith("CREATE FUNCTION"):
        definition = definition.replace(
            "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
        )
    op.execute(
        sa.text(definition.replace(":", r"\:")).execution_options(no_parameters=True)
    )
