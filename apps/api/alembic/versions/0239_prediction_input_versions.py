"""Require immutable object versions for new prediction input receipts."""

from alembic import op

revision = "0239"
down_revision = "0238"
branch_labels = None
depends_on = None


def upgrade():
    # Keep the complete ownership/restore guard from 0238. Historical null
    # receipts remain immutable evidence and may still retire or be relocated.
    op.execute("""
    CREATE FUNCTION guard_prediction_input_version() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE member record;
    BEGIN
        IF NEW.contract_version<>2 THEN RETURN NEW; END IF;
        FOR member IN SELECT key,value FROM jsonb_each(NEW.inventory->'objects') LOOP
            IF member.value->>'state'='verified' AND (
                jsonb_typeof(member.value->'version_id') IS DISTINCT FROM 'string'
                OR member.value->>'version_id'='null'
            ) THEN
                IF TG_OP='UPDATE' THEN
                    IF OLD.inventory->'objects'->member.key=member.value THEN
                        CONTINUE;
                    END IF;
                END IF;
                RAISE EXCEPTION 'New prediction input receipts require immutable object versions'
                    USING ERRCODE='23514',CONSTRAINT='ck_prediction_input_version';
            END IF;
        END LOOP;
        RETURN NEW;
    END $$;
    """)
    op.execute(
        "CREATE TRIGGER trg_prediction_input_version BEFORE INSERT OR UPDATE "
        "ON prediction_input_leases FOR EACH ROW EXECUTE FUNCTION guard_prediction_input_version()"
    )


def downgrade():
    # No locator, receipt, tombstone or responsibility is removed. Further
    # downgrade through 0238 still refuses every v2 owner, including tombstones.
    op.execute("DROP TRIGGER trg_prediction_input_version ON prediction_input_leases")
    op.execute("DROP FUNCTION guard_prediction_input_version()")
