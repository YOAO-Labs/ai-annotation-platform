"""Allow independent managed secondary-inference input responsibility."""

from alembic import op

revision = "0240"
down_revision = "0239"
branch_labels = None
depends_on = None


def _owner_check(*, secondary):
    kinds = (
        "'batch_predict','prediction_retry','interactive_predict',"
        "'predict_test','mask_repair'"
    )
    if secondary:
        kinds += ",'secondary_inference'"
    op.create_check_constraint(
        "ck_prediction_input_owner",
        "prediction_input_leases",
        f"contract_version=1 OR owner_kind IN ({kinds})",
    )


def upgrade():
    op.drop_constraint(
        "ck_prediction_input_owner", "prediction_input_leases", type_="check"
    )
    _owner_check(secondary=True)


def downgrade():
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM prediction_input_leases
            WHERE contract_version=2 AND owner_kind='secondary_inference'
        ) THEN
            RAISE EXCEPTION 'Secondary prediction input responsibility prevents downgrade'
                USING ERRCODE='23514';
        END IF;
    END $$;
    """)
    op.drop_constraint(
        "ck_prediction_input_owner", "prediction_input_leases", type_="check"
    )
    _owner_check(secondary=False)
