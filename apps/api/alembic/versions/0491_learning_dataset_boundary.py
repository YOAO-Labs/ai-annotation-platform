"""Install transaction-bound Learning dataset, analysis and cleanup capabilities."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0491"
down_revision = "bo0490"
branch_labels = depends_on = None

_SQL = (
    Path(__file__).resolve().parents[1] / "sql" / "0491_learning_dataset_boundary.sql"
)
_SCHEMA = "yoao_learning_v1"
_VIEW = f"{_SCHEMA}.training_dataset_metadata"
_FUNCTIONS = (
    "training_dataset_task(jsonb)",
    "training_dataset_profile(jsonb,text)",
    "training_dataset_facts(uuid,uuid,boolean)",
    "training_dataset_read(uuid,uuid,boolean)",
    "training_dataset_hold(uuid,uuid)",
    "training_dataset_request(uuid,uuid,boolean)",
    "training_dataset_job_lock(uuid)",
    "training_dataset_create(uuid,uuid,uuid,uuid,uuid,text,uuid,text,jsonb,text,jsonb)",
    "training_dataset_stream(uuid,uuid)",
    "training_dataset_associate(uuid,uuid)",
    "training_dataset_set_storage(uuid,uuid,uuid,jsonb)",
    "training_dataset_queue(uuid,uuid,uuid,uuid)",
    "training_dataset_start(uuid,uuid,uuid)",
    "training_dataset_progress(uuid,uuid,uuid,integer)",
    "training_dataset_publish(uuid,uuid,uuid,text,jsonb,jsonb,text,text,bigint,timestamptz)",
    "training_dataset_fail(uuid,uuid,uuid,text,boolean,text,text,timestamptz)",
    "training_dataset_delete(uuid,uuid,uuid)",
    "training_dataset_manifest(uuid,uuid)",
    "training_dataset_sample_evidence(uuid,uuid,uuid)",
    "training_original_history()",
    "training_binding_history()",
    "training_dataset_asset_versions(uuid,boolean)",
    "training_dataset_location_versions(uuid)",
    "training_analysis_read(uuid)",
    "training_analysis_start(uuid,uuid,text,text,text,jsonb)",
    "training_analysis_add_members(uuid,text,jsonb)",
    "training_analysis_sample_page(uuid,text,integer,bigint,text,integer,text,text,text[])",
    "training_analysis_member(uuid,uuid)",
    "training_analysis_observations(uuid)",
    "training_analysis_media_counts(uuid)",
    "training_analysis_observe_media(uuid,uuid,text,bigint,text,timestamptz)",
    "training_storage_stage_deletion(uuid,uuid,text,jsonb)",
    "training_storage_claim_deletion(uuid,timestamptz)",
    "training_storage_complete_deletion(uuid,jsonb)",
    "training_storage_deletion_batch()",
    "training_dataset_high_water_violations(uuid)",
)


def upgrade() -> None:
    for statement in _SQL.read_text(encoding="utf-8").split("-- aap-statement-break"):
        op.execute(
            sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
        )
    op.execute(f"REVOKE ALL ON {_VIEW} FROM PUBLIC")
    functions = ", ".join(f"{_SCHEMA}.{signature}" for signature in _FUNCTIONS)
    op.execute(f"REVOKE ALL ON FUNCTION {functions} FROM PUBLIC")


def downgrade() -> None:
    op.execute(f"DROP VIEW {_VIEW}")
    for signature in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION {_SCHEMA}.{signature}")
