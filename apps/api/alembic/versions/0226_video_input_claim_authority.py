"""Keep revoked video requesters from blocking later input and frame claims.

Revision ID: bo0226
Revises: bo0225
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0226"
down_revision = "bo0225"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_UPGRADE_ARTIFACTS = ("0226_video_input_claim_authority.sql",)
_SCHEMA_ARTIFACTS = ()
_CLAIMS = ("claim_accepted_video_input", "claim_accepted_video_frame")


def _statements(name):
    return [
        statement.strip()
        for statement in (_SQL_DIR / name).read_text().split("-- aap-statement-break")
        if statement.strip()
    ]


def _execute(statement):
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade():
    for statement in _statements(_UPGRADE_ARTIFACTS[0]):
        _execute(statement)


def downgrade():
    for name in _CLAIMS:
        statement = next(
            statement
            for statement in _statements("0225_video_inputs_execution.sql")
            if "CREATE FUNCTION aap_assets_v1." + name + "(" in statement
        )
        _execute(statement.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
