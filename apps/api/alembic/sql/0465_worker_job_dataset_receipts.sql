-- A scope is recorded only by a new, proved worker Job. No historical scan,
-- Job FK or cascade: deleting a Job cannot turn its UUID into a fresh birth.
CREATE TABLE aap_jobs_v1.fixture_worker_job_datasets (
    job_id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('prediction_retry', 'point_cloud_cross_frame')),
    project_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    dataset_ids uuid[] NOT NULL CHECK (pg_catalog.array_position(dataset_ids, NULL::uuid) IS NULL),
    payload_sha256 bytea NOT NULL CHECK (pg_catalog.octet_length(payload_sha256) = 32)
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.fixture_worker_job_datasets FROM PUBLIC;
-- aap-statement-break
-- Retention may remove the Job before a later fixture claims this Dataset.
-- Only the IDs of deletions lacking physical retirement proof are needed.
CREATE TABLE aap_jobs_v1.unsettled_worker_job_deletions (
    job_id uuid PRIMARY KEY REFERENCES aap_jobs_v1.fixture_worker_job_datasets(job_id)
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.unsettled_worker_job_deletions FROM PUBLIC;
