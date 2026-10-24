"""Extend the existing media owner with a bounded video delivery profile."""

from alembic import op
import sqlalchemy as sa

revision = "0237"
down_revision = "0236"
branch_labels = None
depends_on = None

VIDEO_CHECK = """
CASE WHEN input_snapshot->'schema' = '4'::jsonb THEN COALESCE((
    input_snapshot->>'schema' = '4'
    AND input_snapshot->'probe_contract' = '1'::jsonb
    AND input_snapshot->>'probe_contract' = '1'
    AND input_snapshot->'references' = '{}'::jsonb
    AND jsonb_typeof(input_snapshot->'project') = 'object'
    AND input_snapshot->'project'->>'id' = project_id::text
    AND input_snapshot->'project'->>'data_type' = 'video'
    AND jsonb_typeof(input_snapshot->'tasks') = 'array'
    AND jsonb_typeof(input_snapshot->'media') = 'array'
    AND jsonb_array_length(CASE WHEN jsonb_typeof(input_snapshot->'tasks') = 'array'
        THEN input_snapshot->'tasks' ELSE '[]'::jsonb END) <= 1000
    AND jsonb_array_length(CASE WHEN jsonb_typeof(input_snapshot->'media') = 'array'
        THEN input_snapshot->'media' ELSE '[]'::jsonb END)
        = jsonb_array_length(CASE WHEN jsonb_typeof(input_snapshot->'tasks') = 'array'
        THEN input_snapshot->'tasks' ELSE '[]'::jsonb END)
    AND object_count <= 1000 AND size_bytes <= 21474836480
), false) ELSE true END
"""

_INVENTORY_START = (
    "IF jsonb_typeof(NEW.inventory->'objects') IS DISTINCT FROM 'object' THEN"
)
_VIDEO_BINDINGS = """IF NEW.input_snapshot->'schema' = '4'::jsonb THEN
                    IF NOT COALESCE(
                        jsonb_typeof(NEW.input_snapshot->'tasks') = 'array'
                        AND jsonb_typeof(NEW.input_snapshot->'media') = 'array', false)
                    THEN
                        RAISE EXCEPTION 'Video media requires bounded Task bindings'
                            USING ERRCODE='23514', CONSTRAINT='ck_export_media_video';
                    END IF;
                    IF jsonb_array_length(NEW.input_snapshot->'tasks') > 1000
                        OR jsonb_array_length(NEW.input_snapshot->'media') > 1000
                    THEN
                        RAISE EXCEPTION 'Video Task bindings exceed their budget'
                            USING ERRCODE='23514', CONSTRAINT='ck_export_media_video';
                    END IF;
                    FOR member IN SELECT value FROM jsonb_array_elements(NEW.input_snapshot->'media') LOOP
                        IF NOT COALESCE(
                            jsonb_typeof(member.value) = 'object'
                            AND member.value ?& ARRAY['task_id','role','link_id','source_key','source']
                            AND member.value - ARRAY['task_id','role','link_id','source_key','source']::text[] = '{}'::jsonb
                            AND member.value->>'role' = 'video'
                            AND member.value->'link_id' = 'null'::jsonb
                            AND jsonb_typeof(member.value->'source') = 'object'
                            AND member.value->'source'->>'owner_kind' IN ('dataset_asset','task_original')
                            AND member.value->'source'->>'media_type' = 'video', false)
                        THEN
                            RAISE EXCEPTION 'Video media binding is invalid'
                                USING ERRCODE='23514', CONSTRAINT='ck_export_media_video';
                        END IF;
                    END LOOP;
                END IF;
                """

_GUARD_REPLACEMENTS = (
    (
        "member_size < 0 OR member_size > 268435456",
        "member_size < 0 OR (NEW.input_snapshot->'schema' = '4'::jsonb AND member_size = 0) "
        "OR member_size > CASE WHEN NEW.input_snapshot->'schema' = '4'::jsonb "
        "THEN 5368709120 ELSE 268435456 END",
    ),
    (
        "counted > 21000",
        "counted > CASE WHEN NEW.input_snapshot->'schema' = '4'::jsonb THEN 1000 ELSE 21000 END",
    ),
    (
        "total > 25769803776",
        "total > CASE WHEN NEW.input_snapshot->'schema' = '4'::jsonb THEN 21474836480 ELSE 25769803776 END",
    ),
    (_INVENTORY_START, _VIDEO_BINDINGS + _INVENTORY_START),
)
_PAIR_REPLACEMENTS = (
    (
        "OR output.media IS DISTINCT FROM 'image'",
        "OR output.media IS DISTINCT FROM CASE WHEN bundle.input_snapshot->'schema' = '4'::jsonb "
        "THEN 'video' ELSE 'image' END",
    ),
)


def _definition(value, replacements, *, upgrade):
    """Replace known clauses only; retain all ownership and physical-close guards."""
    if not isinstance(value, str):
        raise RuntimeError("Unexpected export media guard definition")
    for old, new in replacements:
        before, after = (old, new) if upgrade else (new, old)
        if value.count(before) != 1 or (upgrade and after in value):
            raise RuntimeError("Unexpected export media guard definition")
        value = value.replace(before, after)
    return value


def _replace(name, replacements, *, upgrade):
    definition = op.get_bind().scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:name AS regprocedure))"),
        {"name": name + "()"},
    )
    op.execute(_definition(definition, replacements, upgrade=upgrade))


def upgrade():
    op.create_check_constraint(
        "ck_export_media_video", "export_media_bundles", VIDEO_CHECK
    )
    _replace("guard_export_media_bundle", _GUARD_REPLACEMENTS, upgrade=True)
    _replace("check_export_media_pair", _PAIR_REPLACEMENTS, upgrade=True)


def downgrade():
    # Even empty/deleted schema-four owners remain incompatible historical facts.
    op.execute(
        "LOCK TABLE export_media_bundles, export_outputs IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text("""
            SELECT EXISTS (
                SELECT 1 FROM export_media_bundles WHERE input_snapshot->'schema' = '4'::jsonb
            ) OR EXISTS (
                SELECT 1 FROM export_outputs WHERE media_bundle_id IS NOT NULL AND media = 'video'
            )
        """)
    ):
        raise RuntimeError("Cannot downgrade while video media responsibility exists")
    _replace("check_export_media_pair", _PAIR_REPLACEMENTS, upgrade=False)
    _replace("guard_export_media_bundle", _GUARD_REPLACEMENTS, upgrade=False)
    op.drop_constraint("ck_export_media_video", "export_media_bundles", type_="check")
