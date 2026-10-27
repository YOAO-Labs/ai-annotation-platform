"""Own the guarded E2E fixture account and invitation operations."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0305"
down_revision = "bo0304"
branch_labels = depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1] / "sql/0305_identity_e2e_fixtures.sql"
    for statement in sql.read_text().split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    for signature in (
        "create_account(uuid,text,text,text,text)",
        "mark_manual_inactive(uuid,timestamptz,uuid)",
        "create_invitations(jsonb)",
        "delete_accounts(boolean,text[])",
        "delete_reset_tokens(uuid[])",
        "delete_invitations(uuid[])",
        "delete_organization_members(uuid[])",
    ):
        op.execute(f"DROP FUNCTION aap_identity_fixture_v1.{signature} RESTRICT")
    op.execute("DROP VIEW aap_identity_fixture_v1.account_profiles RESTRICT")
    op.execute("DROP SCHEMA aap_identity_fixture_v1 RESTRICT")
