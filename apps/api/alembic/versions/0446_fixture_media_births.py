"""Fence fixture media responsibility at its real owner birth."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0446"
down_revision = "bo0445"
branch_labels = depends_on = None


def upgrade() -> None:
    sql_dir = Path(__file__).resolve().parents[1] / "sql"
    for filename in (
        "0446_fixture_media_births.sql",
        "0446_fixture_original_readers.sql",
        "0446_fixture_independent_source.sql",
        "0446_media_producer_binding.sql",
        "0446_fixture_independent_birth.sql",
        "0446_fixture_native_source_ownership.sql",
        "0446_fixture_native_media_jobs.sql",
        "0446_fixture_media_references.sql",
        "0446_fixture_media_jobs.sql",
        "0446_fixture_media_execution.sql",
        "0446_fixture_native_media_admissions.sql",
        "0446_fixture_native_media_sources.sql",
        "0446_fixture_native_media_outputs.sql",
        "0446_fixture_native_target_fence.sql",
        "0446_fixture_native_target_references.sql",
        "0446_fixture_native_media_execution.sql",
        "0446_fixture_native_media_scan.sql",
        "0446_fixture_native_media_scans.sql",
        "0446_fixture_native_media_backfill_source.sql",
        "0446_fixture_native_media_claims.sql",
        "0446_fixture_native_media_reads.sql",
        "0446_fixture_native_runtime_ready.sql",
        "0446_fixture_native_media_resolver.sql",
        "0446_fixture_native_media_garbage.sql",
        "0446_fixture_media_children.sql",
        "0446_fixture_media_pyramid_attempts.sql",
        "0446_fixture_native_pyramid.sql",
        "0446_fixture_media_video_attempts.sql",
        "0446_fixture_media_dispatch.sql",
        "0446_fixture_media_versions.sql",
        "0446_fixture_independent_execution.sql",
        "0446_fixture_legacy_birth_admission.sql",
        "0446_fixture_screenshot_producer.sql",
        "0446_fixture_independent_pyramids.sql",
        "0446_fixture_independent_video.sql",
        "0446_fixture_screenshot_output_receipt.sql",
        "0446_media_native_stages.sql",
        "0446_fixture_media_permit_fence.sql",
        "0446_fixture_media_retirement.sql",
        "0446_fixture_independent_retirement.sql",
        "0446_fixture_native_media_retirement.sql",
        "0446_fixture_native_job_retirement.sql",
        "0446_fixture_media_output_permits.sql",
        "0446_fixture_media_version_retirement.sql",
        "0446_fixture_media_output_inventory.sql",
        "0446_fixture_media_output_fence.sql",
        "0446_fixture_media_no_birth.sql",
        "0446_fixture_media_retired.sql",
        "0446_fixture_media_jobs_retired.sql",
        "0446_fixture_native_target_retirement.sql",
        "0446_fixture_video_anchors.sql",
        "0446_fixture_native_target_ack.sql",
        "0446_fixture_media_dataset_guards.sql",
    ):
        for statement in (sql_dir / filename).read_text(encoding="utf-8").split(
            "-- aap-statement-break"
        ):
            if statement.strip():
                op.execute(
                    sa.text(statement.strip().replace(":", r"\:")).execution_options(
                        no_parameters=True
                    )
                )


def downgrade() -> None:
    raise RuntimeError("Cannot discard fixture media birth evidence")
