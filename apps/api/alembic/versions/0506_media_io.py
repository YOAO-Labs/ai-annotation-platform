"""Give Assets Source finite registered media physical capabilities."""

from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "bo0506"
down_revision = "bo0505"
branch_labels = depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1] / "sql" / "0506_media_io.sql"
    for statement in sql.read_text(encoding="utf-8").split("-- aap-statement-break"):
        op.execute(
            text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
        )


def downgrade() -> None:
    op.execute("LOCK TABLE yoao_assets_v1.media_io_claims IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $block$
        BEGIN
            IF EXISTS (SELECT FROM yoao_assets_v1.media_io_claims) THEN
                RAISE EXCEPTION 'Settle registered media physical operations before downgrade';
            END IF;
        END;
        $block$;
    """)
    for signature in (
        "yoao_assets_v1.open_saved_operator_original_operation(uuid)",
        "yoao_assets_v1.media_io_retired_original(uuid,uuid,text,boolean)",
        "yoao_assets_v1.media_io_prediction_input(uuid,uuid,text,text,boolean)",
        "yoao_assets_v1.begin_embedded_admitted_original(jsonb)",
        "yoao_assets_v1.prove_embedded_admitted_original(jsonb)",
        "yoao_assets_v1._embedded_original_builtin(text)",
        "yoao_assets_v1.media_io_original_cache(uuid,uuid,uuid,text,boolean)",
        "yoao_assets_v1.media_io_derivative(text,text,uuid,uuid,uuid,jsonb,boolean)",
        "yoao_assets_v1.media_io_mask_upload(uuid,uuid,text)",
        "yoao_labeling_v1._media_io_mask_upload(uuid)",
        "yoao_assets_v1.media_io_thumbnail(text,uuid,uuid,boolean)",
        "yoao_assets_v1.saved_operator_auxiliary_original(uuid,uuid)",
        "yoao_assets_v1.saved_operator_item_original(uuid,uuid)",
        "yoao_assets_v1.begin_saved_operator_original(jsonb)",
        "yoao_assets_v1.media_io_creation_evidence(uuid,uuid,text,jsonb)",
        "yoao_assets_v1.media_io_creation(uuid,uuid,uuid,text,boolean)",
        "yoao_assets_v1.media_io_original_read(text,uuid,uuid)",
        "yoao_assets_v1.media_io_cache_read(text,uuid,text,uuid)",
        "yoao_assets_v1.media_io_artifact(text,text,uuid,uuid,boolean)",
        "yoao_labeling_v1._media_io_raster(uuid,text)",
        "yoao_labeling_v1._media_io_comment(uuid,text)",
        "yoao_workspace_v1._media_io_guide(uuid,text)",
        "yoao_assets_v1._media_io_import_target(jsonb,text,text)",
        "yoao_assets_v1._media_io_creation_target(public.managed_original_creations)",
        "yoao_assets_v1._media_io_target(jsonb,text,text,boolean)",
        "yoao_assets_v1._media_io_permit(uuid,uuid,text)",
    ):
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("DROP TABLE yoao_assets_v1.media_io_claims")
    op.execute("DROP TABLE yoao_assets_v1.media_io_derivative_manifests")
    op.execute("DROP TABLE yoao_assets_v1.embedded_media_claim_key")
