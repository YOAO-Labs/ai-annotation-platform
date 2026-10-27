"""Allow Task-scoped image manifests in Scene-mode image projects.

Revision ID: bo0227
Revises: bo0226
"""

from __future__ import annotations

from pathlib import Path
import re

from alembic import op
import sqlalchemy as sa

revision = "bo0227"
down_revision = "bo0226"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_UPGRADE_ARTIFACTS = ("0227_scene_image_input_manifests.sql",)
_FROZEN_CONTROL = "0222_accepted_input_manifests_control.sql"
_CURRENT_SCOPE = "aap_assets_v1._accepted_image_input_manifest_current_scope"
_LEGACY_SCENE_IMAGE_JOB_COUNT = """
SELECT count(*)
FROM public.async_jobs AS job
JOIN public.projects AS project ON project.id = job.project_id
WHERE job.kind = 'batch_predict'
  AND job.status IN ('pending', 'running')
  AND project.data_type = 'image'
  AND project.scene_mode
  AND COALESCE(job.payload ->> 'execution_scope', 'bulk') = 'bulk'
  AND job.payload ->> 'accepted_input_manifest_protocol'
      IS DISTINCT FROM 'aap-assets-input-manifest/v1'
  AND COALESCE(job.payload ->> 'execution_unit', '')
      NOT IN ('frame', 'video', 'scene')
  AND COALESCE(job.payload ->> 'task', '') NOT IN ('text', 'tracker')
  AND COALESCE(job.payload ->> 'task_type', '') NOT IN ('text', 'tracker')
  AND NOT EXISTS (
      SELECT 1
      FROM jsonb_array_elements(
          CASE
              WHEN pg_catalog.jsonb_typeof(job.payload -> 'pipeline_stages') = 'array'
                  THEN job.payload -> 'pipeline_stages'
              ELSE '[]'::jsonb
          END
      ) AS stage
      WHERE stage ->> 'task' IN ('text', 'tracker')
         OR stage #>> '{source,execution_unit}' IN ('frame', 'video', 'scene')
  )
"""
_UNSUPPORTED_RETAINED_MANIFEST_COUNT = """
SELECT count(*)
FROM public.asset_accepted_image_input_manifests AS manifest
LEFT JOIN public.projects AS project ON project.id = manifest.project_id
WHERE project.id IS NULL
   OR project.data_type IS DISTINCT FROM 'image'
   OR project.scene_mode IS DISTINCT FROM false
"""


def _statements(filename: str) -> list[str]:
    return [
        statement.strip()
        for statement in (_SQL_DIR / filename)
        .read_text(encoding="utf-8")
        .split("-- aap-statement-break")
        if statement.strip()
    ]


def _count(bind, query: str) -> int:
    return int(bind.execute(sa.text(query)).scalar_one())


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def _restore_current_scope() -> list[str]:
    statements = _statements(_FROZEN_CONTROL)
    definition = next(
        (
            statement
            for statement in statements
            if re.match(
                rf"(?is)^CREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+{re.escape(_CURRENT_SCOPE)}\s*\(",
                statement,
            )
        ),
        None,
    )
    if definition is None:
        raise RuntimeError(f"Cannot locate frozen definition for {_CURRENT_SCOPE}")
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
            if re.match(
                rf"(?is)^REVOKE\s+ALL\s+ON\s+FUNCTION\s+{re.escape(_CURRENT_SCOPE)}\s*\(",
                statement,
            )
        ),
        None,
    )
    if revoke is None:
        raise RuntimeError(f"Cannot locate frozen revoke for {_CURRENT_SCOPE}")
    return [definition, revoke]


def upgrade() -> None:
    legacy_jobs = _count(op.get_bind(), _LEGACY_SCENE_IMAGE_JOB_COUNT)
    if legacy_jobs:
        raise RuntimeError(
            "Cannot enable Scene-mode image input manifests while active legacy "
            "or ambiguous bulk Jobs remain: "
            f"{legacy_jobs}. Drain or explicitly fail those Scene-image Jobs first."
        )
    for statement in _statements(_UPGRADE_ARTIFACTS[0]):
        _execute(statement)


def downgrade() -> None:
    retained = _count(op.get_bind(), _UNSUPPORTED_RETAINED_MANIFEST_COUNT)
    if retained:
        raise RuntimeError(
            "Cannot restore the image-only helper while retained Scene-mode "
            f"image manifests remain: {retained}."
        )
    for statement in _restore_current_scope():
        _execute(statement)
