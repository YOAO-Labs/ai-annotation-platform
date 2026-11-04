"""Add transaction-owned acceptance history to the existing Mask capability."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0430"
down_revision = "bo0429"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_KINDS = (
    "'split_components', 'copy_component', 'copy_keyframe', 'join_masks', "
    "'overlap', 'convert_annotations', 'delete_small_islands', 'fill_small_holes', "
    "'resolve_same_class_overlap', 'mask_repair_rollback', 'slice_polygon', "
    "'restore_slice', 'slice_mask', 'create_annotation'"
)


def _apply(sql: str) -> None:
    op.execute(sa.text(sql.replace(":", r"\:")).execution_options(no_parameters=True))


def _mask_command(*, history: bool) -> None:
    source = (_SQL / "0348_labeling_mask_edits.sql").read_text(encoding="utf-8")
    source = source.split("-- aap-statement-break", 1)[0]
    source = source.replace("CREATE FUNCTION ", "CREATE OR REPLACE FUNCTION ", 1)
    marker = "    v_operation := p_request ->> 'operation';\n"
    if source.count(marker) != 1:
        raise RuntimeError(
            "Mask edit owner source no longer matches the frozen command"
        )
    if history:
        source = source.replace(
            marker,
            marker
            + "    IF v_operation = 'acceptance_history' THEN\n"
            + "        RETURN aap_labeling_v1._acceptance_history(p_request);\n"
            + "    END IF;\n",
            1,
        )
    _apply(source)


def upgrade() -> None:
    op.drop_constraint(
        "ck_annotation_operations_kind", "annotation_operations", type_="check"
    )
    op.create_check_constraint(
        "ck_annotation_operations_kind",
        "annotation_operations",
        f"kind IN ({_KINDS}, 'accept_prediction', 'restore_acceptance')",
    )
    _apply((_SQL / "0430_acceptance_source.sql").read_text(encoding="utf-8"))
    _apply("""
        DO $owner$
        DECLARE
            assets_owner name;
            labeling_owner name;
            assets_create boolean;
        BEGIN
            SELECT pg_catalog.pg_get_userbyid(proowner) INTO STRICT assets_owner
            FROM pg_catalog.pg_proc
            WHERE oid = 'aap_assets_v1._image_owner_source(uuid,uuid,uuid,boolean)'::pg_catalog.regprocedure;
            SELECT pg_catalog.pg_get_userbyid(proowner) INTO STRICT labeling_owner
            FROM pg_catalog.pg_proc
            WHERE oid = 'aap_labeling_v1.mask_edit_command(jsonb)'::pg_catalog.regprocedure;
            assets_create := pg_catalog.has_schema_privilege(assets_owner, 'aap_assets_v1', 'CREATE');
            IF NOT assets_create THEN
                EXECUTE pg_catalog.format('GRANT CREATE ON SCHEMA aap_assets_v1 TO %I', assets_owner);
            END IF;
            EXECUTE pg_catalog.format(
                'ALTER FUNCTION aap_assets_v1._acceptance_history_source(uuid) OWNER TO %I', assets_owner);
            EXECUTE pg_catalog.format(
                'GRANT EXECUTE ON FUNCTION aap_assets_v1._acceptance_history_source(uuid) TO %I', labeling_owner);
            IF NOT assets_create THEN
                EXECUTE pg_catalog.format('REVOKE CREATE ON SCHEMA aap_assets_v1 FROM %I', assets_owner);
            END IF;
        END
        $owner$;
    """)
    _apply(
        "REVOKE ALL ON FUNCTION aap_assets_v1._acceptance_history_source(uuid) FROM PUBLIC"
    )
    _apply((_SQL / "0430_acceptance_history.sql").read_text(encoding="utf-8"))
    _apply("""
        DO $owner$
        DECLARE owner_name text;
        BEGIN
            SELECT pg_catalog.pg_get_userbyid(proowner) INTO owner_name
            FROM pg_catalog.pg_proc
            WHERE oid = 'aap_labeling_v1.mask_edit_command(jsonb)'::pg_catalog.regprocedure;
            EXECUTE pg_catalog.format(
                'ALTER FUNCTION aap_labeling_v1._acceptance_history(jsonb) OWNER TO %I', owner_name);
        END
        $owner$;
    """)
    _apply(
        "REVOKE ALL ON FUNCTION aap_labeling_v1._acceptance_history(jsonb) FROM PUBLIC"
    )
    _mask_command(history=True)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("LOCK TABLE annotation_operations IN ACCESS EXCLUSIVE MODE")
    )
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM annotation_operations "
            "WHERE kind IN ('accept_prediction','restore_acceptance'))"
        )
    ):
        raise RuntimeError("Acceptance history exists; retain the compatible schema")
    _mask_command(history=False)
    op.execute("DROP FUNCTION aap_labeling_v1._acceptance_history(jsonb)")
    op.execute("DROP FUNCTION aap_assets_v1._acceptance_history_source(uuid)")
    op.drop_constraint(
        "ck_annotation_operations_kind", "annotation_operations", type_="check"
    )
    op.create_check_constraint(
        "ck_annotation_operations_kind", "annotation_operations", f"kind IN ({_KINDS})"
    )
