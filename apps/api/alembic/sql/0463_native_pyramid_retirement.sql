-- Durable retirement ownership for ordinary native pyramid outputs.
--
-- A saved output survives its business asset/generation/job rows and has
-- exactly one retirement owner: this private ledger under aap_assets_v1.
-- Fixture-owned outputs keep their sealed fixture owner; outputs without a
-- native attempt receipt keep the legacy bucket-root lane.  No cleanup caller
-- can address an arbitrary prefix, invent a missing receipt or claim absence
-- without the exact prepared page, cursor and lease.
CREATE FUNCTION aap_assets_v1._native_pyramid_retirement_policy_valid(p_policy jsonb)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN pg_catalog.jsonb_typeof(p_policy) IS DISTINCT FROM 'object' THEN false
        WHEN pg_catalog.jsonb_typeof(p_policy -> 'gc_grace_hours') IS DISTINCT FROM 'number' THEN false
        WHEN pg_catalog.jsonb_typeof(p_policy -> 'url_expiry_seconds') IS DISTINCT FROM 'number' THEN false
        WHEN (p_policy ->> 'gc_grace_hours') !~ '^(0|[1-9][0-9]{0,9})$' THEN false
        WHEN (p_policy ->> 'url_expiry_seconds') !~ '^[1-9][0-9]{0,9}$' THEN false
        WHEN (p_policy ->> 'gc_grace_hours')::numeric > 2147483647 THEN false
        WHEN (p_policy ->> 'url_expiry_seconds')::numeric > 2147483647 THEN false
        ELSE true
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._native_pyramid_retirement_policy_valid(jsonb) FROM PUBLIC;
-- aap-statement-break
-- The saved gc grace and the signed-URL lifetime both have to elapse; the
-- policy is frozen at allocation so later binding changes cannot shorten it.
CREATE FUNCTION aap_assets_v1._native_pyramid_retirement_grace(p_policy jsonb)
RETURNS interval
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.make_interval(secs => GREATEST(
        (p_policy ->> 'gc_grace_hours')::numeric * 3600,
        (p_policy ->> 'url_expiry_seconds')::numeric
    )::double precision)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._native_pyramid_retirement_grace(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._native_pyramid_retirement_store_valid(p_store jsonb)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN pg_catalog.jsonb_typeof(p_store) IS DISTINCT FROM 'object' THEN false
        WHEN NOT aap_assets_v1._media_object_has_only(p_store, ARRAY['endpoint_url', 'bucket']) THEN false
        WHEN NOT (p_store ?& ARRAY['endpoint_url', 'bucket']) THEN false
        WHEN pg_catalog.jsonb_typeof(p_store -> 'endpoint_url') IS DISTINCT FROM 'string' THEN false
        WHEN pg_catalog.jsonb_typeof(p_store -> 'bucket') IS DISTINCT FROM 'string' THEN false
        WHEN pg_catalog.length(p_store ->> 'endpoint_url') NOT BETWEEN 8 AND 2048 THEN false
        WHEN (p_store ->> 'endpoint_url') !~ '^https?://[^[:space:]/?#@]+$' THEN false
        WHEN pg_catalog.strpos((p_store ->> 'endpoint_url'), chr(92)) > 0 THEN false
        WHEN pg_catalog.length(p_store ->> 'bucket') NOT BETWEEN 1 AND 255 THEN false
        WHEN (p_store ->> 'bucket') ~ '[[:space:][:cntrl:]/?#@%:]' THEN false
        WHEN pg_catalog.strpos((p_store ->> 'bucket'), chr(92)) > 0 THEN false
        ELSE true
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._native_pyramid_retirement_store_valid(jsonb) FROM PUBLIC;
-- aap-statement-break
-- The saved allocation is the only addressable scope.  Its prefix is the full
-- key prefix, never a bucket-root pattern, and its identity is recomputed from
-- the saved endpoint and bucket.
CREATE FUNCTION aap_assets_v1._native_pyramid_retirement_target_valid(
    p_ref jsonb, p_output uuid
)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN p_output IS NULL THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref) IS DISTINCT FROM 'object' THEN false
        WHEN NOT aap_assets_v1._media_object_has_only(p_ref, ARRAY[
            'schema', 'purpose', 'location_id', 'config_revision',
            'binding_generation', 'endpoint_url', 'bucket', 'key_prefix',
            'prefix', 'storage_identity'
        ]) THEN false
        WHEN NOT (p_ref ?& ARRAY[
            'schema', 'purpose', 'location_id', 'config_revision',
            'binding_generation', 'endpoint_url', 'bucket', 'key_prefix',
            'prefix', 'storage_identity'
        ]) THEN false
        WHEN p_ref -> 'schema' IS DISTINCT FROM '2'::jsonb THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'purpose') IS DISTINCT FROM 'string'
            OR p_ref ->> 'purpose' IS DISTINCT FROM 'media-cache' THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'location_id') IS DISTINCT FROM 'string'
            OR (p_ref ->> 'location_id') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'config_revision') IS DISTINCT FROM 'number'
            OR (p_ref ->> 'config_revision') !~ '^[1-9][0-9]{0,9}$'
            THEN false
        WHEN (p_ref ->> 'config_revision')::numeric > 2147483647 THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'binding_generation') IS DISTINCT FROM 'number'
            OR (p_ref ->> 'binding_generation') !~ '^[1-9][0-9]{0,18}$'
            THEN false
        WHEN (p_ref ->> 'binding_generation')::numeric > 9223372036854775807
            THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'endpoint_url') IS DISTINCT FROM 'string'
            OR pg_catalog.length(p_ref ->> 'endpoint_url') NOT BETWEEN 8 AND 2048
            OR (p_ref ->> 'endpoint_url') !~ '^https?://[^[:space:]/?#@]+$'
            OR pg_catalog.strpos((p_ref ->> 'endpoint_url'), chr(92)) > 0
            THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'bucket') IS DISTINCT FROM 'string'
            OR pg_catalog.length(p_ref ->> 'bucket') NOT BETWEEN 1 AND 255
            OR (p_ref ->> 'bucket') ~ '[[:space:][:cntrl:]/?#@%:]'
            OR pg_catalog.strpos((p_ref ->> 'bucket'), chr(92)) > 0
            THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'key_prefix') IS DISTINCT FROM 'string'
            OR pg_catalog.octet_length(p_ref ->> 'key_prefix') > 436
            OR pg_catalog.strpos((p_ref ->> 'key_prefix'), chr(92)) > 0
            THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'prefix') IS DISTINCT FROM 'string'
            OR pg_catalog.length(p_ref ->> 'prefix') NOT BETWEEN 1 AND 2048
            OR (p_ref ->> 'prefix') IS DISTINCT FROM
                COALESCE(p_ref ->> 'key_prefix', '')
                || 'image-pyramids/objects/' || p_output::text || '/'
            THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'storage_identity') IS DISTINCT FROM 'string'
            OR (p_ref ->> 'storage_identity') !~ '^[0-9a-f]{64}$'
            THEN false
        WHEN (p_ref ->> 'storage_identity') IS DISTINCT FROM pg_catalog.encode(
            pg_catalog.sha256(pg_catalog.convert_to(
                pg_catalog.jsonb_build_array(
                    pg_catalog.rtrim(p_ref ->> 'endpoint_url', '/'),
                    p_ref ->> 'bucket'
                )::text, 'UTF8'
            )), 'hex'
        ) THEN false
        ELSE true
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._native_pyramid_retirement_target_valid(jsonb, uuid) FROM PUBLIC;
-- aap-statement-break
-- Recovered facts are immutable and retained as the durable obligation; the
-- output row itself has no business foreign key and survives deletion.
CREATE TABLE aap_assets_v1.native_pyramid_retirement_targets (
    output_id uuid PRIMARY KEY
        REFERENCES public.image_pyramid_outputs(id) ON DELETE RESTRICT,
    asset_id uuid NOT NULL,
    generation integer NOT NULL CHECK (generation > 0),
    gate_revision integer NOT NULL CHECK (gate_revision > 0),
    location_id uuid NOT NULL,
    config_revision integer NOT NULL CHECK (config_revision > 0),
    binding_generation bigint NOT NULL CHECK (binding_generation > 0),
    endpoint_url text NOT NULL CHECK (
        pg_catalog.length(endpoint_url) BETWEEN 8 AND 2048
        AND endpoint_url ~ '^https?://[^[:space:]/?#@]+$'
        AND pg_catalog.strpos(endpoint_url, chr(92)) = 0
    ),
    bucket text NOT NULL CHECK (
        pg_catalog.length(bucket) BETWEEN 1 AND 255
        AND bucket !~ '[[:space:][:cntrl:]/?#@%:]'
        AND pg_catalog.strpos(bucket, chr(92)) = 0
    ),
    key_prefix text NOT NULL CHECK (
        pg_catalog.octet_length(key_prefix) <= 436
        AND pg_catalog.strpos(key_prefix, chr(92)) = 0
    ),
    prefix text NOT NULL,
    storage_identity text NOT NULL CHECK (storage_identity ~ '^[0-9a-f]{64}$'),
    policy jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(policy) = 'object'
        AND pg_catalog.jsonb_typeof(policy -> 'gc_grace_hours') = 'number'
        AND pg_catalog.jsonb_typeof(policy -> 'url_expiry_seconds') = 'number'
    ),
    gc_grace_hours integer NOT NULL CHECK (gc_grace_hours BETWEEN 0 AND 2147483647),
    url_expiry_seconds integer NOT NULL CHECK (url_expiry_seconds BETWEEN 1 AND 2147483647),
    registered_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (prefix = key_prefix || 'image-pyramids/objects/' || output_id::text || '/'),
    CHECK (policy ->> 'gc_grace_hours' = gc_grace_hours::text),
    CHECK (policy ->> 'url_expiry_seconds' = url_expiry_seconds::text)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.native_pyramid_retirement_targets FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_assets_v1.native_pyramid_retirements (
    output_id uuid PRIMARY KEY
        REFERENCES aap_assets_v1.native_pyramid_retirement_targets(output_id) ON DELETE RESTRICT,
    not_before timestamptz NOT NULL,
    after_operation_id uuid,
    lease_owner uuid,
    lease_token uuid,
    lease_expires_at timestamptz,
    closed_at timestamptz,
    registered_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (closed_at IS NULL OR (
        lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
    )),
    CHECK (lease_token IS NULL OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.native_pyramid_retirements FROM PUBLIC;
-- aap-statement-break
CREATE INDEX ix_native_pyramid_retirement_targets_store
    ON aap_assets_v1.native_pyramid_retirement_targets(endpoint_url, bucket);
-- aap-statement-break
CREATE INDEX ix_native_pyramid_retirements_open
    ON aap_assets_v1.native_pyramid_retirements(not_before, output_id)
    WHERE closed_at IS NULL;
-- aap-statement-break
CREATE TABLE aap_assets_v1.native_pyramid_retirement_pages (
    page_id uuid PRIMARY KEY,
    output_id uuid NOT NULL
        REFERENCES aap_assets_v1.native_pyramid_retirements(output_id) ON DELETE RESTRICT,
    lease_token uuid NOT NULL,
    after_operation_id uuid,
    gate_revision integer NOT NULL CHECK (gate_revision > 0),
    terminal boolean NOT NULL,
    member_ids uuid[] NOT NULL,
    state text NOT NULL DEFAULT 'prepared' CHECK (
        state IN ('prepared', 'members_absent', 'prefix_absent', 'closed_without_ack')
    ),
    prepared_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    settled_at timestamptz,
    CHECK ((state = 'prepared') = (settled_at IS NULL)),
    CHECK (pg_catalog.cardinality(member_ids) <= 100),
    CHECK (terminal = (pg_catalog.cardinality(member_ids) = 0))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.native_pyramid_retirement_pages FROM PUBLIC;
-- aap-statement-break
CREATE INDEX ix_native_pyramid_retirement_pages_open
    ON aap_assets_v1.native_pyramid_retirement_pages(output_id)
    WHERE state = 'prepared';
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_native_pyramid_retirement_target()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '23514',
        MESSAGE = 'native_pyramid_retirement_target_retained';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_native_pyramid_retirement_target() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER native_pyramid_retirement_target_retained
BEFORE UPDATE OR DELETE ON aap_assets_v1.native_pyramid_retirement_targets
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_native_pyramid_retirement_target();
-- aap-statement-break
-- A prepared page owns its exact member IDs and cursor until it is settled;
-- only the physical executor can settle it, and only forwards.
CREATE FUNCTION aap_assets_v1.guard_native_pyramid_retirement_page()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'native_pyramid_retirement_page_retained';
    END IF;
    IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
    IF (NEW.page_id, NEW.output_id, NEW.lease_token, NEW.after_operation_id,
        NEW.gate_revision, NEW.terminal, NEW.member_ids, NEW.prepared_at)
       IS DISTINCT FROM
       (OLD.page_id, OLD.output_id, OLD.lease_token, OLD.after_operation_id,
        OLD.gate_revision, OLD.terminal, OLD.member_ids, OLD.prepared_at) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'native_pyramid_retirement_page_immutable';
    END IF;
    IF OLD.state <> 'prepared' THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'native_pyramid_retirement_page_settled';
    END IF;
    IF NEW.state NOT IN ('members_absent', 'prefix_absent', 'closed_without_ack') THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'native_pyramid_retirement_page_state';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_native_pyramid_retirement_page() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER native_pyramid_retirement_page_guard
BEFORE UPDATE OR DELETE ON aap_assets_v1.native_pyramid_retirement_pages
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_native_pyramid_retirement_page();
-- aap-statement-break
-- The exact member receipts a prepared page addresses, in durable cursor order.
CREATE FUNCTION aap_assets_v1._native_pyramid_retirement_page_items(
    p_output uuid, p_member_ids uuid[]
)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'operation_id', member.operation_id,
        'relative_key', member.relative_key,
        'sha256', member.sha256,
        'size_bytes', member.size_bytes,
        'etag', member.etag,
        'version_id', member.version_id
    ) ORDER BY requested.ordinality), '[]'::jsonb)
    FROM pg_catalog.unnest(p_member_ids) WITH ORDINALITY AS requested(operation_id, ordinality)
    JOIN aap_assets_v1.image_pyramid_native_members AS member
      ON member.operation_id = requested.operation_id
     AND member.output_id = p_output
     AND member.state = 'verified'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._native_pyramid_retirement_page_items(uuid, uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._native_pyramid_retirement_claimable(p_output uuid)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM public.image_pyramid_outputs AS output
        JOIN aap_assets_v1.native_pyramid_retirements AS retirement
          ON retirement.output_id = output.id
        WHERE output.id = p_output
          AND output.state IN ('building', 'ready', 'retiring')
          AND retirement.closed_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM aap_assets_v1.image_pyramid_native_attempts AS attempt
              JOIN aap_assets_v1.fixture_native_media_jobs AS fixture
                ON fixture.family = 'image' AND fixture.job_id = attempt.job_id
              WHERE attempt.output_id = output.id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM aap_assets_v1.fixture_media_births AS birth
              WHERE birth.owner_kind = 'pyramid_output' AND birth.owner_id = output.id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM aap_assets_v1.image_pyramid_native_members AS member
              WHERE member.output_id = output.id
                AND (member.state NOT IN ('verified', 'not_written')
                     OR (member.state = 'verified' AND (
                         member.etag IS NULL
                         OR member.etag !~ '^"[^"]+"$'
                         OR pg_catalog.length(member.etag) > 1024
                         OR (member.version_id IS NOT NULL
                             AND pg_catalog.length(member.version_id) > 1024))))
          )
          -- A published inventory is the object history; every listed object
          -- must still have its exact immutable receipt and vice versa.
          AND (
              output.inventory IS NULL
              OR (
                  pg_catalog.jsonb_typeof(output.inventory) = 'object'
                  AND pg_catalog.jsonb_typeof(output.inventory -> 'objects') = 'object'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_catalog.jsonb_each(
                          output.inventory -> 'objects'
                      ) AS inventory_object(key, receipt)
                      LEFT JOIN aap_assets_v1.image_pyramid_native_members AS member
                        ON member.output_id = output.id
                       AND member.state = 'verified'
                       AND (output.storage_ref ->> 'prefix') || member.relative_key
                           = inventory_object.key
                       AND member.sha256 = inventory_object.receipt ->> 'sha256'
                       AND pg_catalog.to_jsonb(member.size_bytes)
                           = inventory_object.receipt -> 'size_bytes'
                      WHERE pg_catalog.left(
                                inventory_object.key,
                                pg_catalog.length(output.storage_ref ->> 'prefix')
                            ) IS DISTINCT FROM (output.storage_ref ->> 'prefix')
                         OR NOT aap_assets_v1._media_object_has_only(
                                inventory_object.receipt,
                                ARRAY['sha256', 'size_bytes']
                            )
                         OR NOT (inventory_object.receipt ?& ARRAY['sha256', 'size_bytes'])
                         OR member.operation_id IS NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM aap_assets_v1.image_pyramid_native_members AS member
                      WHERE member.output_id = output.id
                        AND member.state = 'verified'
                        AND NOT (output.inventory -> 'objects' ? (
                            (output.storage_ref ->> 'prefix') || member.relative_key
                        ))
                  )
              )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM public.storage_operations AS operation
              WHERE operation.location_id = (
                        SELECT target.location_id
                        FROM aap_assets_v1.native_pyramid_retirement_targets AS target
                        WHERE target.output_id = output.id
                    )
                AND operation.purpose IN (
                    'image_pyramid_' || pg_catalog.replace(output.id::text, '-', ''),
                    'image_pyramid_retirement_' || pg_catalog.replace(output.id::text, '-', '')
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM aap_assets_v1.native_pyramid_retirement_pages AS page
              WHERE page.output_id = output.id AND page.state = 'prepared'
          )
          -- The legacy bucket-root lane must never share this exact prefix.
          AND NOT EXISTS (
              SELECT 1
              FROM public.asset_image_garbage AS garbage
              JOIN aap_assets_v1.native_pyramid_retirement_targets AS target
                ON target.output_id = output.id
              WHERE garbage.prefix = target.prefix
          )
          AND NOT EXISTS (
              SELECT 1
              FROM public.image_pyramid_generations AS generation
              WHERE generation.output_id = output.id
                AND (
                    generation.status IN ('pending', 'building')
                    OR (generation.status = 'ready' AND EXISTS (
                        SELECT 1
                        FROM public.image_pyramid_assets AS asset
                        WHERE asset.id = generation.asset_id
                          AND generation.generation IN (
                              asset.active_generation, asset.building_generation
                          )
                    ))
                )
          )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._native_pyramid_retirement_claimable(uuid) FROM PUBLIC;
-- aap-statement-break
-- Allocation itself registers the obligation from the live generation policy;
-- the output's full saved reference and the attempt facts must agree exactly.
CREATE FUNCTION aap_assets_v1.record_native_pyramid_retirement()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_ref jsonb;
    v_policy jsonb;
    v_grace interval;
BEGIN
    SELECT output.* INTO v_output
    FROM public.image_pyramid_outputs AS output
    WHERE output.id = NEW.output_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'native_pyramid_retirement_output_missing';
    END IF;
    -- A sealed fixture owner retires its own outputs; ordinary producers that
    -- merely read a fixture original are not fixture-owned outputs.
    IF EXISTS (
        SELECT 1
        FROM aap_assets_v1.fixture_native_media_jobs AS fixture
        WHERE fixture.family = 'image' AND fixture.job_id = NEW.job_id
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.fixture_media_births AS birth
        WHERE birth.owner_kind = 'pyramid_output' AND birth.owner_id = NEW.output_id
    ) THEN
        RETURN NEW;
    END IF;
    IF v_output.asset_id IS DISTINCT FROM NEW.asset_id
       OR v_output.generation IS DISTINCT FROM NEW.generation
       OR v_output.lease_token IS DISTINCT FROM NEW.lease_token THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'native_pyramid_retirement_attempt_mismatch';
    END IF;
    SELECT generation.* INTO v_generation
    FROM public.image_pyramid_generations AS generation
    WHERE generation.asset_id = NEW.asset_id
      AND generation.generation = NEW.generation;
    IF NOT FOUND
       OR v_generation.job_id IS DISTINCT FROM NEW.job_id
       OR v_generation.job_generation IS DISTINCT FROM NEW.job_generation
       OR v_generation.source_generation IS DISTINCT FROM NEW.source_generation
       OR v_generation.source_snapshot IS DISTINCT FROM NEW.source_snapshot THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'native_pyramid_retirement_generation_mismatch';
    END IF;
    v_ref := v_output.storage_ref;
    IF NOT aap_assets_v1._native_pyramid_retirement_target_valid(v_ref, NEW.output_id) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'native_pyramid_retirement_target_invalid';
    END IF;
    v_policy := v_generation.policy;
    IF NOT aap_assets_v1._native_pyramid_retirement_policy_valid(v_policy) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'native_pyramid_retirement_policy_unknown';
    END IF;
    v_grace := aap_assets_v1._native_pyramid_retirement_grace(v_policy);
    INSERT INTO aap_assets_v1.native_pyramid_retirement_targets(
        output_id, asset_id, generation, gate_revision, location_id,
        config_revision, binding_generation, endpoint_url, bucket, key_prefix,
        prefix, storage_identity, policy, gc_grace_hours, url_expiry_seconds
    ) VALUES (
        NEW.output_id, NEW.asset_id, NEW.generation, NEW.gate_revision,
        (v_ref ->> 'location_id')::uuid,
        (v_ref ->> 'config_revision')::integer,
        (v_ref ->> 'binding_generation')::bigint,
        v_ref ->> 'endpoint_url', v_ref ->> 'bucket', v_ref ->> 'key_prefix',
        v_ref ->> 'prefix', v_ref ->> 'storage_identity', v_policy,
        (v_policy ->> 'gc_grace_hours')::integer,
        (v_policy ->> 'url_expiry_seconds')::integer
    ) ON CONFLICT (output_id) DO NOTHING;
    INSERT INTO aap_assets_v1.native_pyramid_retirements(
        output_id, not_before
    ) VALUES (
        NEW.output_id, pg_catalog.clock_timestamp() + v_grace
    ) ON CONFLICT (output_id) DO NOTHING;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_native_pyramid_retirement() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER native_pyramid_retirement_registration
AFTER INSERT ON aap_assets_v1.image_pyramid_native_attempts
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.record_native_pyramid_retirement();
-- aap-statement-break
-- Business deletion must not erase the grace that was still owed to the last
-- reader.  The frozen policy stays in the private target; a stale generation
-- only advances the durable deadline.
CREATE FUNCTION aap_assets_v1.preserve_native_pyramid_retirement_grace()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_policy jsonb;
    v_grace interval;
BEGIN
    IF OLD.output_id IS NULL THEN RETURN OLD; END IF;
    SELECT target.policy INTO v_policy
    FROM aap_assets_v1.native_pyramid_retirement_targets AS target
    WHERE target.output_id = OLD.output_id;
    -- The allocation-time policy is the durable one; a later generation write
    -- must not shorten the grace that was already promised.
    IF NOT aap_assets_v1._native_pyramid_retirement_policy_valid(v_policy) THEN
        RETURN OLD;
    END IF;
    v_grace := aap_assets_v1._native_pyramid_retirement_grace(v_policy);
    UPDATE aap_assets_v1.native_pyramid_retirements AS retirement
    SET not_before = GREATEST(
            retirement.not_before,
            COALESCE(OLD.last_accessed_at, pg_catalog.clock_timestamp()) + v_grace
        ),
        updated_at = pg_catalog.clock_timestamp()
    WHERE retirement.output_id = OLD.output_id
      AND retirement.closed_at IS NULL;
    RETURN OLD;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.preserve_native_pyramid_retirement_grace() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER native_pyramid_retirement_grace
BEFORE DELETE ON public.image_pyramid_generations
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.preserve_native_pyramid_retirement_grace();
-- aap-statement-break
-- Adopt only outputs whose surviving attempt, generation and saved reference
-- agree.  Anything unknown (missing policy, missing receipt, foreign owner)
-- stays with its legacy owner and is never fabricated from current bindings.
INSERT INTO aap_assets_v1.native_pyramid_retirement_targets(
    output_id, asset_id, generation, gate_revision, location_id,
    config_revision, binding_generation, endpoint_url, bucket, key_prefix,
    prefix, storage_identity, policy, gc_grace_hours, url_expiry_seconds
)
SELECT
    output.id, attempt.asset_id, attempt.generation, attempt.gate_revision,
    (output.storage_ref ->> 'location_id')::uuid,
    (output.storage_ref ->> 'config_revision')::integer,
    (output.storage_ref ->> 'binding_generation')::bigint,
    output.storage_ref ->> 'endpoint_url',
    output.storage_ref ->> 'bucket',
    output.storage_ref ->> 'key_prefix',
    output.storage_ref ->> 'prefix',
    output.storage_ref ->> 'storage_identity',
    generation.policy,
    (generation.policy ->> 'gc_grace_hours')::integer,
    (generation.policy ->> 'url_expiry_seconds')::integer
FROM public.image_pyramid_outputs AS output
JOIN aap_assets_v1.image_pyramid_native_attempts AS attempt
  ON attempt.output_id = output.id
JOIN public.image_pyramid_generations AS generation
  ON generation.asset_id = output.asset_id
 AND generation.generation = output.generation
JOIN public.image_pyramid_assets AS asset
  ON asset.id = output.asset_id
WHERE output.state IN ('building', 'ready')
  AND output.lease_token = attempt.lease_token
  AND attempt.asset_id = output.asset_id
  AND attempt.generation = output.generation
  AND generation.output_id IS NOT DISTINCT FROM output.id
  AND generation.job_id = attempt.job_id
  AND generation.job_generation = attempt.job_generation
  AND generation.source_generation = attempt.source_generation
  AND generation.source_snapshot = attempt.source_snapshot
  AND asset.profile_version = attempt.profile_version
  AND aap_assets_v1._native_pyramid_retirement_target_valid(output.storage_ref, output.id)
  AND aap_assets_v1._native_pyramid_retirement_policy_valid(generation.policy)
  AND NOT EXISTS (
      SELECT 1
      FROM aap_assets_v1.fixture_native_media_jobs AS fixture
      WHERE fixture.family = 'image' AND fixture.job_id = attempt.job_id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM aap_assets_v1.fixture_media_births AS birth
      WHERE birth.owner_kind = 'pyramid_output' AND birth.owner_id = output.id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM aap_assets_v1.native_pyramid_retirement_targets AS existing
      WHERE existing.output_id = output.id
  )
ON CONFLICT (output_id) DO NOTHING;
-- aap-statement-break
INSERT INTO aap_assets_v1.native_pyramid_retirements(output_id, not_before)
SELECT
    target.output_id,
    GREATEST(
        target.registered_at,
        COALESCE(generation.last_accessed_at, output.updated_at, target.registered_at)
            + aap_assets_v1._native_pyramid_retirement_grace(target.policy)
    )
FROM aap_assets_v1.native_pyramid_retirement_targets AS target
JOIN public.image_pyramid_outputs AS output ON output.id = target.output_id
LEFT JOIN public.image_pyramid_generations AS generation
  ON generation.asset_id = target.asset_id
 AND generation.generation = target.generation
WHERE NOT EXISTS (
    SELECT 1
    FROM aap_assets_v1.native_pyramid_retirements AS retirement
    WHERE retirement.output_id = target.output_id
)
ON CONFLICT (output_id) DO NOTHING;
-- aap-statement-break
-- One caller-supplied store selects the lane; the retired target is always the
-- saved allocation, never the current purpose binding.  Active references and
-- unknown writes remain barriers, and live maintenance refuses new work.
CREATE FUNCTION aap_assets_v1.claim_native_pyramid_retirement(
    p_worker_id uuid, p_store jsonb, p_lease_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_endpoint text;
    v_bucket text;
    v_candidate record;
    v_retirement aap_assets_v1.native_pyramid_retirements%ROWTYPE;
    v_target aap_assets_v1.native_pyramid_retirement_targets%ROWTYPE;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_scope record;
    v_gate record;
    v_lease_expires timestamptz;
    v_token uuid;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_not_before timestamptz;
    v_last_accessed timestamptz;
    v_live boolean;
    v_skip boolean;
    v_output_state text;
    v_ref jsonb;
BEGIN
    IF p_worker_id IS NULL OR p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 30 AND 3600
       OR NOT aap_assets_v1._native_pyramid_retirement_store_valid(p_store) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_endpoint := p_store ->> 'endpoint_url';
    v_bucket := p_store ->> 'bucket';
    FOR v_candidate IN
        SELECT retirement.output_id
        FROM aap_assets_v1.native_pyramid_retirements AS retirement
        JOIN aap_assets_v1.native_pyramid_retirement_targets AS target
          ON target.output_id = retirement.output_id
        WHERE target.endpoint_url = v_endpoint
          AND target.bucket = v_bucket
          AND retirement.closed_at IS NULL
          AND retirement.not_before <= v_now
          AND (retirement.lease_expires_at IS NULL
               OR retirement.lease_expires_at <= v_now)
          AND aap_assets_v1._native_pyramid_retirement_claimable(retirement.output_id)
        ORDER BY retirement.not_before, retirement.output_id
        LIMIT 20
        FOR UPDATE OF retirement SKIP LOCKED
    LOOP
        v_skip := false;
        BEGIN
            SELECT retirement.* INTO v_retirement
            FROM aap_assets_v1.native_pyramid_retirements AS retirement
            WHERE retirement.output_id = v_candidate.output_id
            FOR UPDATE;
            IF NOT FOUND OR v_retirement.closed_at IS NOT NULL THEN
                v_skip := true;
            END IF;
            IF NOT v_skip THEN
                SELECT target.* INTO v_target
                FROM aap_assets_v1.native_pyramid_retirement_targets AS target
                WHERE target.output_id = v_candidate.output_id;
                IF NOT FOUND THEN v_skip := true; END IF;
                IF NOT v_skip THEN
                    SELECT output.* INTO v_output
                    FROM public.image_pyramid_outputs AS output
                    WHERE output.id = v_candidate.output_id
                    FOR UPDATE NOWAIT;
                    IF NOT FOUND THEN v_skip := true; END IF;
                END IF;
            END IF;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            v_skip := true;
        END;
        CONTINUE WHEN v_skip;
        CONTINUE WHEN v_target.endpoint_url IS DISTINCT FROM v_endpoint
                    OR v_target.bucket IS DISTINCT FROM v_bucket;
        -- A new purpose binding must never redirect this saved allocation.
        v_ref := pg_catalog.jsonb_build_object(
            'schema', 2,
            'purpose', 'media-cache',
            'location_id', v_target.location_id::text,
            'config_revision', v_target.config_revision,
            'binding_generation', v_target.binding_generation,
            'endpoint_url', v_target.endpoint_url,
            'bucket', v_target.bucket,
            'key_prefix', v_target.key_prefix,
            'prefix', v_target.prefix,
            'storage_identity', v_target.storage_identity
        );
        IF v_output.storage_ref IS DISTINCT FROM v_ref THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        CONTINUE WHEN v_output.state NOT IN ('building', 'ready', 'retiring');
        CONTINUE WHEN NOT aap_assets_v1._native_pyramid_retirement_claimable(
            v_target.output_id);
        v_live := false;
        v_skip := false;
        BEGIN
            FOR v_scope IN
                SELECT generation.asset_id, generation.generation,
                       generation.status
                FROM public.image_pyramid_generations AS generation
                WHERE generation.output_id = v_target.output_id
                ORDER BY generation.asset_id, generation.generation
                FOR UPDATE NOWAIT
            LOOP
                IF v_scope.status IN ('pending', 'building') THEN
                    -- A building generation is a retained reference even when
                    -- its lease looks expired: only the owner can settle it.
                    v_live := true;
                ELSIF v_scope.status = 'ready' THEN
                    IF EXISTS (
                        SELECT 1
                        FROM public.image_pyramid_assets AS asset
                        WHERE asset.id = v_scope.asset_id
                          AND v_scope.generation IN (
                              asset.active_generation, asset.building_generation
                          )
                    ) THEN
                        v_live := true;
                    END IF;
                END IF;
            END LOOP;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            v_skip := true;
        END;
        CONTINUE WHEN v_skip OR v_live;
        v_not_before := v_retirement.not_before;
        SELECT generation.last_accessed_at INTO v_last_accessed
        FROM public.image_pyramid_generations AS generation
        WHERE generation.asset_id = v_target.asset_id
          AND generation.generation = v_target.generation;
        IF FOUND AND v_last_accessed IS NOT NULL THEN
            v_not_before := GREATEST(
                v_not_before,
                v_last_accessed
                    + aap_assets_v1._native_pyramid_retirement_grace(v_target.policy)
            );
        END IF;
        IF v_not_before > v_now THEN
            UPDATE aap_assets_v1.native_pyramid_retirements AS retirement
            SET not_before = v_not_before, updated_at = v_now
            WHERE retirement.output_id = v_target.output_id;
            CONTINUE;
        END IF;
        SELECT gate.state, gate.revision INTO v_gate
        FROM public.storage_maintenance_gates AS gate
        WHERE gate.location_id = v_target.location_id
        FOR SHARE;
        IF NOT FOUND OR v_gate.state <> 'active' THEN
            CONTINUE;
        END IF;
        UPDATE public.image_pyramid_outputs AS output
        SET state = 'retiring', updated_at = v_now
        WHERE output.id = v_target.output_id
          AND output.state IN ('building', 'ready');
        IF FOUND AND v_output.state = 'ready' THEN
            -- Stop new reads first, then outlive any URL issued before this fence.
            -- last_accessed_at alone is insufficient because reads throttle it.
            v_not_before := GREATEST(
                v_not_before,
                pg_catalog.clock_timestamp() + pg_catalog.make_interval(
                    secs => (v_target.policy ->> 'url_expiry_seconds')::integer
                )
            );
            UPDATE aap_assets_v1.native_pyramid_retirements AS retirement
            SET not_before = v_not_before, updated_at = pg_catalog.clock_timestamp()
            WHERE retirement.output_id = v_target.output_id;
            CONTINUE;
        ELSIF NOT FOUND THEN
            SELECT output.state INTO v_output_state
            FROM public.image_pyramid_outputs AS output
            WHERE output.id = v_target.output_id;
            CONTINUE WHEN v_output_state IS DISTINCT FROM 'retiring';
        END IF;
        v_token := pg_catalog.gen_random_uuid();
        v_lease_expires := v_now
            + pg_catalog.make_interval(secs => p_lease_seconds);
        UPDATE aap_assets_v1.native_pyramid_retirements AS retirement
        SET lease_owner = p_worker_id, lease_token = v_token,
            lease_expires_at = v_lease_expires, not_before = v_not_before,
            updated_at = v_now
        WHERE retirement.output_id = v_target.output_id
          AND retirement.closed_at IS NULL;
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'claim', pg_catalog.jsonb_build_object(
                'output_id', v_target.output_id,
                'lease_token', v_token,
                'lease_expires_at', v_lease_expires,
                'storage_ref', v_ref,
                'after_operation_id', v_retirement.after_operation_id
            )
        ), NULL);
    END LOOP;
    RETURN aap_assets_v1._image_reply(
        pg_catalog.jsonb_build_object('claim', NULL), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_native_pyramid_retirement(uuid, jsonb, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.prepare_native_pyramid_retirement_page(
    p_output_id uuid, p_lease_token uuid, p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_retirement aap_assets_v1.native_pyramid_retirements%ROWTYPE;
    v_target aap_assets_v1.native_pyramid_retirement_targets%ROWTYPE;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_page aap_assets_v1.native_pyramid_retirement_pages%ROWTYPE;
    v_member record;
    v_ref jsonb;
    v_member_ids uuid[] := ARRAY[]::uuid[];
    v_after uuid;
    v_page_id uuid;
    v_collected integer := 0;
    v_batch integer;
    v_gate record;
    v_early_skip boolean := false;
    v_missing boolean := false;
    v_busy boolean := false;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_output_id IS NULL OR p_lease_token IS NULL OR p_page_size IS NULL
       OR p_page_size NOT BETWEEN 1 AND 100 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT retirement.* INTO v_retirement
        FROM aap_assets_v1.native_pyramid_retirements AS retirement
        WHERE retirement.output_id = p_output_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND OR v_retirement.closed_at IS NOT NULL THEN
            v_missing := true;
        END IF;
        IF NOT v_missing THEN
            SELECT target.* INTO v_target
            FROM aap_assets_v1.native_pyramid_retirement_targets AS target
            WHERE target.output_id = p_output_id;
            IF NOT FOUND THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            SELECT output.* INTO v_output
            FROM public.image_pyramid_outputs AS output
            WHERE output.id = p_output_id
            FOR UPDATE NOWAIT;
            IF NOT FOUND THEN v_missing := true; END IF;
        END IF;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        v_busy := true;
    END;
    IF v_busy THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END IF;
    IF v_missing OR v_retirement.lease_token IS DISTINCT FROM p_lease_token
       OR v_retirement.lease_expires_at IS NULL
       OR v_retirement.lease_expires_at <= v_now THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    IF v_output.state IS DISTINCT FROM 'retiring' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    v_ref := pg_catalog.jsonb_build_object(
        'schema', 2,
        'purpose', 'media-cache',
        'location_id', v_target.location_id::text,
        'config_revision', v_target.config_revision,
        'binding_generation', v_target.binding_generation,
        'endpoint_url', v_target.endpoint_url,
        'bucket', v_target.bucket,
        'key_prefix', v_target.key_prefix,
        'prefix', v_target.prefix,
        'storage_identity', v_target.storage_identity
    );
    IF v_output.storage_ref IS DISTINCT FROM v_ref THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    SELECT page.* INTO v_page
    FROM aap_assets_v1.native_pyramid_retirement_pages AS page
    WHERE page.output_id = p_output_id AND page.state = 'prepared'
    ORDER BY page.prepared_at, page.page_id
    LIMIT 1
    FOR UPDATE;
    IF FOUND THEN
        IF v_page.lease_token IS DISTINCT FROM p_lease_token THEN
            -- A lease timeout alone does not close an unsettled physical page.
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END IF;
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id', p_output_id,
            'lease_token', p_lease_token,
            'page_id', v_page.page_id,
            'lease_expires_at', v_retirement.lease_expires_at,
            'after_operation_id', v_page.after_operation_id,
            'items', aap_assets_v1._native_pyramid_retirement_page_items(
                p_output_id, v_page.member_ids),
            'exhausted', v_page.terminal
        ), NULL);
    END IF;
    v_after := v_retirement.after_operation_id;
    LOOP
        v_batch := 0;
        FOR v_member IN
            SELECT member.operation_id, member.state, member.etag, member.version_id
            FROM aap_assets_v1.image_pyramid_native_members AS member
            WHERE member.output_id = p_output_id
              AND (v_after IS NULL OR member.operation_id > v_after)
            ORDER BY member.operation_id
            LIMIT 200
        LOOP
            v_batch := v_batch + 1;
            v_after := v_member.operation_id;
            IF v_member.state NOT IN ('verified', 'not_written') THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            IF v_member.state = 'verified' THEN
                IF v_member.etag IS NULL
                   OR v_member.etag !~ '^"[^"]+"$'
                   OR pg_catalog.length(v_member.etag) > 1024
                   OR (v_member.version_id IS NOT NULL
                       AND pg_catalog.length(v_member.version_id) > 1024) THEN
                    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
                END IF;
                v_member_ids := v_member_ids || v_member.operation_id;
                v_collected := v_collected + 1;
                EXIT WHEN v_collected >= p_page_size;
            END IF;
        END LOOP;
        EXIT WHEN v_collected >= p_page_size;
        EXIT WHEN v_batch < 200;
    END LOOP;
    v_page_id := pg_catalog.gen_random_uuid();
    BEGIN
        SELECT gate.state, gate.revision INTO v_gate
        FROM public.storage_maintenance_gates AS gate
        WHERE gate.location_id = v_target.location_id
        FOR SHARE;
        IF NOT FOUND OR v_gate.state <> 'active' THEN
            v_early_skip := true;
        ELSE
            INSERT INTO public.storage_operations(id, location_id, revision, purpose, node_id)
            VALUES (
                v_page_id, v_target.location_id, v_gate.revision,
                'image_pyramid_retirement_'
                    || pg_catalog.replace(p_output_id::text, '-', ''),
                'native-pyramid-retirement:' || v_page_id::text
            );
        END IF;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        v_early_skip := true;
    END;
    IF v_early_skip THEN
        RETURN aap_assets_v1._image_reply(NULL, 'storage_unavailable');
    END IF;
    INSERT INTO aap_assets_v1.native_pyramid_retirement_pages(
        page_id, output_id, lease_token, after_operation_id, gate_revision,
        terminal, member_ids
    ) VALUES (
        v_page_id, p_output_id, p_lease_token, v_after, v_gate.revision,
        v_collected = 0, v_member_ids
    );
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id', p_output_id,
        'lease_token', p_lease_token,
        'page_id', v_page_id,
        'lease_expires_at', v_retirement.lease_expires_at,
        'after_operation_id', v_after,
        'items', aap_assets_v1._native_pyramid_retirement_page_items(
            p_output_id, v_member_ids),
        'exhausted', v_collected = 0
    ), NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN unique_violation THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_native_pyramid_retirement_page(uuid, uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.complete_native_pyramid_retirement_page(
    p_output_id uuid, p_lease_token uuid, p_page_id uuid,
    p_outcome text, p_operation_ids jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_retirement aap_assets_v1.native_pyramid_retirements%ROWTYPE;
    v_target aap_assets_v1.native_pyramid_retirement_targets%ROWTYPE;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_page aap_assets_v1.native_pyramid_retirement_pages%ROWTYPE;
    v_permit record;
    v_ref jsonb;
    v_operation_ids uuid[] := ARRAY[]::uuid[];
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_output_id IS NULL OR p_lease_token IS NULL OR p_page_id IS NULL
       OR p_outcome IS NULL
       OR p_outcome NOT IN ('members_absent', 'prefix_absent', 'closed_without_ack')
       OR pg_catalog.jsonb_typeof(p_operation_ids) IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_array_length(p_operation_ids) > 100 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(p_operation_ids) AS element(value)
            WHERE pg_catalog.jsonb_typeof(element.value) IS DISTINCT FROM 'string'
        ) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        SELECT COALESCE(pg_catalog.array_agg(
                   requested.operation_id ORDER BY requested.ordinality
               ), ARRAY[]::uuid[])
        INTO v_operation_ids
        FROM (
            SELECT (element.value #>> '{}')::uuid AS operation_id,
                   element.ordinality
            FROM pg_catalog.jsonb_array_elements(p_operation_ids)
                 WITH ORDINALITY AS element(value, ordinality)
        ) AS requested;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END;
    IF pg_catalog.cardinality(v_operation_ids) <> (
        SELECT pg_catalog.count(DISTINCT requested.operation_id)
        FROM pg_catalog.unnest(v_operation_ids) AS requested(operation_id)
    ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF p_outcome = 'members_absent' THEN
        IF pg_catalog.cardinality(v_operation_ids) = 0 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
    ELSIF pg_catalog.cardinality(v_operation_ids) > 0 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT retirement.* INTO v_retirement
    FROM aap_assets_v1.native_pyramid_retirements AS retirement
    WHERE retirement.output_id = p_output_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    SELECT page.* INTO v_page
    FROM aap_assets_v1.native_pyramid_retirement_pages AS page
    WHERE page.page_id = p_page_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_page.output_id IS DISTINCT FROM p_output_id THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF v_page.lease_token IS DISTINCT FROM p_lease_token THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    IF v_page.state <> 'prepared' THEN
        -- A lost response is replayed from the owner's current settled state;
        -- a different outcome is never accepted for the same page.
        IF v_page.state IS DISTINCT FROM p_outcome THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_conflict');
        END IF;
        IF p_outcome = 'members_absent'
           AND v_operation_ids IS DISTINCT FROM v_page.member_ids THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        IF p_outcome = 'prefix_absent' THEN
            RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
                'output_id', p_output_id,
                'after_operation_id', v_page.after_operation_id,
                'completed', true
            ), NULL);
        END IF;
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id', p_output_id,
            'after_operation_id', v_retirement.after_operation_id,
            'completed', false
        ), NULL);
    END IF;
    IF p_outcome IN ('members_absent', 'prefix_absent') THEN
        IF v_retirement.lease_token IS DISTINCT FROM p_lease_token
           OR v_retirement.lease_expires_at IS NULL
           OR v_retirement.lease_expires_at <= v_now
           OR v_retirement.closed_at IS NOT NULL THEN
            RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
        END IF;
    END IF;
    SELECT target.* INTO v_target
    FROM aap_assets_v1.native_pyramid_retirement_targets AS target
    WHERE target.output_id = p_output_id;
    SELECT output.* INTO v_output
    FROM public.image_pyramid_outputs AS output
    WHERE output.id = p_output_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_output.state IS DISTINCT FROM 'retiring' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    v_ref := pg_catalog.jsonb_build_object(
        'schema', 2,
        'purpose', 'media-cache',
        'location_id', v_target.location_id::text,
        'config_revision', v_target.config_revision,
        'binding_generation', v_target.binding_generation,
        'endpoint_url', v_target.endpoint_url,
        'bucket', v_target.bucket,
        'key_prefix', v_target.key_prefix,
        'prefix', v_target.prefix,
        'storage_identity', v_target.storage_identity
    );
    IF v_output.storage_ref IS DISTINCT FROM v_ref THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    SELECT operation.location_id, operation.revision, operation.purpose
    INTO v_permit
    FROM public.storage_operations AS operation
    WHERE operation.id = v_page.page_id
    FOR UPDATE;
    IF NOT FOUND
       OR v_permit.location_id IS DISTINCT FROM v_target.location_id
       OR v_permit.revision IS DISTINCT FROM v_page.gate_revision
       OR v_permit.purpose IS DISTINCT FROM (
            'image_pyramid_retirement_'
                || pg_catalog.replace(p_output_id::text, '-', '')
       ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF p_outcome = 'members_absent' THEN
        IF v_page.terminal THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        IF pg_catalog.cardinality(v_page.member_ids) = 0
           OR v_page.after_operation_id IS NULL
           OR v_operation_ids IS DISTINCT FROM v_page.member_ids THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        UPDATE aap_assets_v1.native_pyramid_retirement_pages AS page
        SET state = 'members_absent', settled_at = v_now
        WHERE page.page_id = v_page.page_id AND page.state = 'prepared';
        UPDATE aap_assets_v1.native_pyramid_retirements AS retirement
        SET after_operation_id = v_page.after_operation_id, updated_at = v_now
        WHERE retirement.output_id = p_output_id;
        DELETE FROM public.storage_operations AS operation
        WHERE operation.id = v_page.page_id;
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id', p_output_id,
            'after_operation_id', v_page.after_operation_id,
            'completed', false
        ), NULL);
    ELSIF p_outcome = 'prefix_absent' THEN
        IF NOT v_page.terminal THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        IF EXISTS (
            SELECT 1
            FROM aap_assets_v1.image_pyramid_native_members AS member
            WHERE member.output_id = p_output_id
              AND (v_page.after_operation_id IS NULL
                   OR member.operation_id > v_page.after_operation_id)
        ) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_conflict');
        END IF;
        IF EXISTS (
            SELECT 1
            FROM aap_assets_v1.native_pyramid_retirement_pages AS page
            WHERE page.output_id = p_output_id
              AND page.state = 'prepared'
              AND page.page_id <> v_page.page_id
        ) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END IF;
        UPDATE public.image_pyramid_outputs AS output
        SET state = 'deleted', updated_at = v_now
        WHERE output.id = p_output_id AND output.state = 'retiring';
        IF NOT FOUND THEN
            RETURN aap_assets_v1._image_reply(NULL, 'dependency_changed');
        END IF;
        UPDATE aap_assets_v1.native_pyramid_retirement_pages AS page
        SET state = 'prefix_absent', settled_at = v_now
        WHERE page.page_id = v_page.page_id AND page.state = 'prepared';
        UPDATE aap_assets_v1.native_pyramid_retirements AS retirement
        SET after_operation_id = v_page.after_operation_id,
            closed_at = v_now, lease_owner = NULL, lease_token = NULL,
            lease_expires_at = NULL, updated_at = v_now
        WHERE retirement.output_id = p_output_id;
        DELETE FROM public.storage_operations AS operation
        WHERE operation.id = v_page.page_id;
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id', p_output_id,
            'after_operation_id', v_page.after_operation_id,
            'completed', true
        ), NULL);
    END IF;
    -- Physical closure may be reported by the page's own executor after its
    -- claim lease expired, but it never advances the cursor or claims absence.
    UPDATE aap_assets_v1.native_pyramid_retirement_pages AS page
    SET state = 'closed_without_ack', settled_at = v_now
    WHERE page.page_id = v_page.page_id AND page.state = 'prepared';
    DELETE FROM public.storage_operations AS operation
    WHERE operation.id = v_page.page_id;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id', p_output_id,
        'after_operation_id', v_retirement.after_operation_id,
        'completed', false
    ), NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_native_pyramid_retirement_page(uuid, uuid, uuid, text, jsonb) FROM PUBLIC;
-- aap-statement-break
-- One Assets owner owns the private ledger.  Copy cleanup-only EXECUTE from an
-- existing cleanup-only capability: claim_image_garbage is also granted to the
-- legacy monolithic runtime and must never become the grant template.
DO $owner$
DECLARE
    v_owner name;
    v_create boolean;
    v_signature text;
    v_grant record;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(proowner) INTO STRICT v_owner
    FROM pg_catalog.pg_proc
    WHERE oid =
          'aap_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer)'::regprocedure;
    v_create := pg_catalog.has_schema_privilege(v_owner, 'aap_assets_v1', 'CREATE');
    IF NOT v_create THEN
        EXECUTE pg_catalog.format('GRANT CREATE ON SCHEMA aap_assets_v1 TO %I', v_owner);
    END IF;
    EXECUTE pg_catalog.format(
        'ALTER TABLE aap_assets_v1.native_pyramid_retirement_targets OWNER TO %I', v_owner);
    EXECUTE pg_catalog.format(
        'ALTER TABLE aap_assets_v1.native_pyramid_retirements OWNER TO %I', v_owner);
    EXECUTE pg_catalog.format(
        'ALTER TABLE aap_assets_v1.native_pyramid_retirement_pages OWNER TO %I', v_owner);
    FOREACH v_signature IN ARRAY ARRAY[
        '_native_pyramid_retirement_policy_valid(jsonb)',
        '_native_pyramid_retirement_grace(jsonb)',
        '_native_pyramid_retirement_store_valid(jsonb)',
        '_native_pyramid_retirement_target_valid(jsonb,uuid)',
        '_native_pyramid_retirement_page_items(uuid,uuid[])',
        '_native_pyramid_retirement_claimable(uuid)',
        'record_native_pyramid_retirement()',
        'guard_native_pyramid_retirement_target()',
        'guard_native_pyramid_retirement_page()',
        'preserve_native_pyramid_retirement_grace()',
        'claim_native_pyramid_retirement(uuid,jsonb,integer)',
        'prepare_native_pyramid_retirement_page(uuid,uuid,integer)',
        'complete_native_pyramid_retirement_page(uuid,uuid,uuid,text,jsonb)'
    ] LOOP
        EXECUTE pg_catalog.format(
            'ALTER FUNCTION aap_assets_v1.%s OWNER TO %I', v_signature, v_owner);
    END LOOP;
    IF NOT v_create THEN
        EXECUTE pg_catalog.format('REVOKE CREATE ON SCHEMA aap_assets_v1 FROM %I', v_owner);
    END IF;
    FOR v_grant IN
        SELECT role.rolname, acl.is_grantable
        FROM pg_catalog.pg_proc AS procedure
        CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) AS acl
        JOIN pg_catalog.pg_roles AS role ON role.oid = acl.grantee
        WHERE procedure.oid =
              'aap_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer)'::regprocedure
          AND acl.privilege_type = 'EXECUTE'
          AND acl.grantee <> procedure.proowner
    LOOP
        FOREACH v_signature IN ARRAY ARRAY[
            'claim_native_pyramid_retirement(uuid,jsonb,integer)',
            'prepare_native_pyramid_retirement_page(uuid,uuid,integer)',
            'complete_native_pyramid_retirement_page(uuid,uuid,uuid,text,jsonb)'
        ] LOOP
            EXECUTE pg_catalog.format(
                'GRANT EXECUTE ON FUNCTION aap_assets_v1.%s TO %I%s',
                v_signature, v_grant.rolname,
                CASE WHEN v_grant.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
            );
        END LOOP;
    END LOOP;
END;
$owner$;
