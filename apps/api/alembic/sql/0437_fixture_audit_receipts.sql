LOCK TABLE yoao_fixture_v1.fixture_run_resources IN ACCESS EXCLUSIVE MODE;
-- aap-statement-break
DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources WHERE kind = 'audit'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'fixture_audit_uuid_receipts_require_explicit_retirement';
    END IF;
END
$migration$;
-- aap-statement-break
CREATE TABLE yoao_fixture_v1.fixture_audit_events (
    audit_id bigint NOT NULL,
    audit_created_at timestamptz NOT NULL,
    run_id uuid NOT NULL REFERENCES yoao_fixture_v1.fixture_runs(id) ON DELETE RESTRICT,
    PRIMARY KEY (audit_id, audit_created_at)
);
-- aap-statement-break
CREATE INDEX ix_yoao_fixture_v1_fixture_audit_events_run_id
ON yoao_fixture_v1.fixture_audit_events (run_id);
-- aap-statement-break
REVOKE ALL ON TABLE yoao_fixture_v1.fixture_audit_events FROM PUBLIC;
-- aap-statement-break
ALTER TABLE yoao_fixture_v1.fixture_run_resources
    DROP CONSTRAINT ck_fixture_run_resource_kind,
    ADD CONSTRAINT ck_fixture_run_resource_kind CHECK (
        kind IN ('user', 'project', 'dataset', 'item', 'task', 'creation',
                 'auxiliary', 'annotation', 'registry', 'template', 'reclaim', 'invitation')
    );
