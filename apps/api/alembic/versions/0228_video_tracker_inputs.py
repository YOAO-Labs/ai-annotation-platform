"""Retain the source scope and exact video input for tracker jobs.

Revision ID: bo0228
Revises: bo0227
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0228"
down_revision = "bo0227"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_UPGRADE_ARTIFACTS = ("0228_video_tracker_inputs.sql",)
_STATEMENT_SEPARATOR = "-- aap-statement-break"

_REPLACED_FUNCTIONS = {
    "aap_assets_v1.claim_accepted_video_input(uuid,uuid,integer)": (
        "0226_video_input_claim_authority.sql"
    ),
    "aap_assets_v1.sweep_accepted_video_inputs(integer)": (
        "0225_video_inputs_retention.sql"
    ),
}

_NEW_INDEXES = (
    (
        "ix_annotation_video_tracker_input_sources_input",
        "annotation_video_tracker_input_sources",
    ),
    ("ix_video_tracker_input_scopes_task", "video_tracker_input_scopes"),
)
_NEW_TABLES = (
    "annotation_video_tracker_input_sources",
    "video_tracker_input_scopes",
)

# Old tracker jobs have no captured source proof. Installing the new protocol
# while one is running would strand it; a populated candidate must first be
# accepted or discarded through the old owner. An empty candidate is not
# reviewable, and a cancelled tracking candidate ages out after the same
# 24-hour window used by the control protocol. Unknown/malformed states fail
# closed while they could still represent a reviewable candidate.
_LEGACY_TRACKER_RESPONSIBILITY_COUNT = """
WITH legacy_jobs AS (
    SELECT job.*,
           CASE
               WHEN job.staged_result IS NULL THEN false
               WHEN pg_catalog.jsonb_typeof(job.staged_result) = 'object' THEN
                   CASE
                       WHEN job.staged_result = '{}'::jsonb THEN false
                       WHEN pg_catalog.jsonb_typeof(
                           job.staged_result -> 'results'
                       ) = 'array' THEN
                           pg_catalog.jsonb_array_length(
                               job.staged_result -> 'results'
                           ) > 0
                       ELSE true
                   END
               WHEN pg_catalog.jsonb_typeof(job.staged_result) = 'array' THEN
                   job.staged_result <> '[]'::jsonb
               ELSE true
           END AS staged_candidate_or_ambiguity
    FROM public.video_tracker_jobs AS job
)
SELECT count(*)
FROM legacy_jobs AS job
WHERE job.status IN ('queued', 'running')
   OR (
       job.status IN ('pending_review', 'partially_reviewed')
       AND (job.staged_result IS NULL OR job.staged_candidate_or_ambiguity)
   )
   OR (
       job.job_kind = 'tracking'
       AND job.status = 'cancelled'
       AND job.created_at + interval '24 hours' > pg_catalog.clock_timestamp()
       AND job.staged_candidate_or_ambiguity
   )
   OR job.status NOT IN (
       'queued', 'running', 'pending_review', 'partially_reviewed',
       'cancelled', 'completed', 'accepted', 'discarded', 'failed'
   )
"""

_DOWNGRADE_GUARDS = {
    "tracker_scopes": "SELECT count(*) FROM public.video_tracker_input_scopes",
    "annotation_pins": (
        "SELECT count(*) FROM public.annotation_video_tracker_input_sources"
    ),
    "tracker_inputs": (
        "SELECT count(*) FROM public.asset_accepted_video_inputs "
        "WHERE input_owner = 'tracker'"
    ),
    "tracker_garbage": """
        SELECT count(*)
        FROM public.asset_accepted_input_garbage AS garbage
        WHERE EXISTS (
            SELECT 1
            FROM public.asset_accepted_video_inputs AS input
            WHERE input.input_owner = 'tracker'
              AND pg_catalog.left(
                    garbage.prefix,
                    pg_catalog.length(
                        'accepted-video-inputs/' || input.input_id::text || '/'
                    )
                  ) = 'accepted-video-inputs/' || input.input_id::text || '/'
        )
    """,
    "outstanding_tracker_jobs": _LEGACY_TRACKER_RESPONSIBILITY_COUNT,
}


def _statements(filename: str) -> list[str]:
    return [
        statement.strip()
        for statement in (_SQL_DIR / filename)
        .read_text(encoding="utf-8")
        .split(_STATEMENT_SEPARATOR)
        if statement.strip()
    ]


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def _count(bind, query: str) -> int:
    return int(bind.execute(sa.text(query)).scalar_one())


def _function_signature(statement: str) -> str | None:
    match = re.search(
        r"(?is)\bREVOKE\s+ALL\s+ON\s+FUNCTION\s+(.+?)\s+FROM\s+PUBLIC\s*;\s*$",
        statement,
    )
    return " ".join(match.group(1).split()) if match else None


def _function_name(signature: str) -> str:
    return signature.split("(", 1)[0].strip().lower()


def _canonical_signature(signature: str) -> str:
    return "".join(signature.split()).lower()


def _new_function_signatures() -> list[str]:
    replaced = {_function_name(signature) for signature in _REPLACED_FUNCTIONS}
    signatures: list[str] = []
    for filename in reversed(_UPGRADE_ARTIFACTS):
        for statement in reversed(_statements(filename)):
            signature = _function_signature(statement)
            if signature and _function_name(signature) not in replaced:
                signatures.append(signature)
    return signatures


def _restore_replaced_functions() -> list[str]:
    restores: list[str] = []
    for signature, filename in _REPLACED_FUNCTIONS.items():
        function_name = _function_name(signature)
        statements = _statements(filename)
        definition = next(
            (
                statement
                for statement in statements
                if re.search(
                    rf"(?is)\bCREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+"
                    rf"{re.escape(function_name)}\s*\(",
                    statement,
                )
            ),
            None,
        )
        if definition is None:
            raise RuntimeError(f"Cannot locate frozen definition for {signature}")
        definition = re.sub(
            rf"(?is)\bCREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+"
            rf"{re.escape(function_name)}\s*\(",
            f"CREATE OR REPLACE FUNCTION {function_name}(",
            definition,
            count=1,
        )
        revoke = next(
            (
                statement
                for statement in statements
                if (candidate := _function_signature(statement))
                and _canonical_signature(candidate) == _canonical_signature(signature)
            ),
            None,
        )
        if revoke is None:
            raise RuntimeError(f"Cannot locate frozen ACL for {signature}")
        restores.extend((definition, revoke))
    return restores


def _run_artifact(filename: str) -> None:
    for statement in _statements(filename):
        _execute(statement)


def upgrade() -> None:
    legacy_jobs = _count(op.get_bind(), _LEGACY_TRACKER_RESPONSIBILITY_COUNT)
    if legacy_jobs:
        raise RuntimeError(
            "Cannot install retained video tracker inputs while legacy tracker work, "
            "reviewable candidates, or ambiguous candidate states remain: "
            f"{legacy_jobs}. Drain tracking/correction jobs and accept or discard "
            "reviewable candidates before upgrading."
        )
    for filename in _UPGRADE_ARTIFACTS:
        _run_artifact(filename)


def downgrade() -> None:
    bind = op.get_bind()
    retained = {name: _count(bind, query) for name, query in _DOWNGRADE_GUARDS.items()}
    nonempty = {name: count for name, count in retained.items() if count}
    if nonempty:
        detail = ", ".join(f"{name}={count}" for name, count in nonempty.items())
        raise RuntimeError(
            "Cannot remove retained video tracker scopes, evidence or cleanup "
            f"responsibility: {detail}."
        )

    # CREATE OR REPLACE preserves the old functions' owner and role ACLs while
    # restoring their exact frozen pre-tracker bodies before shared columns go.
    for statement in _restore_replaced_functions():
        _execute(statement)

    for name, table in (
        (
            "trg_annotation_video_tracker_input_source_immutable",
            "annotation_video_tracker_input_sources",
        ),
        (
            "trg_propagate_annotation_video_tracker_input_source",
            "annotation_video_tracker_input_sources",
        ),
        ("trg_inherit_annotation_video_tracker_input_sources", "annotations"),
    ):
        _execute(f"DROP TRIGGER IF EXISTS {name} ON public.{table} RESTRICT")

    # Drop all new helpers before their row types. Some signatures use the
    # composite row type of asset_accepted_video_inputs, so they must go before
    # the tracker-owner columns are removed.
    for signature in _new_function_signatures():
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")

    for index_name, table_name in _NEW_INDEXES:
        op.drop_index(index_name, table_name=table_name, schema="public")
    for table_name in _NEW_TABLES:
        op.drop_table(table_name, schema="public")

    op.drop_index(
        "ix_asset_accepted_video_inputs_owner_claim",
        table_name="asset_accepted_video_inputs",
        schema="public",
    )
    op.drop_constraint(
        "ck_asset_accepted_video_input_owner",
        "asset_accepted_video_inputs",
        schema="public",
        type_="check",
    )
    op.drop_constraint(
        "asset_accepted_video_inputs_input_owner_check",
        "asset_accepted_video_inputs",
        schema="public",
        type_="check",
    )
    op.drop_column(
        "asset_accepted_video_inputs",
        "tracker_job_id",
        schema="public",
    )
    op.drop_column(
        "asset_accepted_video_inputs",
        "input_owner",
        schema="public",
    )
