"""Bind frozen image exports to their Dataset scope before Job admission."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0444"
down_revision = "bo0443"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def _execute(name: str) -> None:
    source = (_SQL_DIR / name).read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def upgrade() -> None:
    _execute("0444_image_export_scope.sql")
    _execute("0444_image_export_dataset_scope.sql")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.image_export_scopes) THEN "
        "RAISE EXCEPTION 'image export scope bindings prevent downgrade'; "
        "END IF; END $$"
    )
    _execute("0443_fixture_dataset_retired_jobs.sql")
    op.execute("DROP FUNCTION aap_jobs_v1.fixture_job_dataset_scope(uuid,text,jsonb,uuid,uuid)")
    op.execute("DROP TRIGGER image_export_scope_complete ON public.image_export_scopes")
    op.execute("DROP TRIGGER image_export_fixture_dataset_birth ON yoao_fixture_v1.fixture_run_resources")
    op.execute("DROP FUNCTION public.guard_image_export_fixture_dataset_birth()")
    op.execute("DROP FUNCTION public.check_image_export_scope_committed()")
    op.execute("DROP TRIGGER image_export_job_binding ON public.async_jobs")
    op.execute("DROP TRIGGER aaa_image_export_job_birth ON public.async_jobs")
    op.execute("DROP TRIGGER image_export_bundle_binding ON public.export_media_bundles")
    op.execute("DROP TRIGGER image_export_scope_transition ON public.image_export_scopes")
    op.execute("DROP TRIGGER image_export_scope_immutable ON public.image_export_scopes")
    op.execute("DROP FUNCTION public.guard_image_export_job_birth()")
    op.execute("DROP FUNCTION public.guard_image_export_scope_transition()")
    op.execute("DROP FUNCTION public.guard_image_export_scope()")
    op.execute("DROP FUNCTION public.image_export_scope_matches(uuid,uuid)")
    op.execute("DROP FUNCTION public.prepare_image_export_scope(uuid,uuid,uuid,uuid,uuid[])")
    op.execute("DROP FUNCTION public.image_export_snapshot_datasets(jsonb)")
    op.execute("DROP TABLE public.image_export_scopes")
