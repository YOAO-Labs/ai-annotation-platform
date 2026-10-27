"""Pin each retained training run to its explicit physical namespace."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0221"
down_revision = "0220"
branch_labels = None
depends_on = None

# Keep migrations independent of application imports and future model changes.
_DATASET_REF_CHECK = "\nstorage_ref IS NULL OR COALESCE((\n    jsonb_typeof(storage_ref) = 'object'\n    AND storage_ref ?& ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','prefix','storage_identity',\n        'origin','evidence']\n    AND storage_ref - ARRAY['schema','purpose','location_id','config_revision',\n        'binding_generation','endpoint_url','bucket','prefix','storage_identity',\n        'origin','evidence']::text[] = '{}'::jsonb\n    AND storage_ref->'schema' = '1'::jsonb\n    AND storage_ref->>'purpose' = 'training-dataset'\n    AND jsonb_typeof(storage_ref->'location_id') = 'string'\n    AND storage_ref->'config_revision' = '1'::jsonb\n    AND storage_ref->'binding_generation' = '1'::jsonb\n    AND jsonb_typeof(storage_ref->'endpoint_url') = 'string'\n    AND storage_ref->>'endpoint_url' ~ '^https?://[^[:space:]/?#@]+$'\n    AND jsonb_typeof(storage_ref->'bucket') = 'string'\n    AND length(storage_ref->>'bucket') BETWEEN 1 AND 255\n    AND storage_ref->>'bucket' !~ '[[:space:]/?#@]'\n    AND position(chr(92) in (storage_ref->>'bucket')) = 0\n    AND jsonb_typeof(storage_ref->'prefix') = 'string'\n    AND storage_ref->>'prefix' ~ '^projects/[0-9a-f-]{36}/datasets/[0-9a-f-]{36}/$'\n    AND jsonb_typeof(storage_ref->'storage_identity') = 'string'\n    AND storage_ref->>'storage_identity' ~ '^[0-9a-f]{64}$'\n    AND storage_ref->>'origin' IN ('new','verified-retained')\n    AND (storage_ref->'evidence' = 'null'::jsonb\n        OR jsonb_typeof(storage_ref->'evidence') = 'object')\n    AND (storage_ref->>'origin' != 'new' OR storage_ref->'evidence' = 'null'::jsonb)\n), false)\n"
_RUN_REF_CHECK = _DATASET_REF_CHECK.replace(
    "'training-dataset'", "'training-run'"
).replace("/datasets/", "/runs/")
_DELETION_REF_CHECK = f"({_DATASET_REF_CHECK}) OR ({_RUN_REF_CHECK})"


def upgrade():
    op.add_column("training_runs", sa.Column("storage_ref", JSONB(none_as_null=True)))
    op.create_check_constraint(
        "ck_training_run_storage_ref", "training_runs", _RUN_REF_CHECK
    )
    op.create_check_constraint(
        "ck_training_run_storage_prefix",
        "training_runs",
        "storage_ref IS NULL OR storage_ref->>'prefix' = "
        "'projects/' || project_id::text || '/runs/' || id::text || '/'",
    )
    op.execute(
        "CREATE TRIGGER trg_training_runs_storage_ref "
        "BEFORE UPDATE ON training_runs FOR EACH ROW "
        "EXECUTE FUNCTION guard_training_dataset_storage_ref()"
    )
    op.drop_constraint(
        "ck_training_deletion_storage_ref", "training_storage_deletions", type_="check"
    )
    op.create_check_constraint(
        "ck_training_deletion_storage_ref",
        "training_storage_deletions",
        _DELETION_REF_CHECK,
    )


def downgrade():
    op.execute(
        "LOCK TABLE training_runs, training_storage_deletions IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM training_runs WHERE storage_ref IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM training_storage_deletions "
            "WHERE storage_ref->>'purpose' = 'training-run')"
        )
    ):
        raise RuntimeError(
            "Pinned training run storage facts prevent destructive downgrade"
        )
    op.drop_constraint(
        "ck_training_deletion_storage_ref", "training_storage_deletions", type_="check"
    )
    op.create_check_constraint(
        "ck_training_deletion_storage_ref",
        "training_storage_deletions",
        _DATASET_REF_CHECK,
    )
    op.execute("DROP TRIGGER trg_training_runs_storage_ref ON training_runs")
    op.drop_constraint("ck_training_run_storage_prefix", "training_runs", type_="check")
    op.drop_constraint("ck_training_run_storage_ref", "training_runs", type_="check")
    op.drop_column("training_runs", "storage_ref")
