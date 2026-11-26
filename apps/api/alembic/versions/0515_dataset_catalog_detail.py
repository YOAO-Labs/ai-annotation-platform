"""Read the public dataset detail and lifecycle from one Assets snapshot."""

from alembic import op

revision = "bo0515"
down_revision = "bo0514"
branch_labels = depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION yoao_assets_v1.get_catalog_dataset_detail(p_actor uuid, p_dataset uuid)
        RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
        DECLARE
            actor jsonb;
            detail jsonb;
        BEGIN
            IF p_dataset IS NULL THEN
                RETURN yoao_assets_v1._catalog_failure('invalid_request');
            END IF;
            actor := yoao_assets_v1._catalog_actor(p_actor, false);
            IF actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN actor;
            END IF;
            SELECT yoao_assets_v1._catalog_dataset_summary(d.id) || pg_catalog.jsonb_build_object(
                'archived_at', d.archived_at, 'archived_by', d.archived_by,
                'lifecycle_revision', d.lifecycle_revision
            ) INTO detail FROM public.datasets AS d WHERE d.id = p_dataset;
            IF detail IS NULL THEN
                RETURN yoao_assets_v1._catalog_failure('dataset_missing');
            END IF;
            RETURN pg_catalog.jsonb_build_object('schema', 'aap-assets-catalog/v1', 'ok', true,
                'data', pg_catalog.jsonb_build_object('dataset', detail));
        END;
        $function$;
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION yoao_assets_v1.get_catalog_dataset_detail(uuid,uuid) FROM PUBLIC"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION yoao_assets_v1.get_catalog_dataset_detail(uuid,uuid)")
