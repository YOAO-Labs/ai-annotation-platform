"""Retain image-pyramid attempt owners independently of business cascades."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0232"
down_revision = "0231"
branch_labels = None
depends_on = None

_REF_CHECK = r"""
COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','prefix','storage_identity']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','prefix','storage_identity']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb AND storage_ref->>'schema' = '1'
    AND storage_ref->>'purpose' = 'media-cache'
    AND storage_ref->>'location_id' = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
    AND storage_ref->'config_revision' = '1'::jsonb AND storage_ref->>'config_revision' = '1'
    AND storage_ref->'binding_generation' = '1'::jsonb AND storage_ref->>'binding_generation' = '1'
    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND position(chr(92) in (storage_ref->>'endpoint_url')) = 0
    AND jsonb_typeof(storage_ref->'bucket') = 'string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'
    AND position(chr(92) in (storage_ref->>'bucket')) = 0
    AND storage_ref->>'prefix' = 'image-pyramids/objects/' || id::text || '/'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
), false)
"""


def upgrade():
    op.create_table(
        "image_pyramid_outputs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("asset_id", UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("lease_token", UUID(as_uuid=True), nullable=False),
        sa.Column("storage_ref", JSONB(none_as_null=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="building"),
        sa.Column("inventory", JSONB(none_as_null=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("generation > 0", name="ck_image_pyramid_output_generation"),
        sa.CheckConstraint(
            "state IN ('building','ready','retiring','deleted')",
            name="ck_image_pyramid_output_state",
        ),
        sa.CheckConstraint(
            "(state != 'ready' OR inventory IS NOT NULL) "
            "AND (state != 'building' OR inventory IS NULL) "
            "AND (inventory IS NULL OR COALESCE((jsonb_typeof(inventory) = 'object' "
            "AND inventory ?& ARRAY['schema','objects'] "
            "AND inventory - ARRAY['schema','objects']::text[] = '{}'::jsonb "
            "AND inventory->'schema' = '1'::jsonb AND inventory->>'schema' = '1' "
            "AND jsonb_typeof(inventory->'objects') = 'object' "
            "AND inventory->'objects' ? (storage_ref->>'prefix' || 'manifest.json') "
            "AND inventory->'objects' ? (storage_ref->>'prefix' || 'overview.webp')), false))",
            name="ck_image_pyramid_output_inventory",
        ),
        sa.CheckConstraint(_REF_CHECK, name="ck_image_pyramid_output_storage_ref"),
    )
    op.create_index(
        "ix_image_pyramid_outputs_cleanup",
        "image_pyramid_outputs",
        ["state", "updated_at", "id"],
    )
    op.add_column(
        "image_pyramid_generations", sa.Column("original_ref", JSONB(none_as_null=True))
    )
    op.add_column(
        "image_pyramid_generations", sa.Column("auth_ref", JSONB(none_as_null=True))
    )
    op.add_column(
        "image_pyramid_generations", sa.Column("output_id", UUID(as_uuid=True))
    )
    op.create_foreign_key(
        "fk_image_pyramid_generation_output",
        "image_pyramid_generations",
        "image_pyramid_outputs",
        ["output_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_image_pyramid_input_receipts",
        "image_pyramid_generations",
        "COALESCE((original_ref IS NULL AND auth_ref IS NULL) OR "
        "(jsonb_typeof(original_ref) = 'object' AND jsonb_typeof(auth_ref) = 'object'), false)",
    )
    op.execute("""
        CREATE FUNCTION guard_image_pyramid_output() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='DELETE' THEN
                IF OLD.state <> 'deleted' THEN
                    RAISE EXCEPTION 'Unsettled pyramid output owners must be retained'
                        USING ERRCODE='23514', CONSTRAINT='ck_image_pyramid_output_delete_guard';
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP='INSERT' THEN
                IF NEW.state <> 'building' OR NEW.inventory IS NOT NULL THEN
                    RAISE EXCEPTION 'New pyramid outputs must begin building'
                        USING ERRCODE='23514', CONSTRAINT='ck_image_pyramid_output_initial';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.state='deleted' AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Deleted pyramid outputs are terminal'
                    USING ERRCODE='23514', CONSTRAINT='ck_image_pyramid_output_terminal';
            END IF;
            IF ROW(NEW.id,NEW.asset_id,NEW.generation,NEW.lease_token,NEW.storage_ref,NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id,OLD.asset_id,OLD.generation,OLD.lease_token,OLD.storage_ref,OLD.created_at)
            THEN
                RAISE EXCEPTION 'Pyramid output identity is immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_image_pyramid_output_facts_immutable';
            END IF;
            IF NOT (NEW.state=OLD.state
                OR (OLD.state='building' AND NEW.state IN ('ready','retiring'))
                OR (OLD.state='ready' AND NEW.state='retiring')
                OR (OLD.state='retiring' AND NEW.state='deleted'))
            THEN
                RAISE EXCEPTION 'Pyramid output state cannot move backwards'
                    USING ERRCODE='23514', CONSTRAINT='ck_image_pyramid_output_state_forward';
            END IF;
            IF NEW.inventory IS DISTINCT FROM OLD.inventory
                AND NOT (OLD.state='building' AND NEW.state='ready'
                    AND OLD.inventory IS NULL AND NEW.inventory IS NOT NULL)
            THEN
                RAISE EXCEPTION 'Pyramid inventory is published exactly once'
                    USING ERRCODE='23514', CONSTRAINT='ck_image_pyramid_output_inventory_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_image_pyramid_output BEFORE INSERT OR UPDATE OR DELETE ON image_pyramid_outputs FOR EACH ROW EXECUTE FUNCTION guard_image_pyramid_output()"
    )
    op.execute("""
        CREATE FUNCTION guard_image_pyramid_inputs() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.original_ref,NEW.auth_ref) IS DISTINCT FROM ROW(OLD.original_ref,OLD.auth_ref) THEN
                RAISE EXCEPTION 'Pyramid input and authority receipts are immutable'
                    USING ERRCODE='23514', CONSTRAINT='ck_image_pyramid_inputs_immutable';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_image_pyramid_inputs BEFORE UPDATE ON image_pyramid_generations FOR EACH ROW EXECUTE FUNCTION guard_image_pyramid_inputs()"
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM image_pyramid_outputs) OR EXISTS "
            "(SELECT 1 FROM image_pyramid_generations WHERE original_ref IS NOT NULL "
            "OR auth_ref IS NOT NULL OR output_id IS NOT NULL)"
        )
    ).scalar():
        raise RuntimeError("Cannot discard saved pyramid ownership or receipts")
    op.execute("DROP TRIGGER trg_image_pyramid_inputs ON image_pyramid_generations")
    op.execute("DROP FUNCTION guard_image_pyramid_inputs()")
    op.drop_constraint(
        "ck_image_pyramid_input_receipts", "image_pyramid_generations", type_="check"
    )
    op.drop_constraint(
        "fk_image_pyramid_generation_output",
        "image_pyramid_generations",
        type_="foreignkey",
    )
    for name in ("output_id", "auth_ref", "original_ref"):
        op.drop_column("image_pyramid_generations", name)
    op.execute("DROP TRIGGER trg_image_pyramid_output ON image_pyramid_outputs")
    op.execute("DROP FUNCTION guard_image_pyramid_output()")
    op.drop_table("image_pyramid_outputs")
