"""Seal authenticated directory references without a managed image Input."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0511"
down_revision = "bo0510"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_MEMBERS = "public.asset_accepted_image_input_manifest_members"
_STATE = "ck_asset_accepted_image_input_manifest_member_state"


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    source = (_SQL / "0511_image_reference_manifest_members.sql").read_text(
        encoding="utf-8"
    )
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            _execute(statement)


def downgrade() -> None:
    _execute(f"LOCK TABLE {_MEMBERS} IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text(f"SELECT EXISTS (SELECT FROM {_MEMBERS} WHERE state = 'reference')")
    ):
        raise RuntimeError("Cannot downgrade retained reference manifest members")

    control = (_SQL / "0222_accepted_input_manifests_control.sql").read_text(
        encoding="utf-8"
    )
    for name in (
        "capture_accepted_image_input_manifest",
        "seal_accepted_image_input_manifest",
    ):
        prefix = f"CREATE FUNCTION aap_assets_v1.{name}("
        if control.count(prefix) != 1:
            raise RuntimeError(f"Expected one frozen definition of {name}")
        start = control.index(prefix)
        end = control.index("$function$;", start) + len("$function$;")
        _execute(
            control[start:end]
            .replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
            .replace("aap_assets_v1.", "yoao_assets_v1.")
            .replace("aap_labeling_v1.", "yoao_labeling_v1.")
        )

    _execute(
        "DROP FUNCTION yoao_assets_v1._image_manifest_reference_matches(jsonb,jsonb,jsonb)"
    )
    _execute(
        "DROP FUNCTION yoao_assets_v1.is_sealed_image_reference_member("
        "uuid,uuid,uuid,uuid,text,uuid,text,integer,bigint)"
    )
    state = (_SQL / "0222_accepted_input_manifests_state.sql").read_text(
        encoding="utf-8"
    )
    start = state.index(f"CONSTRAINT {_STATE} CHECK (")
    end = state.index("\n);", start)
    _execute(f"ALTER TABLE {_MEMBERS} DROP CONSTRAINT {_STATE}, ADD {state[start:end]}")
