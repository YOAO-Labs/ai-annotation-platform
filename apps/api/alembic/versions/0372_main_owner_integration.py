"""Reconcile main storage and learning contracts with native module owners."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0372"
down_revision = "bo0371"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

_ARTIFACTS = (
    "0371_assets_owner_main_integration.sql",
    "0371_assets_publication_storage_gate.sql",
    "0371_identity_avatar_account_command.sql",
    "0371_notifications_main_integration.sql",
    "0371_project_deletion_storage_retirement.sql",
    "0371_inference_backend_endpoint_auth_revision.sql",
    "0371_inference_routing_auth_rotation.sql",
    "0371_media_owner_item_links.sql",
    "0372_prediction_failure_snapshot.sql",
)

_RESTORE = {
    "aap_assets_v1.create_storage_connection": "0217_assets_connections.sql",
    "aap_assets_v1.update_storage_connection": "0217_assets_connections.sql",
    "aap_assets_v1.delete_storage_connection": "0217_assets_connections.sql",
    "aap_assets_v1._storage_connection_facts": "0217_assets_connections.sql",
    "aap_assets_v1.prepare_dataset_import": "0214_dataset_import_core.sql",
    "aap_assets_v1.record_storage_connection_test": "0217_assets_connections.sql",
    "aap_assets_v1.capture_storage_connection": "0217_assets_connections.sql",
    "aap_identity_accounts_v1.apply_account_command": "0238_identity_accounts.sql",
    "aap_notifications_v1._visible_inbox_rows": "0324_notifications_inbox_query.sql",
    "aap_notifications_v1.read_state_command": "0316_notifications_read_state.sql",
    "aap_notifications_v1.delete_command": "0318_notifications_delete.sql",
    "aap_workspace_v1.delete_project": "0199_project_deletion.sql",
    "aap_inference_backend_endpoint_v1._endpoint_record": "0369_inference_backend_endpoint.sql",
    "aap_inference_backend_endpoint_v1._endpoint_transport_identity": "0369_inference_backend_endpoint.sql",
    "aap_inference_backend_endpoint_v1.capture_backend_endpoint_subject": "0369_inference_backend_endpoint.sql",
    "aap_inference_backend_endpoint_v1._open_backend_endpoint_subject": "0369_inference_backend_endpoint.sql",
    "aap_inference_routing_v1._routing_registry_before_change": "0279_inference_routing.sql",
    "aap_labeling_v1.prediction_record_command": "0342_labeling_prediction_records.sql",
}

_ADDED_FUNCTIONS = (
    "aap_assets_v1.storage_connection_revision_bump()",
    "aap_assets_v1._connector_location_id(uuid)",
    "aap_assets_v1._storage_connection_location_referenced(uuid)",
    "aap_assets_v1._managed_original_endpoint(text)",
    "aap_assets_v1._managed_original_prefix(text)",
    "aap_assets_v1._managed_original_identity(text,text)",
    "aap_assets_v1._managed_original_gate_revision(uuid)",
    "aap_assets_v1._managed_original_request_valid(jsonb)",
    "aap_assets_v1.begin_managed_original(jsonb)",
    "aap_assets_v1._managed_original_reply(jsonb,text)",
    "aap_assets_v1._managed_original_facts(public.managed_original_creations)",
    "aap_assets_v1._managed_original_proof_valid(jsonb)",
    "aap_assets_v1.record_managed_original_proof(uuid,jsonb)",
    "aap_assets_v1.publish_verified_dataset_original()",
    "aap_assets_v1._managed_original_source_gate_active(uuid)",
    "aap_assets_v1.lock_publication_storage_gate(uuid,bigint)",
    "aap_notifications_v1._notification_is_migration_inventory(uuid)",
    "aap_notifications_v1.read_state_command_scoped(jsonb)",
    "aap_notifications_v1.delete_command_scoped(pg_catalog.jsonb)",
    "aap_notifications_v1._visible_inbox_rows_scoped(uuid,text)",
    "aap_notifications_v1.inbox_page_scoped(uuid,text,boolean,integer,integer)",
    "aap_notifications_v1.inbox_total_count_scoped(uuid,text)",
    "aap_notifications_v1.inbox_unread_count_scoped(uuid,text)",
    "aap_workspace_v1.retire_project_uploads(uuid)",
    "aap_labeling_v1.retire_project_comment_uploads(uuid)",
    "aap_assets_v1.read_video_derivative_links(uuid[])",
    "aap_labeling_v1.publish_task_pyramid_thumbnail(uuid,text)",
    "aap_assets_v1.publish_image_pyramid_thumbnail(uuid,integer,uuid)",
)


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def _restore_function(name: str, filename: str) -> None:
    source = (_SQL_DIR / filename).read_text(encoding="utf-8")
    prefixes = (f"CREATE FUNCTION {name}(", f"CREATE OR REPLACE FUNCTION {name}(")
    matches = [prefix for prefix in prefixes if prefix in source]
    if len(matches) != 1 or source.count(matches[0]) != 1:
        raise RuntimeError(f"Expected one frozen definition of {name}")
    start = source.index(matches[0])
    end = source.index("$function$;", start) + len("$function$;")
    definition = source[start:end]
    if definition.startswith("CREATE FUNCTION"):
        definition = definition.replace(
            "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
        )
    _execute(definition)


def upgrade() -> None:
    for filename in _ARTIFACTS:
        for statement in (
            (_SQL_DIR / filename)
            .read_text(encoding="utf-8")
            .split("-- aap-statement-break")
        ):
            if statement.strip():
                _execute(statement.strip())


def downgrade() -> None:
    # Restoring definitions preserves the pre-existing function owners and ACLs.
    # Main's durable object/creation/retirement records are never discarded here.
    for name, filename in _RESTORE.items():
        _restore_function(name, filename)
    _execute("DROP TRIGGER publish_verified_dataset_original ON public.dataset_items")
    _execute(
        "DROP TRIGGER storage_connection_revision_bump ON public.storage_connections"
    )
    _execute("DROP INDEX public.ix_managed_creation_dataset_object")
    _execute("DROP FUNCTION " + ", ".join(_ADDED_FUNCTIONS) + " RESTRICT")
