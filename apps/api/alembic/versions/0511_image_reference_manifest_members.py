"""Seal authenticated directory references without a managed image Input."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0511"
down_revision = "bo0510"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_MEMBERS = "public.asset_accepted_image_input_manifest_members"
_STATE = "ck_asset_accepted_image_input_manifest_member_state"
_OLD_STAGE_STATE = "v_manifest ->> 'state' NOT IN ('ready','failed')"
_REFERENCE_STAGE = """v_manifest ->> 'state' NOT IN ('ready','failed','reference')
                   OR (v_manifest ->> 'state' = 'reference' AND (
                       (v_manifest_item <> 'null'::jsonb AND
                        v_manifest_item -> 'accepted_input_id' IS DISTINCT FROM 'null'::jsonb)
                       OR v_input IS DISTINCT FROM 'null'::jsonb
                       OR v_manifest -> 'source' IS DISTINCT FROM 'null'::jsonb
                       OR v_manifest -> 'error' IS DISTINCT FROM 'null'::jsonb
                       OR NOT yoao_assets_v1.is_sealed_image_reference_member(
                           v_execution.job_id, v_execution.actor_id, v_execution.project_id, v_task,
                           v_manifest ->> 'selection_digest', (v_manifest ->> 'dataset_item_id')::uuid,
                           v_manifest ->> 'file_path', (v_manifest ->> 'task_version')::integer,
                           (v_manifest ->> 'task_source_generation')::bigint
                       )
                   ))"""


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def _stage_reference_state(before: str, after: str) -> None:
    definition = op.get_bind().scalar(
        sa.text(
            "SELECT pg_catalog.pg_get_functiondef("
            "'yoao_jobs_v1.stage_batch_result_members(jsonb)'::regprocedure)"
        )
    )
    if definition.count(before) != 1:
        raise RuntimeError("Unexpected batch result selection contract")
    _execute(definition.replace(before, after, 1))


def upgrade() -> None:
    source = (_SQL / "0511_image_reference_manifest_members.sql").read_text(
        encoding="utf-8"
    )
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            _execute(statement)
    _stage_reference_state(_OLD_STAGE_STATE, _REFERENCE_STAGE)


def downgrade() -> None:
    _execute(f"LOCK TABLE {_MEMBERS} IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text(f"SELECT EXISTS (SELECT FROM {_MEMBERS} WHERE state = 'reference')")
    ):
        raise RuntimeError("Cannot downgrade retained reference manifest members")
    _stage_reference_state(_REFERENCE_STAGE, _OLD_STAGE_STATE)

    _execute("""
        DO $acl$
        DECLARE
            assets_owner regrole := (
                SELECT proowner FROM pg_catalog.pg_proc
                WHERE oid = 'yoao_assets_v1.capture_accepted_image_input_manifest(uuid,uuid,uuid,jsonb)'::regprocedure
            );
            jobs_owner regrole := (
                SELECT proowner FROM pg_catalog.pg_proc
                WHERE oid = 'yoao_jobs_v1.read_job_execution_snapshot(uuid)'::regprocedure
            );
        BEGIN
            IF assets_owner <> jobs_owner THEN
                EXECUTE pg_catalog.format(
                    'REVOKE EXECUTE ON FUNCTION yoao_jobs_v1.read_job_execution_snapshot(uuid) FROM %s',
                    assets_owner
                );
            END IF;
        END
        $acl$;
    """)

    control = (_SQL / "0222_accepted_input_manifests_control.sql").read_text(
        encoding="utf-8"
    )
    for name in (
        "capture_accepted_image_input_manifest",
        "seal_accepted_image_input_manifest",
    ):
        prefix = f"CREATE FUNCTION aap_assets_v1.{name}("
        if control.count(prefix) != 1:
            raise RuntimeError(f"Expected one frozen definition of {name}")
        start = control.index(prefix)
        end = control.index("$function$;", start) + len("$function$;")
        _execute(
            control[start:end]
            .replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
            .replace("aap_assets_v1.", "yoao_assets_v1.")
            .replace("aap_labeling_v1.", "yoao_labeling_v1.")
        )

    _execute(
        "DROP FUNCTION yoao_assets_v1._image_manifest_reference_matches(jsonb,jsonb,jsonb)"
    )
    _execute(
        "DROP FUNCTION yoao_assets_v1.is_sealed_image_reference_member("
        "uuid,uuid,uuid,uuid,text,uuid,text,integer,bigint)"
    )
    state = (_SQL / "0222_accepted_input_manifests_state.sql").read_text(
        encoding="utf-8"
    )
    start = state.index(f"CONSTRAINT {_STATE} CHECK (")
    end = state.index("\n);", start)
    _execute(f"ALTER TABLE {_MEMBERS} DROP CONSTRAINT {_STATE}, ADD {state[start:end]}")
