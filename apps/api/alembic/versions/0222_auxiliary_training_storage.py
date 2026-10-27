"""Pin auxiliary training outputs to each durable object's own namespace."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0222"
down_revision = "0221"
branch_labels = None
depends_on = None

# Keep migrations independent of application imports and future model changes.
_REF_CHECK = """
storage_ref IS NULL OR COALESCE((
    jsonb_typeof(storage_ref) = 'object'
    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','prefix','storage_identity',
        'origin','evidence']
    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',
        'binding_generation','endpoint_url','bucket','prefix','storage_identity',
        'origin','evidence']::text[] = '{}'::jsonb
    AND storage_ref->'schema' = '1'::jsonb
    AND storage_ref->>'purpose' = 'training-dataset'
    AND jsonb_typeof(storage_ref->'location_id') = 'string'
    AND storage_ref->'config_revision' = '1'::jsonb
    AND storage_ref->'binding_generation' = '1'::jsonb
    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'
    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'
    AND jsonb_typeof(storage_ref->'bucket') = 'string'
    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255
    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'
    AND position(chr(92) in (storage_ref->>'bucket')) = 0
    AND jsonb_typeof(storage_ref->'prefix') = 'string'
    AND storage_ref->>'prefix' ~ '^projects/[0-9a-f-]{36}/datasets/[0-9a-f-]{36}/$'
    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'
    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'
    AND storage_ref->>'origin' IN ('new','verified-retained')
    AND (storage_ref->'evidence' = 'null'::jsonb
        OR jsonb_typeof(storage_ref->'evidence') = 'object')
    AND (storage_ref->>'origin' != 'new' OR storage_ref->'evidence' = 'null'::jsonb)
), false)
"""
_OWNERS = (
    ("training_previews", "training_preview", "training-preview", "previews"),
    (
        "sample_evaluation_runs",
        "sample_evaluation",
        "sample-evaluation",
        "sample-evaluations",
    ),
    ("active_learning_rounds", "active_learning", "active-learning", "active-learning"),
)


def upgrade():
    for table, stem, purpose, namespace in _OWNERS:
        op.add_column(table, sa.Column("storage_ref", JSONB(none_as_null=True)))
        op.create_check_constraint(
            f"ck_{stem}_storage_ref",
            table,
            _REF_CHECK.replace("'training-dataset'", f"'{purpose}'").replace(
                "/datasets/", f"/{namespace}/"
            ),
        )
        op.create_check_constraint(
            f"ck_{stem}_storage_prefix",
            table,
            "storage_ref IS NULL OR storage_ref->>'prefix' = "
            f"'projects/' || project_id::text || '/{namespace}/' || id::text || '/'",
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_storage_ref "
            f"BEFORE UPDATE ON {table} FOR EACH ROW "
            "EXECUTE FUNCTION guard_training_dataset_storage_ref()"
        )


def downgrade():
    op.execute(
        "LOCK TABLE "
        + ", ".join(table for table, *_ in _OWNERS)
        + " IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT "
            + " OR ".join(
                f"EXISTS (SELECT 1 FROM {table} WHERE storage_ref IS NOT NULL)"
                for table, *_ in _OWNERS
            )
        )
    ):
        raise RuntimeError(
            "Pinned auxiliary training storage facts prevent destructive downgrade"
        )
    for table, stem, *_ in reversed(_OWNERS):
        op.execute(f"DROP TRIGGER trg_{table}_storage_ref ON {table}")
        op.drop_constraint(f"ck_{stem}_storage_prefix", table, type_="check")
        op.drop_constraint(f"ck_{stem}_storage_ref", table, type_="check")
        op.drop_column(table, "storage_ref")
