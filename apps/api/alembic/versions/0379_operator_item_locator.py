"""Fence operator replacement and auxiliary originals on the assets owner."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0379"
down_revision = "bo0378"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_ARTIFACTS = (
    "0379_operator_item_locator.sql",
    "0379_operator_auxiliary.sql",
)
_FUNCTIONS = (
    "aap_assets_v1.publish_operator_item_original(jsonb)",
    "aap_assets_v1.operator_item_original_facts(uuid,uuid)",
    "aap_assets_v1.operator_auxiliary_pointer(uuid,uuid,uuid)",
    "aap_assets_v1.begin_operator_auxiliary(jsonb)",
    "aap_assets_v1.publish_operator_auxiliary(uuid)",
    "aap_assets_v1.operator_auxiliary_owner(uuid,text,jsonb,uuid)",
    "aap_assets_v1.operator_auxiliary_material(uuid,uuid)",
    "aap_assets_v1.operator_item_original_material(uuid,uuid)",
    "aap_assets_v1.bind_operator_auxiliary_item(uuid,uuid,uuid)",
    "aap_assets_v1.bind_operator_auxiliary_scene(uuid,uuid,uuid)",
    "aap_assets_v1.operator_dataset_state(uuid)",
    "aap_assets_v1.operator_scene_state(uuid,uuid)",
    "aap_assets_v1.open_operator_storage_operation(uuid)",
    "aap_assets_v1.record_operator_original_prepared(uuid,jsonb)",
    "aap_assets_v1.mark_operator_original_writing(uuid)",
    "aap_assets_v1.record_operator_original_written(uuid,jsonb)",
    "aap_assets_v1.record_operator_original_verified(uuid,jsonb)",
    "aap_assets_v1.fail_operator_original(uuid,text,text,jsonb)",
    "aap_assets_v1.close_operator_storage_operation(uuid,uuid,boolean)",
    "aap_assets_v1._operator_read_permit_purpose(uuid)",
    "aap_assets_v1._managed_original_scope_key(text,text)",
    "aap_assets_v1._managed_original_import_prefix(text)",
)

_OLD_FUNCTIONS = (
    "aap_assets_v1.begin_managed_original(jsonb)",
    "aap_assets_v1.record_managed_original_proof(uuid,jsonb)",
)
_FROZEN_0371 = "0371_assets_owner_main_integration.sql"


def _restore_frozen(name: str) -> None:
    source = (_SQL_DIR / _FROZEN_0371).read_text(encoding="utf-8")
    base = name.split("(", 1)[0]
    prefixes = (f"CREATE FUNCTION {base}(", f"CREATE OR REPLACE FUNCTION {base}(")
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
    op.execute(
        sa.text(definition.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    for filename in _ARTIFACTS:
        for statement in (
            (_SQL_DIR / filename)
            .read_text(encoding="utf-8")
            .split("-- aap-statement-break")
        ):
            if statement.strip():
                op.execute(
                    sa.text(statement.strip().replace(":", r"\:")).execution_options(
                        no_parameters=True
                    )
                )


def downgrade() -> None:
    for signature in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION {signature} RESTRICT")
    for signature in reversed(_OLD_FUNCTIONS):
        _restore_frozen(signature)
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
