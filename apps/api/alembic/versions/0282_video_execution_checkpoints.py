"""Add immutable Native video window plans and checkpoint journals."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0282"
down_revision = "bo0281"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0282_video_execution_checkpoints.sql"
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
            IF EXISTS (SELECT 1 FROM aap_jobs_v1.video_execution_journals)
               OR EXISTS (SELECT 1 FROM aap_jobs_v1.video_execution_window_attempts)
               OR EXISTS (SELECT 1 FROM aap_jobs_v1.video_execution_captures)
               OR EXISTS (SELECT 1 FROM aap_jobs_v1.video_execution_windows)
               OR EXISTS (SELECT 1 FROM aap_jobs_v1.video_execution_span_closures)
               OR EXISTS (
                   SELECT 1 FROM aap_labeling_v1.video_execution_checkpoint_mask_sets
               )
               OR (SELECT pg_catalog.count(*)
                   FROM aap_jobs_v1.video_execution_checkpoint_restore) <> 4
            THEN
                RAISE EXCEPTION USING
                    ERRCODE='55000',
                    MESSAGE='cannot remove durable video execution checkpoints';
            END IF;
        END
        $guard$"""
    )
    _execute(
        "DROP TRIGGER aap_video_execution_checkpoint_terminal "
        "ON public.video_tracker_jobs"
    )
    _execute(
        "DROP FUNCTION aap_jobs_v1._video_execution_checkpoint_terminal() RESTRICT"
    )
    _execute(
        """DO $restore_functions$
        DECLARE
            v_definition text;
        BEGIN
            FOR v_definition IN
                SELECT restore.function_definition
                FROM aap_jobs_v1.video_execution_checkpoint_restore AS restore
                ORDER BY restore.signature
            LOOP
                EXECUTE v_definition;
            END LOOP;
        END
        $restore_functions$"""
    )
    _execute(
        "DROP FUNCTION aap_jobs_v1.video_execution_checkpoint_step(text,text) RESTRICT"
    )
    _execute("DROP FUNCTION aap_jobs_v1._video_checkpoint_input_facts(uuid) RESTRICT")
    _execute(
        "DROP FUNCTION "
        "aap_labeling_v1.retain_video_execution_checkpoint_masks(uuid,text,jsonb) RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_labeling_v1.video_execution_request_facts(uuid) RESTRICT"
    )
    _execute("DROP FUNCTION aap_assets_v1.video_execution_input_facts(uuid) RESTRICT")
    _execute(
        "DROP FUNCTION "
        "aap_jobs_v1._video_checkpoint_all_routes_retired(uuid,uuid) RESTRICT"
    )
    _execute(
        "DROP FUNCTION "
        "aap_jobs_v1.video_execution_checkpoint_mask_retained(text) RESTRICT"
    )
    _execute(
        "DROP FUNCTION "
        "aap_jobs_v1.video_execution_running_mask_roots_live(uuid) RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_jobs_v1.video_execution_source_item_retained(uuid) RESTRICT"
    )
    _execute("DROP FUNCTION aap_jobs_v1.video_execution_input_retained(uuid) RESTRICT")
    _execute(
        "DROP FUNCTION "
        "aap_jobs_v1._video_checkpoint_plan_valid(jsonb,jsonb,uuid,jsonb) RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_jobs_v1._video_checkpoint_manifest_valid(jsonb) RESTRICT"
    )
    _execute(
        "DROP FUNCTION "
        "aap_jobs_v1._video_checkpoint_plan_identities_complete(jsonb) RESTRICT"
    )
    _execute("DROP FUNCTION aap_jobs_v1._video_checkpoint_digest_valid(text) RESTRICT")
    _execute(
        "DROP FUNCTION aap_jobs_v1._video_checkpoint_integer(jsonb,bigint,bigint) RESTRICT"
    )
    _execute("DROP FUNCTION aap_jobs_v1._video_checkpoint_digest(jsonb) RESTRICT")
    _execute(
        "DROP FUNCTION aap_jobs_v1._video_checkpoint_canonical_json(jsonb) RESTRICT"
    )
    _execute("DROP FUNCTION aap_jobs_v1._video_checkpoint_reply(jsonb,text) RESTRICT")
    _execute(
        "DROP FUNCTION "
        "aap_inference_routing_v1.video_execution_scope_retired(uuid) RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_inference_routing_v1.video_execution_scope_facts(uuid) RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_jobs_v1._video_checkpoint_results_complete(uuid,uuid) RESTRICT"
    )
    for table in (
        "aap_jobs_v1.video_execution_capture_parts",
        "aap_jobs_v1.video_execution_window_parts",
        "aap_jobs_v1.video_execution_windows",
        "aap_jobs_v1.video_execution_captures",
        "aap_jobs_v1.video_execution_window_attempts",
        "aap_jobs_v1.video_execution_span_closures",
        "aap_jobs_v1.video_execution_journals",
        "aap_jobs_v1.video_execution_checkpoint_restore",
        "aap_labeling_v1.video_execution_checkpoint_mask_sets",
    ):
        _execute(f"DROP TABLE {table} RESTRICT")
