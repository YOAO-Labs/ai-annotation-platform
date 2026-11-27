"""Fix SQL key collation, GPU queue FIFO ordering, and dataset job listing."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0518"
down_revision = "bo0517"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_BREAK = "-- aap-statement-break"


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    source = (_SQL / "0518_sql_collation_and_query_fixes.sql").read_text(
        encoding="utf-8"
    )
    for statement in source.split(_BREAK):
        if statement.strip():
            _execute(statement)


def downgrade() -> None:
    for signature in (
        "yoao_jobs_v1.async_job_list_total_v2("
        "uuid, text, text[], text[], uuid, text, boolean, boolean)",
        "yoao_jobs_v1.async_job_list_page_v2("
        "uuid, text, text[], text[], uuid, text, integer, integer, boolean, boolean)",
        "yoao_jobs_v1._async_job_list_rows_v2("
        "uuid, text, text[], text[], uuid, text, boolean, boolean)",
    ):
        _execute(f"DROP FUNCTION IF EXISTS {signature}")

    statements = [
        statement.strip()
        for statement in (_SQL / "0518_sql_collation_and_query_fixes.sql")
        .read_text(encoding="utf-8")
        .split(_BREAK)
        if statement.strip()
    ]
    _execute(
        statements[0].replace(
            'ORDER BY key COLLATE "C"',
            "ORDER BY key",
            1,
        )
    )
    _execute(
        """
CREATE OR REPLACE FUNCTION yoao_learning_v1.training_create_run(
    p_id uuid, p_project uuid, p_creator uuid, p_dataset uuid, p_model uuid, p_study uuid,
    p_trigger uuid, p_request uuid, p_request_digest text, p_name text, p_config jsonb,
    p_config_digest text, p_origin jsonb, p_authorization jsonb
)
RETURNS SETOF yoao_learning_v1._training_run_facts
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_study IS NOT NULL AND p_trigger IS NOT NULL THEN
        RAISE EXCEPTION 'A training run cannot be both a study child and an automatic run'
            USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.training_runs (
        id, project_id, creator_id, dataset_id, initialization_model_id, study_id,
        auto_trigger_id, request_id, request_digest, name, config, config_digest,
        origin, "authorization", status, metrics
    ) VALUES (
        p_id, p_project, p_creator, p_dataset, p_model, p_study,
        p_trigger, p_request, p_request_digest, p_name, p_config, p_config_digest,
        p_origin, p_authorization, 'queued', '{}'::jsonb
    );
    RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_run_facts AS r WHERE r.id = p_id;
END;
$function$;
"""
    )
    _execute(
        """
CREATE OR REPLACE FUNCTION yoao_learning_v1.training_gpu_queue()
RETURNS SETOF yoao_learning_v1._training_run_facts
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT r.* FROM yoao_learning_v1._training_run_facts AS r
WHERE r.status = 'queued' ORDER BY r.created_at LIMIT 100 FOR UPDATE SKIP LOCKED;
$function$;
"""
    )
