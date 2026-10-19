-- Durable byte receipts are independent of image derivative jobs and buckets.
CREATE FUNCTION aap_assets_v1._accepted_image_input_prefix_valid(p_prefix text)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(
        p_prefix ~ '^accepted-inputs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g[1-9][0-9]*/a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/$',
        false
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_prefix_valid(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.asset_accepted_image_inputs (
    input_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    job_id uuid NOT NULL,
    task_id uuid NOT NULL,
    project_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'pending',
    task_version integer NOT NULL,
    task_source_generation bigint NOT NULL,
    dataset_item_id uuid,
    task_source_snapshot varchar(64) NOT NULL,
    source_owner_kind varchar(16) NOT NULL,
    source_owner_id uuid NOT NULL,
    source_generation bigint NOT NULL,
    source_snapshot varchar(64) NOT NULL,
    source_identity jsonb NOT NULL,
    source_facts jsonb NOT NULL,
    generation bigint NOT NULL DEFAULT 0,
    attempt_count integer NOT NULL DEFAULT 0,
    lease_owner uuid,
    lease_token uuid,
    lease_expires_at timestamptz,
    last_lease_token uuid,
    last_outcome varchar(24),
    blob_key varchar(2048),
    blob_version_id varchar(255),
    blob_etag varchar(255),
    byte_size bigint,
    sha256 varchar(64),
    error_code varchar(64),
    retire_after timestamptz,
    retention_checked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CONSTRAINT uq_asset_accepted_image_input_job_task UNIQUE (job_id, task_id),
    CONSTRAINT ck_asset_accepted_image_input_status CHECK (
        status IN ('pending', 'materializing', 'ready', 'failed', 'expired')
    ),
    CONSTRAINT ck_asset_accepted_image_input_task_version CHECK (task_version > 0),
    CONSTRAINT ck_asset_accepted_image_input_task_generation CHECK (
        task_source_generation > 0
    ),
    CONSTRAINT ck_asset_accepted_image_input_source_generation CHECK (
        source_generation > 0
    ),
    CONSTRAINT ck_asset_accepted_image_input_snapshots CHECK (
        task_source_snapshot ~ '^[0-9a-f]{64}$'
        AND source_snapshot ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_asset_accepted_image_input_owner CHECK (
        (dataset_item_id IS NULL AND source_owner_kind = 'task'
            AND source_owner_id = task_id)
        OR (dataset_item_id IS NOT NULL AND source_owner_kind = 'dataset_item'
            AND source_owner_id = dataset_item_id)
    ),
    CONSTRAINT ck_asset_accepted_image_input_identity CHECK (
        pg_catalog.jsonb_typeof(source_identity) = 'object'
        AND aap_assets_v1._media_object_identity_valid(source_identity)
        AND pg_catalog.jsonb_typeof(source_facts) = 'object'
        AND aap_assets_v1._image_source_data_valid(source_facts)
        AND source_facts #>> '{owner,kind}' = source_owner_kind
        AND source_facts #>> '{owner,id}' = source_owner_id::text
        AND source_facts ->> 'task_id' = task_id::text
        AND source_facts ->> 'project_id' = project_id::text
        AND source_facts ->> 'source_generation' = source_generation::text
        AND source_facts ->> 'source_snapshot' = source_snapshot
        AND aap_assets_v1._image_normalize_identity(source_facts -> 'object_identity')
            = aap_assets_v1._image_normalize_identity(source_identity)
        AND source_facts -> 'source_drifted' = 'false'::jsonb
        AND source_facts -> 'upload_ready' = 'true'::jsonb
    ),
    CONSTRAINT ck_asset_accepted_image_input_generation CHECK (
        generation >= 0 AND attempt_count >= 0
    ),
    CONSTRAINT ck_asset_accepted_image_input_lease CHECK (
        (lease_token IS NULL) = (lease_expires_at IS NULL)
        AND (lease_token IS NULL) = (lease_owner IS NULL)
        AND (status = 'materializing') = (lease_token IS NOT NULL)
    ),
    CONSTRAINT ck_asset_accepted_image_input_last_outcome CHECK (
        (last_lease_token IS NULL) = (last_outcome IS NULL)
        AND (last_outcome IS NULL OR last_outcome IN (
            'ready', 'failed_retryable', 'failed_terminal', 'lease_expired'
        ))
    ),
    CONSTRAINT ck_asset_accepted_image_input_blob CHECK (
        (status = 'ready'
            AND blob_key IS NOT NULL
            AND blob_version_id IS NOT NULL
            AND pg_catalog.char_length(blob_version_id) BETWEEN 1 AND 255
            AND blob_version_id <> 'null'
            AND blob_etag IS NOT NULL
            AND pg_catalog.char_length(blob_etag) BETWEEN 1 AND 255
            AND byte_size IS NOT NULL AND byte_size >= 0
            AND sha256 ~ '^[0-9a-f]{64}$'
            AND error_code IS NULL)
        OR
        (status <> 'ready'
            AND blob_key IS NULL
            AND blob_version_id IS NULL
            AND blob_etag IS NULL
            AND ((byte_size IS NULL) = (sha256 IS NULL)))
    ),
    CONSTRAINT ck_asset_accepted_image_input_error CHECK (
        (status = 'failed' AND error_code IS NOT NULL)
        OR (status = 'pending' AND (
            error_code IS NULL OR error_code IN (
                'storage_unavailable', 'timeout', 'source_unavailable', 'source_busy',
                'task_busy', 'input_busy', 'job_busy', 'reference_busy'
            )
        ))
        OR (status IN ('materializing', 'ready', 'expired') AND error_code IS NULL)
    )
);
-- aap-statement-break
CREATE INDEX ix_asset_accepted_image_inputs_claim
    ON public.asset_accepted_image_inputs(status, lease_expires_at, created_at, input_id);
-- aap-statement-break
CREATE INDEX ix_asset_accepted_image_inputs_job
    ON public.asset_accepted_image_inputs(job_id, task_id, actor_id);
-- aap-statement-break
REVOKE ALL ON TABLE public.asset_accepted_image_inputs FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.asset_accepted_image_input_attempts (
    input_id uuid NOT NULL
        REFERENCES public.asset_accepted_image_inputs(input_id) ON DELETE RESTRICT,
    generation bigint NOT NULL,
    lease_token uuid NOT NULL,
    prefix varchar(1024) NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'leased',
    lease_expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    completed_at timestamptz,
    PRIMARY KEY (input_id, generation),
    UNIQUE (prefix),
    CONSTRAINT ck_asset_accepted_image_input_attempt_generation CHECK (generation > 0),
    CONSTRAINT ck_asset_accepted_image_input_attempt_prefix CHECK (
        aap_assets_v1._accepted_image_input_prefix_valid(prefix)
    ),
    CONSTRAINT ck_asset_accepted_image_input_attempt_status CHECK (
        status IN ('leased', 'ready', 'failed', 'abandoned', 'expired')
    )
);
-- aap-statement-break
CREATE INDEX ix_asset_accepted_image_input_attempts_lease
    ON public.asset_accepted_image_input_attempts(input_id, status, lease_expires_at);
-- aap-statement-break
REVOKE ALL ON TABLE public.asset_accepted_image_input_attempts FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.asset_accepted_input_garbage (
    garbage_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    prefix varchar(1024) NOT NULL UNIQUE,
    reason varchar(40) NOT NULL CHECK (pg_catalog.char_length(reason) BETWEEN 1 AND 40),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    not_before timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    completed_at timestamptz,
    lease_owner uuid,
    lease_token uuid,
    lease_expires_at timestamptz,
    last_ack_token uuid,
    CONSTRAINT ck_asset_accepted_input_garbage_prefix CHECK (
        aap_assets_v1._accepted_image_input_prefix_valid(prefix)
    ),
    CONSTRAINT ck_asset_accepted_input_garbage_lease CHECK (
        (lease_token IS NULL) = (lease_expires_at IS NULL)
        AND (lease_token IS NULL) = (lease_owner IS NULL)
    )
);
-- aap-statement-break
CREATE INDEX ix_asset_accepted_input_garbage_due
    ON public.asset_accepted_input_garbage(not_before, lease_expires_at, garbage_id);
-- aap-statement-break
REVOKE ALL ON TABLE public.asset_accepted_input_garbage FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._queue_accepted_image_input_garbage(
    p_prefix text, p_reason text, p_not_before timestamptz
)
RETURNS void
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT aap_assets_v1._accepted_image_input_prefix_valid(p_prefix)
       OR p_reason IS NULL OR pg_catalog.char_length(p_reason) NOT BETWEEN 1 AND 40
       OR p_not_before IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid accepted input garbage scope';
    END IF;
    INSERT INTO public.asset_accepted_input_garbage(prefix, reason, not_before)
    VALUES (p_prefix, p_reason, p_not_before)
    ON CONFLICT (prefix) DO UPDATE
    SET not_before = LEAST(
        public.asset_accepted_input_garbage.not_before, EXCLUDED.not_before
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._queue_accepted_image_input_garbage(
    text, text, timestamptz
) FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.annotation_input_sources (
    annotation_id uuid NOT NULL
        REFERENCES public.annotations(id) ON DELETE CASCADE,
    input_id uuid NOT NULL,
    sha256 varchar(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (annotation_id, input_id)
);
-- aap-statement-break
CREATE INDEX ix_annotation_input_sources_input
    ON public.annotation_input_sources(input_id, annotation_id);
-- aap-statement-break
REVOKE ALL ON TABLE public.annotation_input_sources FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_image_input_immutable_scope()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'accepted image input receipts are retained';
    END IF;
    IF (NEW.input_id, NEW.job_id, NEW.task_id, NEW.project_id, NEW.actor_id,
        NEW.task_version, NEW.task_source_generation, NEW.dataset_item_id,
        NEW.task_source_snapshot, NEW.source_owner_kind, NEW.source_owner_id,
        NEW.source_generation, NEW.source_snapshot, NEW.source_identity,
        NEW.source_facts, NEW.created_at)
       IS DISTINCT FROM
       (OLD.input_id, OLD.job_id, OLD.task_id, OLD.project_id, OLD.actor_id,
        OLD.task_version, OLD.task_source_generation, OLD.dataset_item_id,
        OLD.task_source_snapshot, OLD.source_owner_kind, OLD.source_owner_id,
        OLD.source_generation, OLD.source_snapshot, OLD.source_identity,
        OLD.source_facts, OLD.created_at) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'accepted image input source identity is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_immutable_scope()
    FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_accepted_image_input_immutable_scope
BEFORE UPDATE OR DELETE ON public.asset_accepted_image_inputs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._accepted_image_input_immutable_scope();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_image_input_attempt_immutable_scope()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'accepted image input attempts are retained';
    END IF;
    IF (NEW.input_id, NEW.generation, NEW.lease_token, NEW.prefix, NEW.created_at)
       IS DISTINCT FROM
       (OLD.input_id, OLD.generation, OLD.lease_token, OLD.prefix, OLD.created_at) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'accepted image input attempt scope is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_attempt_immutable_scope()
    FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_accepted_image_input_attempt_immutable_scope
BEFORE UPDATE OR DELETE ON public.asset_accepted_image_input_attempts
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._accepted_image_input_attempt_immutable_scope();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_input_garbage_immutable_scope()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'accepted input garbage tombstones are retained';
    END IF;
    IF (NEW.garbage_id, NEW.prefix, NEW.reason, NEW.created_at)
       IS DISTINCT FROM (OLD.garbage_id, OLD.prefix, OLD.reason, OLD.created_at) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'accepted input garbage scope is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_input_garbage_immutable_scope()
    FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_accepted_input_garbage_immutable_scope
BEFORE UPDATE OR DELETE ON public.asset_accepted_input_garbage
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._accepted_input_garbage_immutable_scope();
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._annotation_input_source_immutable()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF (NEW.annotation_id, NEW.input_id, NEW.sha256, NEW.created_at)
       IS DISTINCT FROM (OLD.annotation_id, OLD.input_id, OLD.sha256, OLD.created_at) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'annotation input source evidence is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._annotation_input_source_immutable()
    FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_annotation_input_source_immutable
BEFORE UPDATE ON public.annotation_input_sources
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._annotation_input_source_immutable();
