"""Add atomic Labeling finalization for claimed video execution."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0283"
down_revision = "bo0282"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0283_labeling_video_writer.sql"
)


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    _execute(
        """DO $guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM aap_labeling_v1.video_writer_receipts) THEN
                RAISE EXCEPTION USING
                    ERRCODE='55000',
                    MESSAGE='cannot remove immutable video writer receipts';
            END IF;
        END
        $guard$"""
    )
    _execute(
        "DROP TRIGGER aap_video_writer_receipt_immutable "
        "ON aap_labeling_v1.video_writer_receipts"
    )
    _execute("DROP FUNCTION aap_labeling_v1.video_writer_step(text,text) RESTRICT")
    _execute("DROP FUNCTION aap_jobs_v1.video_writer_authority(text,jsonb) RESTRICT")
    _execute(
        "DROP FUNCTION aap_labeling_v1._video_writer_stage_validate("
        "jsonb,jsonb,text,text,jsonb,jsonb) RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_labeling_v1._video_writer_authority_code(text) RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_labeling_v1._video_writer_facts(jsonb,text,jsonb,jsonb) RESTRICT"
    )
    _execute("DROP FUNCTION aap_labeling_v1._video_writer_mask_epochs(jsonb) RESTRICT")
    _execute(
        "DROP FUNCTION aap_labeling_v1._video_writer_source_map(uuid,jsonb,jsonb) RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_labeling_v1._video_writer_instance_number(text) RESTRICT"
    )
    _execute("DROP FUNCTION aap_labeling_v1._video_writer_digest_valid(text) RESTRICT")
    _execute(
        "DROP FUNCTION aap_labeling_v1._video_writer_exact_keys(jsonb,text[]) RESTRICT"
    )
    _execute("DROP FUNCTION aap_labeling_v1._video_writer_reply(jsonb,text) RESTRICT")
    _execute("DROP FUNCTION aap_labeling_v1._video_writer_receipt_immutable() RESTRICT")
    _execute("DROP TABLE aap_labeling_v1.video_writer_receipts RESTRICT")
