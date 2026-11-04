CREATE SCHEMA yoao_fixture_v1;
-- aap-statement-break
REVOKE ALL ON SCHEMA yoao_fixture_v1 FROM PUBLIC;
-- aap-statement-break
ALTER TABLE public.managed_original_retirements
    ALTER COLUMN dataset_item_id DROP NOT NULL,
    ALTER COLUMN asset_id DROP NOT NULL,
    ADD COLUMN auxiliary_original_id uuid,
    ADD COLUMN owner_type varchar(24) NOT NULL DEFAULT 'dataset_asset',
    ADD CONSTRAINT ck_managed_retirement_source_owner CHECK (
        (owner_type = 'dataset_asset' AND dataset_item_id IS NOT NULL
            AND asset_id IS NOT NULL AND auxiliary_original_id IS NULL)
        OR (owner_type = 'dataset_auxiliary' AND dataset_item_id IS NULL
            AND asset_id IS NULL AND auxiliary_original_id IS NOT NULL
            AND creation_id IS NOT NULL AND creation_id = auxiliary_original_id)
    );
-- aap-statement-break
CREATE TABLE yoao_fixture_v1.fixture_original_targets (
    id uuid PRIMARY KEY,
    location_id uuid NOT NULL REFERENCES public.storage_locations(id) ON DELETE RESTRICT,
    bucket varchar(255) NOT NULL,
    generation bigint NOT NULL,
    storage_identity varchar(64) NOT NULL,
    store_receipt_sha256 varchar(64) NOT NULL,
    CONSTRAINT uq_fixture_original_target_identity UNIQUE (storage_identity),
    CONSTRAINT ck_fixture_original_target CHECK (
        generation >= 1 AND length(bucket) BETWEEN 1 AND 255
        AND storage_identity ~ '^[0-9a-f]{64}$'
        AND store_receipt_sha256 ~ '^[0-9a-f]{64}$'
    )
);
-- aap-statement-break
CREATE TABLE yoao_fixture_v1.fixture_runs (
    id uuid PRIMARY KEY,
    parent_run_id uuid REFERENCES yoao_fixture_v1.fixture_runs(id) ON DELETE RESTRICT,
    scope_key varchar(100),
    family varchar(20) NOT NULL,
    state varchar(20) NOT NULL DEFAULT 'open',
    original_target_id uuid REFERENCES yoao_fixture_v1.fixture_original_targets(id) ON DELETE RESTRICT,
    original_storage_identity varchar(64),
    original_store_receipt_sha256 varchar(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    sealed_at timestamptz,
    retired_at timestamptz,
    CONSTRAINT ck_fixture_run_family CHECK (family IN ('owned', 'screenshot', 'scale')),
    CONSTRAINT ck_fixture_run_parent_scope CHECK (
        (parent_run_id IS NULL AND scope_key IS NULL)
        OR (parent_run_id IS NOT NULL AND scope_key IS NOT NULL AND length(scope_key) > 0)
    ),
    CONSTRAINT ck_fixture_run_original_store CHECK (
        (original_target_id IS NULL AND original_storage_identity IS NULL
            AND original_store_receipt_sha256 IS NULL)
        OR (parent_run_id IS NULL AND original_target_id IS NOT NULL
            AND original_storage_identity IS NOT NULL
            AND original_store_receipt_sha256 IS NOT NULL
            AND original_storage_identity ~ '^[0-9a-f]{64}$'
            AND original_store_receipt_sha256 ~ '^[0-9a-f]{64}$')
    ),
    CONSTRAINT ck_fixture_run_state CHECK (
        (state = 'open' AND sealed_at IS NULL AND retired_at IS NULL)
        OR (state = 'sealing' AND sealed_at IS NOT NULL AND retired_at IS NULL)
        OR (state = 'retired' AND sealed_at IS NOT NULL AND retired_at IS NOT NULL)
    )
);
-- aap-statement-break
CREATE UNIQUE INDEX uq_fixture_run_active_parent_scope
ON yoao_fixture_v1.fixture_runs (parent_run_id, scope_key)
WHERE parent_run_id IS NOT NULL AND state <> 'retired';
-- aap-statement-break
CREATE TABLE yoao_fixture_v1.fixture_run_resources (
    run_id uuid NOT NULL REFERENCES yoao_fixture_v1.fixture_runs(id) ON DELETE RESTRICT,
    kind varchar(30) NOT NULL,
    resource_id uuid NOT NULL,
    logical_key varchar(255) NOT NULL,
    state varchar(20) NOT NULL DEFAULT 'reserved',
    creation_id uuid REFERENCES public.managed_original_creations(id) ON DELETE RESTRICT,
    admission_id uuid,
    storage_identity varchar(64),
    item_id uuid,
    item_fingerprint varchar(64),
    PRIMARY KEY (run_id, kind, resource_id),
    CONSTRAINT uq_fixture_run_resource_owner UNIQUE (kind, resource_id),
    CONSTRAINT uq_fixture_run_logical_resource UNIQUE (run_id, kind, logical_key),
    CONSTRAINT uq_fixture_creation_item UNIQUE (item_id),
    CONSTRAINT ck_fixture_run_logical_key CHECK (length(logical_key) > 0),
    CONSTRAINT ck_fixture_run_resource_kind CHECK (
        kind IN ('user', 'project', 'dataset', 'item', 'task', 'creation',
                 'auxiliary', 'annotation', 'registry', 'template', 'reclaim', 'invitation', 'audit')
    ),
    CONSTRAINT ck_fixture_run_resource_state CHECK (state IN ('reserved', 'published', 'retired')),
    CONSTRAINT ck_fixture_run_resource_creation CHECK (
        (kind = 'creation' AND creation_id IS NOT NULL AND creation_id = resource_id
            AND admission_id IS NOT NULL AND item_id IS NOT NULL
            AND item_fingerprint IS NOT NULL AND item_fingerprint ~ '^[0-9a-f]{64}$')
        OR (kind <> 'creation' AND creation_id IS NULL AND admission_id IS NULL
            AND item_id IS NULL AND item_fingerprint IS NULL)
    ),
    CONSTRAINT ck_fixture_run_resource_storage_identity CHECK (
        storage_identity IS NULL OR (kind = 'dataset' AND storage_identity ~ '^[0-9a-f]{64}$')
    )
);
-- aap-statement-break
CREATE UNIQUE INDEX uq_fixture_original_resource
ON yoao_fixture_v1.fixture_run_resources (resource_id)
WHERE kind IN ('creation', 'auxiliary');
-- aap-statement-break
CREATE TABLE yoao_fixture_v1.fixture_run_admissions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES yoao_fixture_v1.fixture_runs(id) ON DELETE RESTRICT,
    kind varchar(30) NOT NULL,
    state varchar(20) NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    CONSTRAINT uq_fixture_run_admission_run_id UNIQUE (run_id, id),
    CONSTRAINT ck_fixture_run_admission_state CHECK (
        (state IN ('active', 'unknown') AND settled_at IS NULL)
        OR (state = 'settled' AND settled_at IS NOT NULL)
    )
);
-- aap-statement-break
ALTER TABLE yoao_fixture_v1.fixture_run_resources
    ADD CONSTRAINT fk_fixture_resource_admission FOREIGN KEY (run_id, admission_id)
    REFERENCES yoao_fixture_v1.fixture_run_admissions(run_id, id) ON DELETE RESTRICT;
-- aap-statement-break
CREATE INDEX ix_fixture_run_admissions_run ON yoao_fixture_v1.fixture_run_admissions (run_id, state);
-- aap-statement-break
CREATE TABLE yoao_fixture_v1.fixture_image_work_intents (
    operation_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    admission_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    job_id uuid UNIQUE,
    generation bigint,
    state varchar(20) NOT NULL DEFAULT 'prepared',
    FOREIGN KEY (run_id, admission_id)
        REFERENCES yoao_fixture_v1.fixture_run_admissions(run_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_fixture_image_work_intent_state CHECK (
        (state = 'prepared' AND job_id IS NULL AND generation IS NULL)
        OR (state IN ('admitted', 'settled') AND job_id IS NOT NULL AND generation >= 1)
    )
);
-- aap-statement-break
CREATE TABLE yoao_fixture_v1.fixture_invitation_intents (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    admission_id uuid NOT NULL,
    project_id uuid NOT NULL,
    inviter_id uuid NOT NULL,
    email varchar(255) NOT NULL UNIQUE,
    invitation_id uuid UNIQUE,
    accepted_user_id uuid UNIQUE,
    state varchar(20) NOT NULL DEFAULT 'prepared',
    FOREIGN KEY (run_id, admission_id)
        REFERENCES yoao_fixture_v1.fixture_run_admissions(run_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_fixture_invitation_intent_state CHECK (
        (state = 'prepared' AND invitation_id IS NULL AND accepted_user_id IS NULL)
        OR (state = 'invited' AND invitation_id IS NOT NULL AND accepted_user_id IS NULL)
        OR (state = 'accepted' AND invitation_id IS NOT NULL AND accepted_user_id IS NOT NULL)
        OR (state = 'settled' AND (accepted_user_id IS NULL OR invitation_id IS NOT NULL))
    )
);
-- aap-statement-break
CREATE TABLE yoao_fixture_v1.fixture_auxiliary_intents (
    creation_id uuid PRIMARY KEY REFERENCES public.managed_original_creations(id) ON DELETE RESTRICT,
    run_id uuid NOT NULL,
    resource_kind varchar(30) NOT NULL DEFAULT 'auxiliary',
    admission_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    kind varchar(20) NOT NULL,
    source_contract jsonb NOT NULL,
    sha256 varchar(64) NOT NULL,
    size_bytes bigint NOT NULL,
    FOREIGN KEY (run_id, admission_id)
        REFERENCES yoao_fixture_v1.fixture_run_admissions(run_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (run_id, resource_kind, creation_id)
        REFERENCES yoao_fixture_v1.fixture_run_resources(run_id, kind, resource_id) ON DELETE RESTRICT,
    CONSTRAINT ck_fixture_auxiliary_kind CHECK (kind IN ('map', 'raw_lidar')),
    CONSTRAINT ck_fixture_auxiliary_resource_kind CHECK (resource_kind = 'auxiliary'),
    CONSTRAINT ck_fixture_auxiliary_contract CHECK (
        jsonb_typeof(source_contract) = 'object' AND octet_length(source_contract::text) <= 65536
    ),
    CONSTRAINT ck_fixture_auxiliary_content CHECK (
        sha256 ~ '^[0-9a-f]{64}$' AND size_bytes BETWEEN 0 AND 268435456
    )
);
-- aap-statement-break
CREATE TABLE yoao_fixture_v1.fixture_presentation_jobs (
    job_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES yoao_fixture_v1.fixture_runs(id) ON DELETE RESTRICT,
    project_id uuid NOT NULL,
    kind varchar(40) NOT NULL,
    state varchar(20) NOT NULL DEFAULT 'admitted',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_fixture_presentation_job_kind CHECK (kind IN ('batch_predict', 'export')),
    CONSTRAINT ck_fixture_presentation_job_state CHECK (state IN ('admitted', 'deleting', 'retired'))
);
-- aap-statement-break
CREATE TABLE yoao_fixture_v1.fixture_maintenance_windows (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES yoao_fixture_v1.fixture_runs(id) ON DELETE RESTRICT,
    location_id uuid NOT NULL REFERENCES public.storage_locations(id) ON DELETE RESTRICT,
    actor_id uuid NOT NULL,
    binding_generation bigint NOT NULL,
    storage_identity varchar(64) NOT NULL,
    revision bigint NOT NULL,
    initial_revision bigint NOT NULL,
    draining_revision bigint NOT NULL,
    stopped_revision bigint,
    resumed_revision bigint,
    state varchar(20) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    resumed_at timestamptz,
    CONSTRAINT ck_fixture_maintenance_evidence CHECK (
        binding_generation >= 1 AND initial_revision >= 1
        AND draining_revision = initial_revision + 1 AND storage_identity ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_fixture_maintenance_state CHECK (
        (state = 'draining' AND revision = draining_revision AND stopped_revision IS NULL
            AND resumed_revision IS NULL AND resumed_at IS NULL)
        OR (state = 'stopped' AND stopped_revision IS NOT NULL AND revision = stopped_revision
            AND stopped_revision = draining_revision + 1 AND resumed_revision IS NULL AND resumed_at IS NULL)
        OR (state = 'resumed' AND resumed_revision IS NOT NULL
            AND (stopped_revision IS NULL OR stopped_revision = draining_revision + 1)
            AND revision = resumed_revision AND resumed_revision = coalesce(stopped_revision, draining_revision) + 1
            AND resumed_at IS NOT NULL)
    )
);
-- aap-statement-break
CREATE UNIQUE INDEX uq_fixture_maintenance_active_location
ON yoao_fixture_v1.fixture_maintenance_windows (location_id) WHERE state <> 'resumed';
-- aap-statement-break
CREATE UNIQUE INDEX uq_fixture_maintenance_active_run
ON yoao_fixture_v1.fixture_maintenance_windows (run_id) WHERE state <> 'resumed';
-- aap-statement-break
REVOKE ALL ON TABLE yoao_fixture_v1.fixture_original_targets, yoao_fixture_v1.fixture_runs, yoao_fixture_v1.fixture_run_resources,
    yoao_fixture_v1.fixture_run_admissions, yoao_fixture_v1.fixture_auxiliary_intents,
    yoao_fixture_v1.fixture_invitation_intents, yoao_fixture_v1.fixture_image_work_intents,
    yoao_fixture_v1.fixture_presentation_jobs,
    yoao_fixture_v1.fixture_maintenance_windows FROM PUBLIC;
