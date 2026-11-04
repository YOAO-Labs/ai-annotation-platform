"""Record isolated fixture run ownership and unsettled admissions."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0432"
down_revision = "bo0431"
branch_labels = depends_on = None
_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def _install_sql(name: str) -> None:
    source = (_SQL_DIR / name).read_text(encoding="utf-8")
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def upgrade() -> None:
    _install_sql("0432_fixture_run_ownership_schema.sql")
    _install_sql("0432_labeling_fixture_retry_guard.sql")
    _install_sql("0432_jobs_fixture_guard.sql")
    _install_sql("0432_jobs_fixture_dataset_guard.sql")
    _install_sql("0432_fixture_jobs_anchor.sql")
    _install_sql("0432_fixture_dataset_jobs_anchor.sql")
    _install_sql("0432_fixture_dataset_identity.sql")
    _install_sql("0432_assets_fixture_auxiliary.sql")
    _install_sql("0432_assets_screenshot_facts.sql")
    _install_sql("0432_identity_fixture_invitations.sql")
    _install_sql("0432_identity_invitation_command.sql")
    _install_sql("0432_labeling_screenshot_operations.sql")
    _install_sql("0432_jobs_fixture_presentation.sql")
    for name in (
        "0432_assets_fixture_source_guard.sql",
        "0432_assets_fixture_reclaim_guard.sql",
        "0432_assets_scale_fixture_guard.sql",
        "0432_labeling_original_read.sql",
        "0432_assets_media_source_read.sql",
        "0432_assets_media_source_admission.sql",
        "0432_assets_spatial_original_admission.sql",
        "0432_assets_media_source_authority.sql",
        "0432_assets_images_source_authority.sql",
        "0432_assets_inputs_source_authority.sql",
        "0432_assets_video_inputs_source_authority.sql",
        "0432_assets_video_inputs_control_source.sql",
        "0432_assets_tracker_control_source.sql",
    ):
        _install_sql(name)


def downgrade() -> None:
    raise RuntimeError("Cannot discard fixture ownership and unsettled I/O fences")
