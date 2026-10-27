"""Install the independent Assets image media owner.

Revision ID: bo0220
Revises: bo0219
"""

from __future__ import annotations

from pathlib import Path
import re

from alembic import op
import sqlalchemy as sa

revision = "bo0220"
down_revision = "bo0219"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_UPGRADE_ARTIFACTS = (
    "0220_image_authorities.sql",
    "0220_assets_images_state.sql",
    "0220_assets_images_control.sql",
    "0220_assets_images_execution.sql",
    "0220_assets_images_garbage.sql",
    "0220_image_cleanup.sql",
    "0220_image_sources.sql",
)

# These functions are replaced so existing callers retain their names and
# signatures. Downgrade restores the last pre-0220 definitions verbatim.
_REPLACED_FUNCTIONS = {
    "aap_assets_v1._queue_pyramid_deletions": (
        "0209_dataset_source_deletion.sql",
        "0209_dataset_source_deletion.sql",
    ),
    "aap_assets_v1.prepare_dataset_unlink_cleanup": (
        "0209_dataset_source_deletion.sql",
        "0203_dataset_unlink_assets.sql",
    ),
    "aap_assets_v1._delete_dataset_source_records": (
        "0209_dataset_source_deletion.sql",
        "0209_dataset_source_deletion.sql",
    ),
    "aap_assets_v1.delete_project_owned_records": (
        "0198_project_owned_cleanup_contract.sql",
        "0198_project_owned_cleanup_contract.sql",
    ),
    "aap_assets_v1.pending_storage_deletions_v2": (
        "0210_asset_storage_cleanup.sql",
        "0210_asset_storage_cleanup.sql",
    ),
    "aap_assets_v1.complete_storage_deletion": (
        "0209_dataset_source_deletion.sql",
        "0203_dataset_unlink_assets.sql",
    ),
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


def upgrade() -> None:
    for filename in _UPGRADE_ARTIFACTS:
        _run_artifact(filename)


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
            if signature and _function_name(signature) not in _REPLACED_FUNCTIONS:
                signatures.append(signature)
    return signatures


def _trigger_drop_statements() -> list[str]:
    triggers: list[str] = []
    pattern = re.compile(
        r"(?is)^CREATE\s+(?:CONSTRAINT\s+)?TRIGGER\s+([a-z_][a-z0-9_]*)\s+"
        r".*?\s+ON\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\b"
    )
    for filename in _UPGRADE_ARTIFACTS:
        for statement in _statements(filename):
            match = pattern.match(statement)
            if match:
                trigger_name, relation = match.groups()
                triggers.append(f"DROP TRIGGER IF EXISTS {trigger_name} ON {relation}")
    return list(reversed(triggers))


def _restore_statements() -> list[str]:
    restores = []
    for function_name, (filename, revoke_filename) in _REPLACED_FUNCTIONS.items():
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
                for statement in _statements(revoke_filename)
                if (signature := _function_signature(statement))
                and _function_name(signature) == function_name
            ),
            None,
        )
        if revoke is None:
            raise RuntimeError(f"Cannot locate frozen revoke for {function_name}")
        restores.extend((definition, revoke))
    return restores


def downgrade() -> None:
    bind = op.get_bind()
    retained = {
        "jobs": _count(bind, "SELECT count(*) FROM public.asset_image_jobs"),
        "operations": _count(
            bind, "SELECT count(*) FROM public.asset_image_operations"
        ),
        "garbage": _count(bind, "SELECT count(*) FROM public.asset_image_garbage"),
        "retained_cleanup": _count(
            bind,
            "SELECT count(*) FROM public.asset_storage_deletions WHERE retained",
        ),
        "task_source_state": _count(
            bind,
            "SELECT count(*) FROM public.tasks "
            "WHERE media_source_generation <> 1 "
            "OR media_object_identity IS NOT NULL OR media_source_drifted",
        ),
        "pyramid_generation_state": _count(
            bind,
            "SELECT count(*) FROM public.image_pyramid_generations "
            "WHERE job_id IS NOT NULL OR job_generation IS NOT NULL "
            "OR source_generation IS NOT NULL OR source_snapshot IS NOT NULL "
            "OR output_prefix IS NOT NULL OR manifest IS NOT NULL OR policy IS NOT NULL",
        ),
        "orphaned_pyramid_assets": _count(
            bind,
            "SELECT count(*) FROM public.image_pyramid_assets AS asset "
            "LEFT JOIN public.dataset_items AS item ON item.id = asset.dataset_item_id "
            "LEFT JOIN public.tasks AS task ON task.id = asset.task_id "
            "WHERE (asset.dataset_item_id IS NOT NULL AND item.id IS NULL) "
            "OR (asset.task_id IS NOT NULL AND task.id IS NULL)",
        ),
    }
    nonempty = {name: count for name, count in retained.items() if count}
    if nonempty:
        detail = ", ".join(f"{name}={count}" for name, count in nonempty.items())
        raise RuntimeError(
            "Cannot downgrade retained image media work, source state, or "
            f"cleanup responsibility: {detail}."
        )

    # Triggers are discovered from the same install artifacts so a new deferred
    # handoff guard cannot be left behind when its SQL file evolves.
    for statement in _trigger_drop_statements():
        op.execute(statement)

    # The garbage table has a CHECK dependency on _image_garbage_prefix_valid.
    op.drop_table("asset_image_garbage", schema="public")

    for signature in _new_function_signatures():
        op.execute(f"DROP FUNCTION {signature} RESTRICT")

    op.drop_table("asset_image_operations", schema="public")
    op.drop_table("asset_image_jobs", schema="public")

    op.execute(
        "ALTER TABLE public.image_pyramid_generations "
        "DROP COLUMN policy, DROP COLUMN manifest, DROP COLUMN output_prefix, "
        "DROP COLUMN source_snapshot, DROP COLUMN source_generation, "
        "DROP COLUMN job_generation, DROP COLUMN job_id"
    )
    op.execute(
        "ALTER TABLE public.image_pyramid_assets "
        "ADD CONSTRAINT image_pyramid_assets_dataset_item_id_fkey "
        "FOREIGN KEY (dataset_item_id) REFERENCES public.dataset_items(id) ON DELETE CASCADE, "
        "ADD CONSTRAINT image_pyramid_assets_task_id_fkey "
        "FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE public.tasks DROP CONSTRAINT ck_tasks_media_object_identity, "
        "DROP CONSTRAINT ck_tasks_media_source_generation, "
        "DROP COLUMN media_source_drifted, DROP COLUMN media_object_identity, "
        "DROP COLUMN media_source_generation"
    )

    op.execute("DROP INDEX public.ix_asset_storage_deletions_pending_bucket_created")
    op.execute("DROP INDEX public.ix_asset_storage_deletions_pending_created")
    op.execute("ALTER TABLE public.asset_storage_deletions DROP COLUMN retained")
    op.execute(
        "CREATE INDEX ix_asset_storage_deletions_pending_bucket_created "
        "ON public.asset_storage_deletions(bucket, created_at, operation_id, asset_id) "
        "INCLUDE (not_before) WHERE completed_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_asset_storage_deletions_pending_created "
        "ON public.asset_storage_deletions(created_at, operation_id, asset_id) "
        "WHERE completed_at IS NULL"
    )

    for statement in _restore_statements():
        op.execute(sa.text(statement.replace(":", r"\:")))
