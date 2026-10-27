"""Install immutable bulk image input membership and preparation barriers.

Revision ID: bo0222
Revises: bo0221
"""

from __future__ import annotations

from pathlib import Path
import re

from alembic import op
import sqlalchemy as sa

revision = "bo0222"
down_revision = "bo0221"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_UPGRADE_ARTIFACTS = (
    "0222_accepted_input_manifests_state.sql",
    "0222_accepted_input_manifests_authorities.sql",
    "0222_accepted_input_manifests_control.sql",
)
_SCHEMA_ARTIFACTS = ("0222_accepted_input_manifests.schema.json",)
_REPLACED_FUNCTIONS = {
    "aap_assets_v1._accepted_image_input_job_scope": "0221_accepted_inputs_control.sql",
    "aap_assets_v1._accepted_image_input_job_consistency": "0221_accepted_inputs_control.sql",
    "aap_assets_v1.read_accepted_image_input_work": "0221_accepted_inputs_control.sql",
    "aap_assets_v1.lock_accepted_image_input": "0221_accepted_inputs_control.sql",
}
_COMPOSITE_FUNCTIONS = {
    "aap_assets_v1._accepted_image_input_manifest_header",
    "aap_assets_v1._accepted_image_input_manifest_member",
}


def _statements(filename: str) -> list[str]:
    artifact = _SQL_DIR / filename
    return [
        statement.strip()
        for statement in artifact.read_text(encoding="utf-8").split(
            _STATEMENT_SEPARATOR
        )
        if statement.strip()
    ]


def _run_artifact(filename: str) -> None:
    for statement in _statements(filename):
        op.execute(sa.text(statement.replace(":", r"\:")))


def _count(bind, query: str) -> int:
    return int(bind.execute(sa.text(query)).scalar_one())


def _function_signature(statement: str) -> str | None:
    match = re.match(
        r"(?is)^REVOKE\s+ALL\s+ON\s+FUNCTION\s+(.+?)\s+FROM\s+PUBLIC\s*;\s*$",
        statement,
    )
    return " ".join(match.group(1).split()) if match else None


def _function_name(signature: str) -> str:
    return signature.split("(", 1)[0].strip().lower()


def _new_function_signatures() -> list[str]:
    signatures = []
    for filename in reversed(_UPGRADE_ARTIFACTS):
        for statement in reversed(_statements(filename)):
            signature = _function_signature(statement)
            if signature and _function_name(signature) not in (
                set(_REPLACED_FUNCTIONS) | _COMPOSITE_FUNCTIONS
            ):
                signatures.append(signature)
    return signatures


def _restore_statements() -> list[str]:
    restores: list[str] = []
    for function_name, filename in _REPLACED_FUNCTIONS.items():
        statements = _statements(filename)
        definition = next(
            (
                statement
                for statement in statements
                if re.match(
                    rf"(?is)^CREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+{re.escape(function_name)}\s*\(",
                    statement,
                )
            ),
            None,
        )
        if definition is None:
            raise RuntimeError(f"Cannot locate frozen definition for {function_name}")
        definition = re.sub(
            r"(?is)^CREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION",
            "CREATE OR REPLACE FUNCTION",
            definition,
            count=1,
        )
        revoke = next(
            (
                statement
                for statement in statements
                if (signature := _function_signature(statement))
                and _function_name(signature) == function_name
            ),
            None,
        )
        if revoke is None:
            raise RuntimeError(f"Cannot locate frozen revoke for {function_name}")
        restores.extend((definition, revoke))
    return restores


def upgrade() -> None:
    bind = op.get_bind()
    legacy_jobs = _count(
        bind,
        "SELECT count(*) FROM public.async_jobs AS job "
        "JOIN public.projects AS project ON project.id = job.project_id "
        "WHERE job.kind = 'batch_predict' "
        "AND job.status IN ('pending', 'running') "
        "AND project.data_type = 'image' AND NOT project.scene_mode "
        "AND COALESCE(job.payload ->> 'execution_scope', 'bulk') = 'bulk' "
        "AND job.payload ->> 'accepted_input_manifest_protocol' "
        "IS DISTINCT FROM 'aap-assets-input-manifest/v1'",
    )
    if legacy_jobs:
        raise RuntimeError(
            "Cannot install bulk image input manifests while active legacy image "
            f"preannotation Jobs lack frozen input identity: {legacy_jobs}. Drain or "
            "explicitly fail those Jobs first."
        )
    for filename in _UPGRADE_ARTIFACTS:
        _run_artifact(filename)


def downgrade() -> None:
    bind = op.get_bind()
    retained = {
        "manifests": _count(
            bind, "SELECT count(*) FROM public.asset_accepted_image_input_manifests"
        ),
        "members": _count(
            bind,
            "SELECT count(*) FROM public.asset_accepted_image_input_manifest_members",
        ),
    }
    nonempty = {name: count for name, count in retained.items() if count}
    if nonempty:
        detail = ", ".join(f"{name}={count}" for name, count in nonempty.items())
        raise RuntimeError(
            f"Cannot downgrade retained immutable bulk image input manifests: {detail}."
        )

    # Restore the exact frozen 0221 capability bodies before removing
    # their manifest-aware replacements.
    for statement in _restore_statements():
        op.execute(statement)

    # Composite row-type helpers must be dropped before their tables.
    op.execute(
        "DROP FUNCTION IF EXISTS aap_assets_v1._accepted_image_input_manifest_member("
        "public.asset_accepted_image_input_manifest_members,"
        "public.asset_accepted_image_input_manifests) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS aap_assets_v1._accepted_image_input_manifest_header("
        "public.asset_accepted_image_input_manifests) RESTRICT"
    )
    op.drop_table("asset_accepted_image_input_manifest_members", schema="public")
    op.drop_table("asset_accepted_image_input_manifests", schema="public")

    for signature in _new_function_signatures():
        op.execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
