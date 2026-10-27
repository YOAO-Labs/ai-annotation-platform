"""Add durable video execution claims and atomic route-scope binding."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0281"
down_revision = "bo0280"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0281_video_execution.sql"


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
            IF EXISTS (SELECT 1 FROM aap_jobs_v1.video_executions)
               OR EXISTS (SELECT 1 FROM aap_jobs_v1.video_execution_route_intents)
               OR EXISTS (SELECT 1 FROM aap_inference_routing_v1.video_execution_scope_permits)
               OR EXISTS (
                    SELECT 1 FROM aap_inference_routing_v1.command_receipts
                    WHERE operation = 'video_acquire'
               )
               OR EXISTS (
                    SELECT 1 FROM aap_inference_routing_v1.scopes
                    WHERE operation = 'tracker'
                      AND state IN ('reserved','active','uncertain')
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'cannot remove durable video execution claims or route intents';
            END IF;
        END
        $guard$"""
    )
    _execute(
        "DROP TRIGGER aap_route_video_execution_attempt_gate "
        "ON aap_inference_routing_v1.attempts RESTRICT"
    )
    _execute(
        "DROP TRIGGER aap_route_video_execution_heartbeat_gate "
        "ON aap_inference_routing_v1.scopes RESTRICT"
    )
    _execute(
        "DROP TRIGGER aap_route_video_execution_scope_permit "
        "ON aap_inference_routing_v1.scopes RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_inference_routing_v1.video_execution_scope_step(text, text) RESTRICT"
    )
    _execute("DROP FUNCTION aap_inference_routing_v1.routing_step(text, text) RESTRICT")
    _execute(
        "ALTER FUNCTION aap_inference_routing_v1._routing_step_0279(text, text) "
        "RENAME TO routing_step"
    )
    _execute(
        "DROP FUNCTION aap_inference_routing_v1.scope_retirement_step(text, text) RESTRICT"
    )
    _execute(
        "ALTER FUNCTION aap_inference_routing_v1._scope_retirement_step_0280(text, text) "
        "RENAME TO scope_retirement_step"
    )
    _execute(
        """DO $restore_fixed_routing_role$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = 'inference_routing_client'
            ) THEN
                EXECUTE 'GRANT EXECUTE ON FUNCTION '
                    || 'aap_inference_routing_v1.routing_step(text, text) '
                    || 'TO inference_routing_client';
                EXECUTE 'GRANT EXECUTE ON FUNCTION '
                    || 'aap_inference_routing_v1.scope_retirement_step(text, text) '
                    || 'TO inference_routing_client';
            END IF;
        END
        $restore_fixed_routing_role$"""
    )
    for signature in (
        "aap_inference_routing_v1._video_execution_canonical_json(jsonb)",
        "aap_inference_routing_v1._video_execution_scope_uuid(uuid)",
        "aap_inference_routing_v1._video_execution_result(jsonb, text)",
        "aap_inference_routing_v1._video_execution_no_scope_rejection(uuid, uuid, text)",
        "aap_inference_routing_v1._video_execution_permit_scope()",
        "aap_inference_routing_v1._video_execution_attempt_gate()",
        "aap_inference_routing_v1._video_execution_heartbeat_gate()",
        "aap_jobs_v1.video_execution_step(text, text)",
        "aap_jobs_v1._video_execution_route_step(text, text)",
        "aap_jobs_v1._video_execution_scope_is_current(uuid, boolean)",
        "aap_jobs_v1._video_execution_claim_view(uuid, uuid)",
        "aap_jobs_v1._video_execution_secret()",
        "aap_jobs_v1._video_execution_realm()",
        "aap_jobs_v1._video_execution_exact_keys(jsonb, text[])",
        "aap_jobs_v1._video_execution_uuid(jsonb)",
        "aap_jobs_v1._video_execution_reply(jsonb, text)",
    ):
        _execute(f"DROP FUNCTION {signature} RESTRICT")
    _execute(
        "ALTER TABLE aap_inference_routing_v1.command_receipts "
        "DROP CONSTRAINT command_receipts_operation_check RESTRICT"
    )
    _execute(
        """ALTER TABLE aap_inference_routing_v1.command_receipts
           ADD CONSTRAINT command_receipts_operation_check CHECK (
             operation IN ('acquire', 'begin', 'settle', 'heartbeat', 'close',
                           'scope_status', 'reconcile', 'prepare_control', 'finish_control',
                           'begin_control', 'settle_control')
           )"""
    )
    for table in (
        "aap_inference_routing_v1.video_execution_scope_permits",
        "aap_jobs_v1.video_execution_route_intents",
        "aap_jobs_v1.video_executions",
        "aap_jobs_v1.video_execution_authority",
    ):
        _execute(f"DROP TABLE {table} RESTRICT")
