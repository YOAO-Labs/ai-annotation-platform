"""Preserve original authority receipts and publication through domain owners."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0375"
down_revision = "bo0374"
branch_labels = depends_on = None

_SQL = (
    Path(__file__).resolve().parents[1] / "sql" / "0375_original_authority_receipts.sql"
)
_FUNCTIONS = (
    "aap_identity_v1.read_accepted_api_key_facts(uuid,uuid)",
    "aap_jobs_v1.read_job_execution_snapshot(uuid)",
    "aap_labeling_v1.create_uploading_task(uuid,uuid,text,text,text,text,uuid)",
    "aap_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid)",
    "aap_assets_v1.read_task_original_binding(uuid)",
    "aap_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid)",
    "aap_labeling_v1.lock_prediction_meta_for_core_read_share(uuid)",
    "aap_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid)",
    "aap_labeling_v1.publish_task_original(uuid,uuid,uuid,uuid,uuid,text)",
)


def upgrade() -> None:
    for statement in _SQL.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    for signature in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION {signature} RESTRICT")
