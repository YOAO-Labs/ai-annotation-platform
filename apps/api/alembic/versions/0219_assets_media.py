"""Install the independent Assets video media state and execution boundary.

Revision ID: bo0219
Revises: bo0218
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0219"
down_revision = "bo0218"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_UPGRADE_ARTIFACTS = (
    "0219_media_authorities.sql",
    "0219_assets_media_state.sql",
    "0219_assets_media_control.sql",
    "0219_assets_media_execution.sql",
)


def _run_artifact(filename: str) -> None:
    artifact = _SQL_DIR / filename
    for statement in artifact.read_text(encoding="utf-8").split(_STATEMENT_SEPARATOR):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def upgrade() -> None:
    for filename in _UPGRADE_ARTIFACTS:
        _run_artifact(filename)


def downgrade() -> None:
    bind = op.get_bind()
    counts = {
        "jobs": bind.execute(
            sa.text("SELECT count(*) FROM public.asset_media_jobs")
        ).scalar_one(),
        "operations": bind.execute(
            sa.text("SELECT count(*) FROM public.asset_media_operations")
        ).scalar_one(),
        "garbage": bind.execute(
            sa.text("SELECT count(*) FROM public.asset_media_garbage")
        ).scalar_one(),
        "staging": bind.execute(
            sa.text("SELECT count(*) FROM public.asset_media_timetable_staging")
        ).scalar_one(),
        "items": bind.execute(
            sa.text(
                "SELECT count(*) FROM public.dataset_items "
                "WHERE media_source_generation <> 1 "
                "OR media_timetable_revision <> 0 "
                "OR media_materialized_generation <> 0 "
                "OR media_object_identity IS NOT NULL OR media_source_drifted"
            )
        ).scalar_one(),
        "frames": bind.execute(
            sa.text(
                "SELECT count(*) FROM public.video_frame_cache "
                "WHERE source_generation <> 0 OR timetable_revision <> 0"
            )
        ).scalar_one(),
        "chunks": bind.execute(
            sa.text(
                "SELECT count(*) FROM public.video_chunks "
                "WHERE source_generation <> 0 OR timetable_revision <> 0"
            )
        ).scalar_one(),
    }
    retained = {name: count for name, count in counts.items() if count}
    if retained:
        detail = ", ".join(f"{name}={count}" for name, count in retained.items())
        raise RuntimeError(
            "Cannot downgrade retained video media work/receipts or source state: "
            f"{detail}."
        )

    for statement in (
        "DROP TRIGGER trg_dataset_items_media_source_generation ON public.dataset_items",
        "DROP TRIGGER trg_dataset_items_media_source_deleted ON public.dataset_items",
        "DROP TRIGGER trg_asset_media_operations_immutable ON public.asset_media_operations",
        "DROP TRIGGER trg_asset_media_garbage_immutable_scope ON public.asset_media_garbage",
    ):
        op.execute(statement)

    for signature in _DOWNGRADE_SIGNATURES:
        op.execute(f"DROP FUNCTION {signature} RESTRICT")

    op.drop_table("asset_media_timetable_staging", schema="public")
    op.drop_table("asset_media_operations", schema="public")
    op.drop_table("asset_media_garbage", schema="public")
    op.drop_table("asset_media_jobs", schema="public")
    op.drop_column("video_chunks", "timetable_revision", schema="public")
    op.drop_column("video_chunks", "source_generation", schema="public")
    op.drop_column("video_frame_cache", "timetable_revision", schema="public")
    op.drop_column("video_frame_cache", "source_generation", schema="public")
    op.drop_column("dataset_items", "media_source_drifted", schema="public")
    op.drop_column("dataset_items", "media_object_identity", schema="public")
    op.drop_column("dataset_items", "media_materialized_generation", schema="public")
    op.drop_column("dataset_items", "media_timetable_revision", schema="public")
    op.drop_column("dataset_items", "media_source_generation", schema="public")


_DOWNGRADE_SIGNATURES: tuple[str, ...] = (
    "aap_assets_v1.advance_video_backfill(uuid, bigint, uuid, integer)",
    "aap_assets_v1.read_video_media_usage()",
    "aap_assets_v1.complete_video_media_garbage(uuid, uuid)",
    "aap_assets_v1.claim_video_media_garbage(uuid, integer, integer)",
    "aap_assets_v1.expire_video_media(integer, integer, integer)",
    "aap_assets_v1.fail_video_media(uuid, bigint, uuid, text, text)",
    "aap_assets_v1.finish_video_media(uuid, bigint, uuid, jsonb)",
    "aap_assets_v1.stage_video_timetable(uuid, bigint, uuid, jsonb)",
    "aap_assets_v1.heartbeat_video_media(uuid, bigint, uuid, integer)",
    "aap_assets_v1.claim_video_media(uuid, uuid, integer, text[])",
    "aap_assets_v1._media_requeue_timetable_dependents(uuid, bigint, bigint)",
    "aap_assets_v1._media_queue_key(text, text)",
    "aap_assets_v1._media_queue_prefix(text, text)",
    "aap_assets_v1._media_artifact_valid(jsonb, text)",
    "aap_assets_v1._media_object_has_only(jsonb, text[])",
    "aap_assets_v1._media_worker_outcome(uuid, bigint, text)",
    "aap_assets_v1.read_internal_video_frame(uuid, integer, integer, text)",
    "aap_assets_v1.scan_video_backfill(jsonb, uuid, integer)",
    "aap_assets_v1.read_video_media_totals(uuid[])",
    "aap_assets_v1.read_video_failures(uuid, integer, bigint)",
    "aap_assets_v1.retry_video_media(uuid, uuid, jsonb)",
    "aap_assets_v1.cancel_video_media(uuid, uuid, uuid)",
    "aap_assets_v1.ensure_video_media(uuid, uuid, uuid, jsonb)",
    "aap_assets_v1.read_video_samples(uuid, uuid, uuid, integer, integer, integer)",
    "aap_assets_v1.read_video_timetable(uuid, uuid, uuid, integer, integer, integer, integer)",
    "aap_assets_v1.read_video_work(uuid, uuid)",
    "aap_assets_v1.schedule_video_media(uuid, uuid, jsonb, boolean)",
    "aap_assets_v1.request_video_media(uuid, uuid, uuid, jsonb, boolean)",
    "aap_assets_v1.observe_media_source(uuid, uuid, uuid, text, jsonb)",
    "aap_assets_v1.resolve_internal_media_source(uuid, text, jsonb)",
    "aap_assets_v1.read_media_preferences()",
    "aap_assets_v1.read_media_summaries(uuid[])",
    "aap_assets_v1.read_media_source(uuid, uuid, uuid)",
    "aap_assets_v1.invalidate_video_media(uuid, uuid, uuid, jsonb)",
    "aap_assets_v1._media_manager_item(uuid, uuid, boolean)",
    "aap_assets_v1._media_read_visible_video_item(uuid, uuid)",
    "aap_assets_v1._media_lock_task_annotation_write(uuid, uuid, uuid)",
    "aap_assets_v1._media_read_task_access(uuid, uuid, uuid)",
    "aap_assets_v1._media_lock_actor(uuid)",
    "aap_assets_v1._media_read_actor(uuid)",
    "aap_settings_v1.read_video_media_preferences()",
    "aap_labeling_v1.read_media_failure_context(uuid, uuid[])",
    "aap_labeling_v1.read_media_visible_video_item(uuid, uuid)",
    "aap_labeling_v1.lock_media_task_annotation_write(uuid, uuid, uuid)",
    "aap_labeling_v1.read_media_task_access(uuid, uuid, uuid)",
    "aap_workspace_v1.read_media_managed_projects(uuid, uuid[])",
    "aap_workspace_v1.lock_media_project_access(uuid, uuid)",
    "aap_workspace_v1.read_media_project_access(uuid, uuid)",
    "aap_assets_v1._media_backfill_snapshot(jsonb, timestamp with time zone)",
    "aap_assets_v1._media_enqueue(uuid, uuid, uuid, jsonb, boolean)",
    "aap_assets_v1._media_reconcile_source(uuid, text, jsonb, text)",
    "aap_assets_v1._media_object_identity_valid(jsonb)",
    "aap_assets_v1._media_work_spec_valid(jsonb)",
    "aap_assets_v1._media_write_operation(uuid, uuid, text, jsonb, jsonb, jsonb)",
    "aap_assets_v1._media_read_operation(uuid, uuid, text, jsonb, jsonb)",
    "aap_assets_v1._media_operation_lock(uuid)",
    "aap_assets_v1._media_chunk_json(public.video_chunks)",
    "aap_assets_v1._media_frame_json(public.video_frame_cache)",
    "aap_assets_v1._media_work_reference(public.asset_media_jobs)",
    "aap_assets_v1._media_source_deleted()",
    "aap_assets_v1._media_source_generation_changed()",
    "aap_assets_v1._media_source_json(public.dataset_items)",
    "aap_assets_v1._media_source_snapshot(public.dataset_items)",
    "aap_assets_v1._media_garbage_immutable_scope()",
    "aap_assets_v1._media_operation_immutable()",
    "aap_assets_v1._media_reply(jsonb, text)",
)
