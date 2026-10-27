"""Add coordinated, proof-bound video execution restore."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0284"
down_revision = "bo0283"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0284_video_restore.sql"


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
            IF EXISTS (SELECT 1 FROM aap_inference_routing_v1.video_restore_records)
               OR EXISTS (SELECT 1 FROM aap_inference_routing_v1.video_restore_contexts)
               OR EXISTS (SELECT 1 FROM aap_inference_routing_v1.video_restore_scopes)
               OR EXISTS (SELECT 1 FROM aap_inference_routing_v1.video_restore_attempts)
               OR EXISTS (SELECT 1 FROM aap_inference_routing_v1.video_restore_scope_retirements)
               OR EXISTS (SELECT 1 FROM aap_inference_routing_v1.video_restore_no_scope_receipts)
               OR EXISTS (SELECT 1 FROM aap_jobs_v1.video_restore_fence)
               OR (SELECT pg_catalog.count(*)
                   FROM aap_inference_routing_v1.video_restore_function_restore) <> 5
            THEN
                RAISE EXCEPTION USING
                    ERRCODE='55000',
                    MESSAGE='cannot remove coordinated video restore state';
            END IF;
        END
        $guard$"""
    )
    _execute(
        """DO $restore_functions$
        DECLARE
            v_definition text;
        BEGIN
            FOR v_definition IN
                SELECT function_definition
                FROM aap_inference_routing_v1.video_restore_function_restore
                ORDER BY signature
            LOOP
                EXECUTE v_definition;
            END LOOP;
        END
        $restore_functions$"""
    )
    for trigger, table in (
        (
            "aap_video_restore_record_guard",
            "aap_inference_routing_v1.video_restore_records",
        ),
        (
            "aap_video_restore_context_guard",
            "aap_inference_routing_v1.video_restore_contexts",
        ),
        (
            "aap_video_restore_scope_snapshot_immutable",
            "aap_inference_routing_v1.video_restore_scopes",
        ),
        (
            "aap_video_restore_attempt_snapshot_immutable",
            "aap_inference_routing_v1.video_restore_attempts",
        ),
        (
            "aap_video_restore_no_scope_immutable",
            "aap_inference_routing_v1.video_restore_no_scope_receipts",
        ),
        (
            "aap_video_restore_scope_retirement_guard",
            "aap_inference_routing_v1.video_restore_scope_retirements",
        ),
    ):
        _execute(f"DROP TRIGGER {trigger} ON {table} RESTRICT")
    _execute(
        "DROP FUNCTION aap_inference_routing_v1.video_restore_step(text,text) RESTRICT"
    )
    _execute("DROP FUNCTION aap_jobs_v1.video_restore_authority(text,jsonb) RESTRICT")
    _execute(
        "DROP FUNCTION aap_jobs_v1._video_restore_route_snapshot(bytea,text) RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_inference_routing_v1._video_execution_scope_step_pre_restore(text,text) RESTRICT"
    )
    for signature in (
        "aap_inference_routing_v1._video_restore_scope_retired(uuid)",
        "aap_inference_routing_v1._video_restore_registry_snapshot(uuid)",
        "aap_inference_routing_v1._video_restore_contexts(uuid)",
        "aap_inference_routing_v1._video_restore_record(uuid)",
        "aap_inference_routing_v1._video_restore_command_sha256(jsonb)",
        "aap_inference_routing_v1._video_restore_digest_valid(text)",
        "aap_inference_routing_v1._video_restore_uuid(jsonb)",
        "aap_inference_routing_v1._video_restore_exact_keys(jsonb,text[])",
        "aap_inference_routing_v1._video_restore_reply(jsonb,text)",
        "aap_inference_routing_v1._video_restore_record_guard()",
        "aap_inference_routing_v1._video_restore_context_guard()",
        "aap_inference_routing_v1._video_restore_scope_retirement_guard()",
        "aap_inference_routing_v1._video_restore_immutable()",
    ):
        _execute(f"DROP FUNCTION {signature} RESTRICT")
    for table in (
        "aap_inference_routing_v1.video_restore_no_scope_receipts",
        "aap_inference_routing_v1.video_restore_scope_retirements",
        "aap_inference_routing_v1.video_restore_attempts",
        "aap_inference_routing_v1.video_restore_scopes",
        "aap_inference_routing_v1.video_restore_contexts",
        "aap_inference_routing_v1.video_restore_records",
        "aap_inference_routing_v1.video_restore_function_restore",
        "aap_jobs_v1.video_restore_fence",
    ):
        _execute(f"DROP TABLE {table} RESTRICT")
