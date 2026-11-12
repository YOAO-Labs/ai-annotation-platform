-- Source owns physical verification and its durable, exact-attempt receipt.
CREATE TABLE aap_assets_v1.import_original_verifications (
    job_id uuid NOT NULL,
    lease_token uuid NOT NULL,
    entry_index integer NOT NULL CHECK (entry_index >= 0),
    item_id uuid NOT NULL,
    creation_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    mode text NOT NULL CHECK (mode IN ('write', 'read')),
    reservation_id uuid,
    admission_id uuid,
    read_operation_id uuid NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    md5 text NOT NULL CHECK (md5 ~ '^[0-9a-f]{32}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    etag text NOT NULL,
    version_id text,
    state text NOT NULL CHECK (state IN ('reserved', 'pending', 'drained', 'completed', 'failed', 'fenced', 'cancelled')),
    evidence jsonb,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    completed_at timestamptz,
    PRIMARY KEY (job_id, lease_token, operation_id),
    UNIQUE (job_id, lease_token, reservation_id),
    CHECK ((mode = 'read') = (reservation_id IS NOT NULL)),
    CHECK ((state IN ('reserved', 'cancelled')) = (admission_id IS NULL)),
    CHECK ((state = 'completed') = (result IS NOT NULL)),
    CHECK ((state IN ('reserved', 'pending', 'drained')) = (completed_at IS NULL)),
    CHECK ((state IN ('drained', 'completed', 'failed', 'fenced')) = (evidence IS NOT NULL)
        OR (state = 'fenced' AND evidence IS NULL))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.import_original_verifications FROM PUBLIC;
-- aap-statement-break
CREATE INDEX ix_import_original_verification_creation_write
    ON aap_assets_v1.import_original_verifications (creation_id,operation_id)
    WHERE mode='write';
-- aap-statement-break
CREATE INDEX ix_import_original_unsettled_write
    ON public.managed_original_creations (id)
    WHERE status='written' AND owner_kind='dataset' AND origin='connector'
      AND authority->>'kind'='dataset_import';
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_import_original_verification()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'import verification receipts are retained' USING ERRCODE='23514';
    END IF;
    IF NOT (
        (OLD.state = 'reserved' AND NEW.state IN ('pending','cancelled'))
        OR (OLD.state = 'pending' AND NEW.state = 'drained')
        OR (OLD.state = 'drained' AND NEW.state IN ('completed','failed','fenced'))
    ) OR (pg_catalog.to_jsonb(NEW) - ARRAY['state','evidence','result','completed_at','admission_id'])
         IS DISTINCT FROM (pg_catalog.to_jsonb(OLD) - ARRAY['state','evidence','result','completed_at','admission_id'])
       OR (NEW.admission_id IS DISTINCT FROM OLD.admission_id
           AND NOT (OLD.state='reserved' AND NEW.state='pending'
                    AND OLD.admission_id IS NULL AND NEW.admission_id IS NOT NULL))
       OR (OLD.state <> 'pending' AND NEW.evidence IS DISTINCT FROM OLD.evidence) THEN
        RAISE EXCEPTION 'import verification receipt is immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_import_original_verification() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER guard_import_original_verification
BEFORE UPDATE OR DELETE ON aap_assets_v1.import_original_verifications
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_import_original_verification();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._import_verification_scope(p jsonb, p_finish boolean)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_job uuid; v_lease uuid; v_index integer; v_item uuid; v_creation_id uuid;
    v_operation_id uuid; v_mode text;
    v_import public.dataset_import_operations%ROWTYPE;
    v_entry public.dataset_import_entries%ROWTYPE;
    v_creation public.managed_original_creations%ROWTYPE;
    v_active jsonb;
    v_gate_revision integer;
BEGIN
    IF pg_catalog.jsonb_typeof(p) IS DISTINCT FROM 'object'
       OR NOT (p ?& ARRAY['job_id','lease_token','entry_index','item_id','creation_id','operation_id','mode'])
       OR (p - ARRAY['job_id','lease_token','entry_index','item_id','creation_id','operation_id','mode']) <> '{}'::jsonb THEN
        RETURN NULL;
    END IF;
    v_job := (p->>'job_id')::uuid; v_lease := (p->>'lease_token')::uuid;
    v_index := (p->>'entry_index')::integer; v_item := (p->>'item_id')::uuid;
    v_creation_id := (p->>'creation_id')::uuid; v_operation_id := (p->>'operation_id')::uuid;
    v_mode := p->>'mode';
    IF v_job IS NULL OR v_lease IS NULL OR v_index IS NULL OR v_index < 0
       OR v_item IS NULL OR v_creation_id IS NULL OR v_operation_id IS NULL
       OR v_mode IS NULL OR v_mode NOT IN ('write','read') THEN RETURN NULL; END IF;
    SELECT * INTO v_import FROM public.dataset_import_operations WHERE job_id=v_job;
    IF NOT FOUND THEN RETURN NULL; END IF;
    PERFORM 1 FROM public.datasets WHERE id=v_import.dataset_id FOR NO KEY UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO v_import FROM public.dataset_import_operations WHERE job_id=v_job FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_import.stage <> 'importing' OR v_import.cursor <> v_index
       OR v_import.lease_token IS DISTINCT FROM v_lease
       OR v_import.lease_expires_at IS NULL
       OR v_import.lease_expires_at <= pg_catalog.clock_timestamp() THEN RETURN NULL; END IF;
    v_active := aap_assets_v1._dataset_import_job_active(v_job, v_import.dataset_id);
    IF v_active->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_active#>>'{error,code}' = 'dataset_busy' THEN
            RAISE EXCEPTION 'import job lock busy' USING ERRCODE='55P03';
        END IF;
        RETURN NULL;
    END IF;
    SELECT * INTO v_entry FROM public.dataset_import_entries
      WHERE job_id=v_job AND entry_index=v_index FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_entry.status <> 'pending' OR v_entry.item_id IS DISTINCT FROM v_item THEN RETURN NULL; END IF;
    SELECT * INTO v_creation FROM public.managed_original_creations
      WHERE id=v_creation_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_creation.dataset_id IS DISTINCT FROM v_import.dataset_id
       OR v_creation.owner_kind <> 'dataset'
       OR v_creation.origin <> 'connector'
       OR v_creation.authority->>'kind' IS DISTINCT FROM 'dataset_import'
       OR v_creation.authority->>'job_id' IS DISTINCT FROM v_job::text
       OR v_creation.authority->>'entry_index' IS DISTINCT FROM v_index::text
       OR v_creation.authority->>'item_id' IS DISTINCT FROM v_item::text
       OR v_creation.authority->>'publication_id' IS DISTINCT FROM v_entry.publication_id::text
       OR v_creation.configuration->'original_ref'->>'location_id' IS DISTINCT FROM v_creation.location_id::text
       OR v_creation.configuration->'original_ref'->>'bucket' IS DISTINCT FROM v_creation.bucket
       OR v_creation.configuration->'original_ref'->>'object_key' IS DISTINCT FROM v_creation.object_key
       OR v_creation.configuration->'original_ref'->>'storage_identity' IS DISTINCT FROM v_creation.storage_identity
       OR v_creation.configuration->>'endpoint' IS DISTINCT FROM v_creation.configuration->'original_ref'->>'endpoint_url'
       OR v_creation.storage_identity IS DISTINCT FROM aap_assets_v1._managed_original_identity(
           v_creation.configuration->'original_ref'->>'endpoint_url', v_creation.bucket)
       OR pg_catalog.substr(v_creation.object_key,
           pg_catalog.char_length(COALESCE(v_creation.configuration->'original_ref'->>'key_prefix',''))+1)
          IS DISTINCT FROM v_entry.file_path
       OR v_creation.sha256 IS NULL OR v_creation.md5 IS NULL OR v_creation.size_bytes IS NULL
       OR v_creation.etag IS NULL THEN RETURN NULL; END IF;
    PERFORM 1 FROM public.storage_locations
      WHERE id=v_creation.location_id FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF v_creation.configuration->'location'->>'connection_id' IS NOT NULL THEN
        PERFORM 1 FROM public.storage_connections
          WHERE id=(v_creation.configuration->'location'->>'connection_id')::uuid
          FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN NULL; END IF;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.storage_locations location
        LEFT JOIN public.storage_connections connection ON connection.id=location.connection_id
        WHERE location.id=v_creation.location_id AND location.enabled AND location.kind='s3'
          AND location.config_revision=(v_creation.configuration->'original_ref'->>'config_revision')::integer
          AND location.connection_id IS NOT DISTINCT FROM
              NULLIF(v_creation.configuration->'location'->>'connection_id','')::uuid
          AND (location.connection_id IS NULL OR (
              connection.kind='s3' AND connection.scope='global' AND connection.secret_enc IS NOT NULL
              AND connection.revision=(v_creation.configuration->>'connection_revision')::integer
              AND connection.config->>'bucket'=v_creation.bucket
              AND aap_assets_v1._managed_original_endpoint(connection.config->>'endpoint')=
                  v_creation.configuration->'original_ref'->>'endpoint_url'
          ))
    ) THEN RETURN NULL; END IF;
    IF v_mode='write' AND v_creation.operation_id IS DISTINCT FROM v_operation_id THEN RETURN NULL; END IF;
    IF v_mode='read' AND v_creation.status <> 'verified' THEN RETURN NULL; END IF;
    IF v_mode='write' AND v_creation.status <> 'written' THEN
        IF NOT p_finish OR v_creation.status <> 'verified' OR NOT EXISTS (
            SELECT 1 FROM aap_assets_v1.import_original_verifications receipt
            WHERE receipt.job_id=v_job AND receipt.lease_token=v_lease
              AND receipt.entry_index=v_index AND receipt.item_id=v_item
              AND receipt.creation_id=v_creation_id AND receipt.operation_id=v_operation_id
              AND receipt.mode='write' AND receipt.state='drained'
        ) OR EXISTS (
            SELECT 1 FROM public.storage_operations WHERE id=v_operation_id
        ) THEN RETURN NULL; END IF;
    ELSIF NOT EXISTS (
        SELECT 1 FROM public.storage_operations AS op
        WHERE op.id=v_operation_id AND op.location_id=v_creation.location_id
          AND op.revision=v_creation.maintenance_revision
          AND op.purpose=CASE WHEN v_mode='write' THEN 'managed_original_creation'
              ELSE aap_assets_v1._operator_read_permit_purpose(v_creation.id) END
    ) THEN RETURN NULL; END IF;
    -- Check saved revision under the same gate lock before interpreting an
    -- inactive gate as transient; revision drift is a definitive fence.
    SELECT gate.revision INTO v_gate_revision FROM public.storage_maintenance_gates gate
      WHERE gate.location_id=v_creation.location_id FOR SHARE;
    IF FOUND AND v_gate_revision IS DISTINCT FROM v_creation.maintenance_revision THEN
        RETURN NULL;
    END IF;
    BEGIN
        IF aap_assets_v1._managed_original_gate_revision(v_creation.location_id)
           IS DISTINCT FROM v_creation.maintenance_revision THEN RETURN NULL; END IF;
    EXCEPTION WHEN SQLSTATE '55006' THEN
        IF p_finish THEN RETURN NULL; END IF;
        RAISE EXCEPTION 'import_verification_maintenance_unavailable' USING ERRCODE='55006';
    END;
    RETURN aap_assets_v1._managed_original_facts(v_creation)
        || pg_catalog.jsonb_build_object(
            'status', v_creation.status, 'sha256', v_creation.sha256,
            'size_bytes', v_creation.size_bytes, 'etag', v_creation.etag,
            'version_id', v_creation.version_id,
            'file_path', pg_catalog.substr(v_creation.object_key,
                pg_catalog.char_length(COALESCE(v_creation.configuration->'original_ref'->>'key_prefix',''))+1),
            'file_type', v_entry.file_type, 'md5', v_creation.md5);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._import_verification_scope(jsonb,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.reserve_import_original_read(p jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_import public.dataset_import_operations%ROWTYPE;
    v_reply jsonb; v_request jsonb; v_scope jsonb; v_operation uuid;
    v_existing aap_assets_v1.import_original_verifications%ROWTYPE;
BEGIN
    IF pg_catalog.jsonb_typeof(p) IS DISTINCT FROM 'object'
       OR NOT (p ?& ARRAY['job_id','lease_token','entry_index','item_id','creation_id','reservation_id'])
       OR (p - ARRAY['job_id','lease_token','entry_index','item_id','creation_id','reservation_id']) <> '{}'::jsonb THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    IF (p->>'reservation_id')::uuid IS NULL THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    SELECT * INTO v_import FROM public.dataset_import_operations WHERE job_id=(p->>'job_id')::uuid;
    IF NOT FOUND THEN RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch'); END IF;
    PERFORM 1 FROM public.datasets WHERE id=v_import.dataset_id FOR NO KEY UPDATE NOWAIT;
    PERFORM 1 FROM public.dataset_import_operations WHERE job_id=v_import.job_id FOR UPDATE NOWAIT;
    PERFORM 1 FROM public.dataset_import_entries
      WHERE job_id=v_import.job_id AND entry_index=(p->>'entry_index')::integer FOR UPDATE NOWAIT;
    SELECT * INTO v_existing FROM aap_assets_v1.import_original_verifications
      WHERE job_id=(p->>'job_id')::uuid AND lease_token=(p->>'lease_token')::uuid
        AND reservation_id=(p->>'reservation_id')::uuid FOR UPDATE;
    IF FOUND THEN
        IF v_existing.state <> 'reserved' OR v_existing.entry_index IS DISTINCT FROM (p->>'entry_index')::integer
           OR v_existing.item_id IS DISTINCT FROM (p->>'item_id')::uuid
           OR v_existing.creation_id IS DISTINCT FROM (p->>'creation_id')::uuid THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
        v_request := (p - 'reservation_id') || pg_catalog.jsonb_build_object(
            'operation_id',v_existing.operation_id,'mode','read');
        IF aap_assets_v1._import_verification_scope(v_request, false) IS NULL THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
        RETURN aap_assets_v1._managed_original_reply(
            pg_catalog.jsonb_build_object('operation_id',v_existing.operation_id,'mode','read'),NULL);
    END IF;
    BEGIN
        v_reply := aap_assets_v1.open_operator_storage_operation((p->>'creation_id')::uuid);
        IF v_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
        IF v_reply->'data'->>'mode' IS DISTINCT FROM 'read' THEN
            RAISE EXCEPTION 'read reservation requires verified creation' USING ERRCODE='P0001';
        END IF;
        v_operation := (v_reply->'data'->>'operation_id')::uuid;
        v_request := (p - 'reservation_id') || pg_catalog.jsonb_build_object('operation_id',v_operation,'mode','read');
        v_scope := aap_assets_v1._import_verification_scope(v_request, false);
        IF v_scope IS NULL THEN
            RAISE EXCEPTION 'read reservation outside import scope' USING ERRCODE='P0001';
        END IF;
        INSERT INTO aap_assets_v1.import_original_verifications
            (job_id,lease_token,entry_index,item_id,creation_id,operation_id,mode,reservation_id,
             read_operation_id,sha256,md5,size_bytes,etag,version_id,state)
        VALUES ((p->>'job_id')::uuid,(p->>'lease_token')::uuid,(p->>'entry_index')::integer,
            (p->>'item_id')::uuid,(p->>'creation_id')::uuid,v_operation,'read',
            (p->>'reservation_id')::uuid,pg_catalog.gen_random_uuid(),
            v_scope->>'sha256',v_scope->>'md5',(v_scope->>'size_bytes')::bigint,
            v_scope->>'etag',v_scope->>'version_id','reserved');
        RETURN v_reply;
    EXCEPTION WHEN raise_exception THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
WHEN SQLSTATE '55006' THEN
    RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.reserve_import_original_read(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.cancel_unstarted_import_original_read(p jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE r aap_assets_v1.import_original_verifications%ROWTYPE;
BEGIN
    SELECT * INTO r FROM aap_assets_v1.import_original_verifications
      WHERE job_id=(p->>'job_id')::uuid AND lease_token=(p->>'lease_token')::uuid
        AND operation_id=(p->>'operation_id')::uuid FOR UPDATE;
    IF NOT FOUND OR r.mode <> 'read' OR p->>'mode' IS DISTINCT FROM 'read'
       OR r.creation_id IS DISTINCT FROM (p->>'creation_id')::uuid
       OR r.item_id IS DISTINCT FROM (p->>'item_id')::uuid
       OR r.entry_index IS DISTINCT FROM (p->>'entry_index')::integer THEN
        RETURN pg_catalog.jsonb_build_object('state','missing');
    END IF;
    IF r.state = 'reserved' THEN
        DELETE FROM public.storage_operations
          WHERE id=r.operation_id AND purpose=aap_assets_v1._operator_read_permit_purpose(r.creation_id);
        IF NOT FOUND THEN
            RAISE EXCEPTION 'reserved import read permit missing' USING ERRCODE='23514';
        END IF;
        UPDATE aap_assets_v1.import_original_verifications
          SET state='cancelled', completed_at=pg_catalog.clock_timestamp()
          WHERE job_id=r.job_id AND lease_token=r.lease_token AND operation_id=r.operation_id;
        RETURN pg_catalog.jsonb_build_object('state','cancelled');
    END IF;
    RETURN pg_catalog.jsonb_build_object('state',r.state);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN pg_catalog.jsonb_build_object('state','missing');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.cancel_unstarted_import_original_read(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_import_original_verification(p jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_scope jsonb; v_existing aap_assets_v1.import_original_verifications%ROWTYPE;
        v_read uuid := pg_catalog.gen_random_uuid();
BEGIN
    IF NOT (p ? 'admission_id') OR (p->>'admission_id')::uuid IS NULL THEN
        RETURN pg_catalog.jsonb_build_object('state','fenced');
    END IF;
    v_scope := aap_assets_v1._import_verification_scope(p - 'admission_id', false);
    IF v_scope IS NULL THEN RETURN pg_catalog.jsonb_build_object('state','fenced'); END IF;
    SELECT * INTO v_existing FROM aap_assets_v1.import_original_verifications
      WHERE job_id=(p->>'job_id')::uuid AND lease_token=(p->>'lease_token')::uuid
        AND operation_id=(p->>'operation_id')::uuid FOR UPDATE;
    IF FOUND THEN
        IF v_existing.creation_id IS DISTINCT FROM (p->>'creation_id')::uuid
           OR v_existing.item_id IS DISTINCT FROM (p->>'item_id')::uuid
           OR v_existing.entry_index IS DISTINCT FROM (p->>'entry_index')::integer
           OR v_existing.mode IS DISTINCT FROM p->>'mode' THEN
            RETURN pg_catalog.jsonb_build_object('state','fenced');
        END IF;
        IF v_existing.state='pending'
           AND v_existing.admission_id=(p->>'admission_id')::uuid THEN
            -- Only the Source request holding this private nonce can resume
            -- an ambiguous admission, before it starts physical read I/O.
            RETURN pg_catalog.jsonb_build_object('state','started','material',v_scope);
        END IF;
        IF v_existing.state <> 'reserved' THEN
            RETURN pg_catalog.jsonb_build_object('state',v_existing.state,'result',v_existing.result);
        END IF;
        v_read := v_existing.read_operation_id;
    ELSIF p->>'mode' = 'read' THEN
        RETURN pg_catalog.jsonb_build_object('state','fenced');
    END IF;
    INSERT INTO public.storage_operations (id,location_id,revision,purpose,node_id)
    SELECT v_read, c.location_id, c.maintenance_revision,
        aap_assets_v1._operator_read_permit_purpose(c.id), 'aap-assets-source-import-verify'
    FROM public.managed_original_creations c WHERE c.id=(p->>'creation_id')::uuid;
    IF v_existing.state = 'reserved' THEN
        UPDATE aap_assets_v1.import_original_verifications
          SET state='pending', admission_id=(p->>'admission_id')::uuid
          WHERE job_id=v_existing.job_id AND lease_token=v_existing.lease_token
            AND operation_id=v_existing.operation_id;
    ELSE
        INSERT INTO aap_assets_v1.import_original_verifications
            (job_id,lease_token,entry_index,item_id,creation_id,operation_id,mode,admission_id,
             read_operation_id,sha256,md5,size_bytes,etag,version_id,state)
        VALUES ((p->>'job_id')::uuid,(p->>'lease_token')::uuid,(p->>'entry_index')::integer,
            (p->>'item_id')::uuid,(p->>'creation_id')::uuid,(p->>'operation_id')::uuid,
            p->>'mode',(p->>'admission_id')::uuid,v_read,v_scope->>'sha256',v_scope->>'md5',
            (v_scope->>'size_bytes')::bigint,v_scope->>'etag',v_scope->>'version_id','pending');
    END IF;
    RETURN pg_catalog.jsonb_build_object('state','started','material',v_scope);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_import_original_verification(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.record_import_original_read(p jsonb, p_result jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE r aap_assets_v1.import_original_verifications%ROWTYPE;
        v_evidence jsonb := pg_catalog.jsonb_build_object('result',p_result);
BEGIN
    IF p ? 'admission_id' THEN
        -- Serialize with a begin whose COMMIT acknowledgement was lost.  A
        -- missing row is conclusive only after that owner transaction drains.
        PERFORM 1 FROM public.dataset_import_operations
          WHERE job_id=(p->>'job_id')::uuid FOR UPDATE;
    END IF;
    SELECT * INTO r FROM aap_assets_v1.import_original_verifications
      WHERE job_id=(p->>'job_id')::uuid AND lease_token=(p->>'lease_token')::uuid
        AND operation_id=(p->>'operation_id')::uuid FOR UPDATE;
    IF NOT FOUND OR r.creation_id IS DISTINCT FROM (p->>'creation_id')::uuid
       OR r.item_id IS DISTINCT FROM (p->>'item_id')::uuid
       OR r.entry_index IS DISTINCT FROM (p->>'entry_index')::integer
       OR r.mode IS DISTINCT FROM p->>'mode' THEN
        RETURN pg_catalog.jsonb_build_object('state','fenced');
    END IF;
    IF p ? 'admission_id' AND (
        p_result IS NOT NULL OR r.admission_id IS DISTINCT FROM (p->>'admission_id')::uuid
    ) THEN
        RETURN pg_catalog.jsonb_build_object('state','fenced');
    END IF;
    IF r.state <> 'pending' THEN
        IF r.evidence IS NOT NULL AND r.evidence IS DISTINCT FROM v_evidence THEN
            RAISE EXCEPTION 'import verification evidence changed' USING ERRCODE='23514';
        END IF;
        RETURN pg_catalog.jsonb_build_object('state',r.state,'result',r.result);
    END IF;
    IF p_result IS NOT NULL AND (
        pg_catalog.jsonb_typeof(p_result) IS DISTINCT FROM 'object'
        OR NOT (p_result ?& ARRAY['sha256','md5','size_bytes','etag','version_id','observation','physical_settled'])
        OR pg_catalog.jsonb_typeof(p_result->'observation') IS DISTINCT FROM 'object'
        OR NOT ((p_result->'observation') ?& ARRAY['etag','version_id','size'])
        OR p_result->>'sha256' IS DISTINCT FROM r.sha256 OR p_result->>'md5' IS DISTINCT FROM r.md5
        OR p_result->>'size_bytes' IS DISTINCT FROM r.size_bytes::text
        OR p_result->>'etag' IS DISTINCT FROM r.etag OR p_result->>'version_id' IS DISTINCT FROM r.version_id
        OR p_result->'observation'->>'etag' IS DISTINCT FROM r.etag
        OR p_result->'observation'->>'version_id' IS DISTINCT FROM r.version_id
        OR p_result->'observation'->>'size' IS DISTINCT FROM r.size_bytes::text
        OR p_result->'physical_settled' IS DISTINCT FROM 'true'::jsonb
    ) THEN
        RAISE EXCEPTION 'import verification evidence mismatch' USING ERRCODE='23514';
    END IF;
    UPDATE aap_assets_v1.import_original_verifications SET state='drained', evidence=v_evidence
      WHERE job_id=r.job_id AND lease_token=r.lease_token AND operation_id=r.operation_id;
    RETURN pg_catalog.jsonb_build_object('state','drained');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_import_original_read(jsonb,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_import_original_verification(p jsonb, p_result jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_receipt aap_assets_v1.import_original_verifications%ROWTYPE;
        v_creation public.managed_original_creations%ROWTYPE;
        v_scope jsonb; v_reply jsonb; v_state text; v_terminal boolean := false;
BEGIN
    v_scope := aap_assets_v1._import_verification_scope(p, true);
    SELECT * INTO v_creation FROM public.managed_original_creations
      WHERE id=(p->>'creation_id')::uuid FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'import verification creation missing' USING ERRCODE='23514';
    END IF;
    SELECT * INTO v_receipt FROM aap_assets_v1.import_original_verifications
      WHERE job_id=(p->>'job_id')::uuid AND lease_token=(p->>'lease_token')::uuid
        AND operation_id=(p->>'operation_id')::uuid FOR UPDATE;
    IF NOT FOUND OR v_receipt.creation_id IS DISTINCT FROM (p->>'creation_id')::uuid
       OR v_receipt.item_id IS DISTINCT FROM (p->>'item_id')::uuid
       OR v_receipt.entry_index IS DISTINCT FROM (p->>'entry_index')::integer
       OR v_receipt.mode IS DISTINCT FROM p->>'mode' THEN
       RETURN pg_catalog.jsonb_build_object('state','fenced'); END IF;
    IF v_receipt.state <> 'drained' THEN
       RETURN pg_catalog.jsonb_build_object('state',v_receipt.state,'result',v_receipt.result); END IF;
    IF v_receipt.evidence->'result' IS DISTINCT FROM COALESCE(p_result,'null'::jsonb) THEN
       RAISE EXCEPTION 'import verification evidence changed' USING ERRCODE='23514'; END IF;
    IF v_receipt.mode='write' THEN
        IF v_creation.operation_id IS DISTINCT FROM v_receipt.operation_id
           OR v_creation.origin IS DISTINCT FROM 'connector'
           OR v_creation.authority->>'kind' IS DISTINCT FROM 'dataset_import'
           OR v_creation.authority->>'job_id' IS DISTINCT FROM v_receipt.job_id::text
           OR v_creation.authority->>'entry_index' IS DISTINCT FROM v_receipt.entry_index::text
           OR v_creation.authority->>'item_id' IS DISTINCT FROM v_receipt.item_id::text
           OR v_creation.sha256 IS DISTINCT FROM v_receipt.sha256
           OR v_creation.md5 IS DISTINCT FROM v_receipt.md5
           OR v_creation.size_bytes IS DISTINCT FROM v_receipt.size_bytes
           OR v_creation.etag IS DISTINCT FROM v_receipt.etag
           OR v_creation.version_id IS DISTINCT FROM v_receipt.version_id THEN
            RAISE EXCEPTION 'import verification creation identity mismatch' USING ERRCODE='23514';
        END IF;
        IF v_creation.status='verified' THEN
            IF pg_catalog.jsonb_typeof(v_creation.verification) IS DISTINCT FROM 'object'
               OR NOT (v_creation.verification ?& ARRAY[
                   'physical_settled','sha256','md5','size_bytes','etag','version_id'
               ])
               OR v_creation.verification->'physical_settled' IS DISTINCT FROM 'true'::jsonb
               OR v_creation.verification->>'sha256' IS DISTINCT FROM v_receipt.sha256
               OR v_creation.verification->>'md5' IS DISTINCT FROM v_receipt.md5
               OR v_creation.verification->>'size_bytes' IS DISTINCT FROM v_receipt.size_bytes::text
               OR v_creation.verification->>'etag' IS DISTINCT FROM v_receipt.etag
               OR v_creation.verification->>'version_id' IS DISTINCT FROM v_receipt.version_id THEN
                RAISE EXCEPTION 'verified creation evidence mismatch' USING ERRCODE='23514';
            END IF;
            v_terminal := true;
        ELSIF v_creation.status='failed' THEN
            IF NOT EXISTS (
                SELECT 1 FROM public.storage_usage_mutations mutation
                WHERE mutation.owner_id=v_creation.id
                  AND mutation.producer='managed_original_creation' AND mutation.kind='create'
                  AND mutation.object_key=v_creation.object_key AND mutation.finished_at IS NOT NULL
                  AND mutation.state IN ('confirmed','unknown')
                  AND mutation.physical_outcome='written'
                  AND mutation.evidence->'physical_settled'='true'::jsonb
                  AND mutation.evidence->>'creation_status'='failed'
                  AND mutation.evidence->'receipt'=v_creation.receipt
            ) THEN
                RAISE EXCEPTION 'failed creation settlement evidence missing' USING ERRCODE='23514';
            END IF;
            v_terminal := true;
        ELSIF v_creation.status <> 'written' THEN
            RAISE EXCEPTION 'import verification creation state conflict' USING ERRCODE='23514';
        END IF;
        IF v_terminal AND EXISTS (
            SELECT 1 FROM public.storage_operations WHERE id=v_receipt.operation_id
        ) THEN
            RAISE EXCEPTION 'terminal creation write permit retained' USING ERRCODE='23514';
        END IF;
    END IF;
    IF p_result IS NULL THEN
        IF v_receipt.mode='write' AND NOT v_terminal THEN
            SELECT aap_assets_v1.fail_operator_original(
                v_receipt.creation_id, 'failed', 'import_verification_failed',
                pg_catalog.jsonb_build_object(
                    'physical_settled', true,
                    'physical_outcome', 'written',
                    'physical_size_bytes', v_receipt.size_bytes,
                    'stage', 'verify',
                    'receipt', creation.receipt
                )
            ) INTO v_reply
            FROM public.managed_original_creations creation
            WHERE creation.id=v_receipt.creation_id;
            IF v_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RAISE EXCEPTION 'import verification failure settlement refused' USING ERRCODE='23514';
            END IF;
        END IF;
        v_state := CASE WHEN v_scope IS NULL THEN 'fenced' ELSE 'failed' END;
    ELSE
        IF pg_catalog.jsonb_typeof(p_result) IS DISTINCT FROM 'object'
           OR NOT (p_result ?& ARRAY['sha256','md5','size_bytes','etag','version_id','observation','physical_settled'])
           OR pg_catalog.jsonb_typeof(p_result->'observation') IS DISTINCT FROM 'object'
           OR NOT ((p_result->'observation') ?& ARRAY['etag','version_id','size'])
           OR (v_scope IS NOT NULL AND (
               p_result->>'sha256' IS DISTINCT FROM v_scope->>'sha256'
               OR p_result->>'md5' IS DISTINCT FROM v_scope->>'md5'
               OR p_result->>'size_bytes' IS DISTINCT FROM v_scope->>'size_bytes'
               OR p_result->>'etag' IS DISTINCT FROM v_scope->>'etag'
               OR p_result->>'version_id' IS DISTINCT FROM v_scope->>'version_id'))
           OR p_result->>'sha256' IS DISTINCT FROM v_receipt.sha256
           OR p_result->>'md5' IS DISTINCT FROM v_receipt.md5
           OR p_result->>'size_bytes' IS DISTINCT FROM v_receipt.size_bytes::text
           OR p_result->>'etag' IS DISTINCT FROM v_receipt.etag
           OR p_result->>'version_id' IS DISTINCT FROM v_receipt.version_id
           OR p_result->'observation'->>'etag' IS DISTINCT FROM v_receipt.etag
           OR p_result->'observation'->>'version_id' IS DISTINCT FROM v_receipt.version_id
           OR p_result->'observation'->>'size' IS DISTINCT FROM v_receipt.size_bytes::text
           OR p_result->'physical_settled' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION 'import verification evidence mismatch' USING ERRCODE='23514';
        END IF;
        IF v_receipt.mode='write' AND NOT v_terminal THEN
            v_reply := aap_assets_v1.record_operator_original_verified(v_receipt.creation_id,p_result);
            IF v_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RAISE EXCEPTION 'import verification settlement refused' USING ERRCODE='23514';
            END IF;
        END IF;
        v_state := CASE WHEN v_scope IS NULL OR v_creation.status='failed'
            THEN 'fenced' ELSE 'completed' END;
    END IF;
    DELETE FROM public.storage_operations WHERE id=v_receipt.read_operation_id;
    IF v_receipt.mode='read' THEN
        DELETE FROM public.storage_operations WHERE id=v_receipt.operation_id
          AND purpose=aap_assets_v1._operator_read_permit_purpose(v_receipt.creation_id);
    END IF;
    UPDATE aap_assets_v1.import_original_verifications
      SET state=v_state, result=CASE WHEN v_state='completed' THEN p_result ELSE NULL END,
          completed_at=pg_catalog.clock_timestamp()
      WHERE job_id=v_receipt.job_id AND lease_token=v_receipt.lease_token AND operation_id=v_receipt.operation_id;
    RETURN pg_catalog.jsonb_build_object('state',v_state,'result',CASE WHEN v_state='completed' THEN p_result ELSE NULL END);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_import_original_verification(jsonb,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.recover_import_original_verification(p_cursor jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    r aap_assets_v1.import_original_verifications%ROWTYPE;
    o public.dataset_import_operations%ROWTYPE;
    c public.managed_original_creations%ROWTYPE;
    e public.dataset_import_entries%ROWTYPE;
    v_current boolean;
    v_command jsonb; v_cursor jsonb; v_reply jsonb;
BEGIN
    IF p_cursor IS NOT NULL AND (
        pg_catalog.jsonb_typeof(p_cursor) IS DISTINCT FROM 'object'
        OR (
            (p_cursor->>'kind' = 'receipt'
             AND p_cursor ?& ARRAY['kind','job_id','lease_token','operation_id']
             AND (p_cursor - ARRAY['kind','job_id','lease_token','operation_id']) = '{}'::jsonb)
            OR (p_cursor->>'kind' = 'creation'
                AND p_cursor ?& ARRAY['kind','creation_id']
                AND (p_cursor - ARRAY['kind','creation_id']) = '{}'::jsonb)
        ) IS NOT TRUE
    ) THEN RAISE EXCEPTION 'invalid recovery cursor' USING ERRCODE='22023'; END IF;

    IF p_cursor IS NULL OR p_cursor->>'kind' = 'receipt' THEN
        SELECT receipt.* INTO r
        FROM aap_assets_v1.import_original_verifications receipt
        WHERE (receipt.state='drained' OR (
            receipt.state='reserved' AND NOT EXISTS (
                SELECT 1 FROM public.dataset_import_operations operation
                WHERE operation.job_id=receipt.job_id AND operation.stage='importing'
                  AND operation.cursor=receipt.entry_index AND operation.lease_token=receipt.lease_token
                  AND operation.lease_expires_at>pg_catalog.clock_timestamp()
            )
        )) AND (
            p_cursor IS NULL OR (receipt.job_id,receipt.lease_token,receipt.operation_id) >
                ((p_cursor->>'job_id')::uuid,(p_cursor->>'lease_token')::uuid,(p_cursor->>'operation_id')::uuid)
        )
        ORDER BY receipt.job_id,receipt.lease_token,receipt.operation_id LIMIT 1;
        IF FOUND THEN
            v_cursor := pg_catalog.jsonb_build_object(
                'kind','receipt','job_id',r.job_id,'lease_token',r.lease_token,'operation_id',r.operation_id);
            v_command := (v_cursor - 'kind') || pg_catalog.jsonb_build_object(
                'entry_index',r.entry_index,'item_id',r.item_id,'creation_id',r.creation_id,'mode',r.mode);
            BEGIN
                IF r.state='drained' THEN
                    PERFORM aap_assets_v1.finish_import_original_verification(
                        v_command,NULLIF(r.evidence->'result','null'::jsonb));
                ELSE
                    SELECT * INTO o FROM public.dataset_import_operations
                      WHERE job_id=r.job_id FOR UPDATE NOWAIT;
                    IF NOT FOUND OR o.stage <> 'importing' OR o.cursor <> r.entry_index
                       OR o.lease_token IS DISTINCT FROM r.lease_token
                       OR o.lease_expires_at IS NULL OR o.lease_expires_at <= pg_catalog.clock_timestamp() THEN
                        PERFORM aap_assets_v1.cancel_unstarted_import_original_read(v_command);
                    END IF;
                END IF;
            EXCEPTION WHEN lock_not_available THEN
                NULL;
            END;
            RETURN pg_catalog.jsonb_build_object('cursor',v_cursor);
        END IF;
    END IF;

    -- A written creation is a durable, drained PUT receipt, not proof of a
    -- verification read.  Stale, terminal, and superseded scopes can no
    -- longer begin a read; pending physical reads remain owned.
    SELECT creation.* INTO c
    FROM public.managed_original_creations creation
    JOIN public.dataset_import_operations operation
      ON operation.job_id::text=creation.authority->>'job_id'
     AND operation.dataset_id=creation.dataset_id
    WHERE creation.status='written' AND creation.owner_kind='dataset'
      AND creation.origin='connector' AND creation.authority->>'kind'='dataset_import'
      AND (operation.stage IN ('completed','failed','cancelled') OR (
          operation.stage='importing' AND
          (operation.lease_expires_at IS NULL
           OR operation.lease_expires_at<=pg_catalog.clock_timestamp()
           OR operation.cursor::text IS DISTINCT FROM creation.authority->>'entry_index'
           OR NOT EXISTS (
               SELECT 1 FROM public.dataset_import_entries entry
               WHERE entry.job_id=operation.job_id
                 AND entry.entry_index::text=creation.authority->>'entry_index'
                 AND entry.status='pending'
                 AND entry.item_id::text=creation.authority->>'item_id'
                 AND entry.publication_id::text=creation.authority->>'publication_id'
           ))
      ))
      AND NOT EXISTS (
          SELECT 1 FROM aap_assets_v1.import_original_verifications receipt
          WHERE receipt.creation_id=creation.id AND receipt.mode='write'
            AND receipt.operation_id=creation.operation_id
      )
      AND (p_cursor IS NULL OR p_cursor->>'kind' <> 'creation'
           OR creation.id>(p_cursor->>'creation_id')::uuid)
    ORDER BY creation.id LIMIT 1;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('cursor',NULL); END IF;
    v_cursor := pg_catalog.jsonb_build_object('kind','creation','creation_id',c.id);
    BEGIN
        -- Begin takes these same locks in this order.  Once both are held,
        -- checking the absence of a receipt and failing the creation fences a
        -- late begin atomically; it cannot have started physical Source I/O.
        SELECT * INTO o FROM public.dataset_import_operations
          WHERE job_id=(c.authority->>'job_id')::uuid FOR UPDATE NOWAIT;
        IF FOUND AND o.dataset_id=c.dataset_id THEN
            SELECT * INTO e FROM public.dataset_import_entries
              WHERE job_id=o.job_id AND entry_index::text=c.authority->>'entry_index'
              FOR UPDATE NOWAIT;
            v_current := FOUND AND o.cursor::text= c.authority->>'entry_index'
                AND e.status='pending'
                AND e.item_id::text=c.authority->>'item_id'
                AND e.publication_id::text=c.authority->>'publication_id';
        ELSE
            v_current := true;
        END IF;
        IF o.dataset_id=c.dataset_id
           AND (o.stage IN ('completed','failed','cancelled') OR (
               o.stage='importing' AND
               (o.lease_expires_at IS NULL OR o.lease_expires_at<=pg_catalog.clock_timestamp()
                OR v_current IS NOT TRUE)
           )) THEN
            SELECT * INTO c FROM public.managed_original_creations WHERE id=c.id FOR UPDATE NOWAIT;
            IF FOUND AND c.status='written' AND c.owner_kind='dataset' AND c.origin='connector'
               AND c.authority->>'kind'='dataset_import'
               AND c.authority->>'job_id'=o.job_id::text AND c.dataset_id=o.dataset_id
               AND (o.stage IN ('completed','failed','cancelled') OR (
                   o.stage='importing' AND (
                       o.lease_expires_at IS NULL OR o.lease_expires_at<=pg_catalog.clock_timestamp()
                       OR o.cursor::text IS DISTINCT FROM c.authority->>'entry_index'
                       OR e.entry_index::text IS DISTINCT FROM c.authority->>'entry_index'
                       OR e.status IS DISTINCT FROM 'pending'
                       OR e.item_id::text IS DISTINCT FROM c.authority->>'item_id'
                       OR e.publication_id::text IS DISTINCT FROM c.authority->>'publication_id'
                   )
               ))
               AND NOT EXISTS (
                   SELECT 1 FROM aap_assets_v1.import_original_verifications receipt
                   WHERE receipt.creation_id=c.id AND receipt.mode='write'
                     AND receipt.operation_id=c.operation_id
               ) THEN
                IF c.size_bytes IS NULL OR c.etag IS NULL
                   OR pg_catalog.jsonb_typeof(c.receipt) IS DISTINCT FROM 'object'
                   OR c.receipt->>'etag' IS DISTINCT FROM c.etag
                   OR NULLIF(c.receipt->>'version_id','null') IS DISTINCT FROM c.version_id
                   OR NOT EXISTS (
                       SELECT 1 FROM public.storage_operations permit
                       WHERE permit.id=c.operation_id AND permit.location_id=c.location_id
                         AND permit.revision=c.maintenance_revision
                         AND permit.purpose='managed_original_creation'
                   ) THEN
                    RAISE EXCEPTION 'unstarted import write evidence mismatch' USING ERRCODE='23514';
                END IF;
                v_reply := aap_assets_v1.fail_operator_original(
                    c.id, 'failed', 'import_verification_not_started',
                    pg_catalog.jsonb_build_object(
                        'physical_settled',true, 'physical_outcome','written',
                        'physical_size_bytes',c.size_bytes, 'stage','verify',
                        'receipt',c.receipt
                    )
                );
                IF v_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    RAISE EXCEPTION 'unstarted import write settlement refused' USING ERRCODE='23514';
                END IF;
            END IF;
        END IF;
    EXCEPTION WHEN lock_not_available THEN
        NULL;
    END;
    RETURN pg_catalog.jsonb_build_object('cursor',v_cursor);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.recover_import_original_verification(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.status_import_original_verification(p jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE r aap_assets_v1.import_original_verifications%ROWTYPE; o public.dataset_import_operations%ROWTYPE;
        v_active jsonb;
BEGIN
    SELECT * INTO r FROM aap_assets_v1.import_original_verifications
      WHERE job_id=(p->>'job_id')::uuid AND lease_token=(p->>'lease_token')::uuid
        AND operation_id=(p->>'operation_id')::uuid;
    IF NOT FOUND OR r.creation_id IS DISTINCT FROM (p->>'creation_id')::uuid
       OR r.item_id IS DISTINCT FROM (p->>'item_id')::uuid
       OR r.entry_index IS DISTINCT FROM (p->>'entry_index')::integer
       OR r.mode IS DISTINCT FROM p->>'mode' THEN
       RETURN pg_catalog.jsonb_build_object('state','missing'); END IF;
    IF r.state = 'drained' THEN
        RETURN pg_catalog.jsonb_build_object('state','drained','evidence',r.evidence);
    END IF;
    SELECT * INTO o FROM public.dataset_import_operations WHERE job_id=r.job_id;
    IF NOT FOUND OR o.stage <> 'importing' OR o.cursor <> r.entry_index
       OR o.lease_token IS DISTINCT FROM r.lease_token OR o.lease_expires_at IS NULL
       OR o.lease_expires_at <= pg_catalog.clock_timestamp() THEN
       RETURN pg_catalog.jsonb_build_object('state','fenced'); END IF;
    v_active := aap_assets_v1._dataset_import_job_active(r.job_id,o.dataset_id);
    IF v_active->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('state',
            CASE WHEN v_active#>>'{error,code}' = 'dataset_busy' THEN 'pending' ELSE 'fenced' END);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.datasets dataset
        JOIN public.dataset_import_entries entry ON entry.job_id=r.job_id
        JOIN public.managed_original_creations creation ON creation.id=r.creation_id
        WHERE dataset.id=o.dataset_id AND entry.entry_index=r.entry_index
          AND entry.item_id=r.item_id AND entry.status='pending'
          AND creation.dataset_id=dataset.id AND creation.owner_kind='dataset'
          AND creation.authority->>'job_id'=r.job_id::text
          AND creation.authority->>'entry_index'=r.entry_index::text
          AND creation.authority->>'item_id'=r.item_id::text
          AND creation.authority->>'publication_id'=entry.publication_id::text
    ) THEN RETURN pg_catalog.jsonb_build_object('state','fenced'); END IF;
    RETURN pg_catalog.jsonb_build_object('state',r.state,'result',r.result);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN pg_catalog.jsonb_build_object('state','fenced');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.status_import_original_verification(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.resume_import_original_verification(p jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_operation uuid; v_request jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p) IS DISTINCT FROM 'object'
       OR NOT (p ?& ARRAY['job_id','lease_token','entry_index','item_id','creation_id'])
       OR (p - ARRAY['job_id','lease_token','entry_index','item_id','creation_id']) <> '{}'::jsonb THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    SELECT operation_id INTO v_operation FROM public.managed_original_creations
      WHERE id=(p->>'creation_id')::uuid AND status='written';
    IF NOT FOUND THEN RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict'); END IF;
    v_request := p || pg_catalog.jsonb_build_object('operation_id',v_operation,'mode','write');
    IF aap_assets_v1._import_verification_scope(v_request, false) IS NULL THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END IF;
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object('operation_id',v_operation,'mode','write'),NULL);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
WHEN SQLSTATE '55006' THEN
    RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.resume_import_original_verification(jsonb) FROM PUBLIC;
-- aap-statement-break
DO $patch$
DECLARE
    v_definition text;
    v_fields_before text := $fields_before$            'status', 'file_size', 'content_hash', 'width', 'height', 'error_code'
$fields_before$;
    v_fields_after text := $fields_after$            'status', 'file_size', 'content_hash', 'width', 'height', 'error_code', 'verification_operation_id'
$fields_after$;
    v_validation_before text := $validation_before$    v_status := p_completion ->> 'status';
$validation_before$;
    v_validation_after text := $validation_after$    v_status := p_completion ->> 'status';
    IF (v_status = 'copied' AND (
          pg_catalog.jsonb_typeof(p_completion->'verification_operation_id') IS DISTINCT FROM 'string'
          OR p_completion->>'verification_operation_id' !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
       )) OR (v_status = 'error' AND p_completion->'verification_operation_id' IS NOT NULL
          AND p_completion->'verification_operation_id' <> 'null'::jsonb) THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'invalid_request');
    END IF;
$validation_after$;
    v_before text := $before$    v_next_cursor := v_operation.cursor + 1;
$before$;
    v_after text := $after$    IF v_status = 'copied' AND NOT EXISTS (
        SELECT 1
        FROM aap_assets_v1.import_original_verifications receipt
        JOIN public.managed_original_creations creation ON creation.id=receipt.creation_id
        WHERE receipt.job_id=p_job_id AND receipt.lease_token=p_lease_token
          AND receipt.entry_index=p_index AND receipt.item_id=v_entry.item_id
          AND receipt.operation_id::text=p_completion->>'verification_operation_id'
          AND receipt.state='completed' AND receipt.result IS NOT NULL
          AND receipt.size_bytes=v_file_size AND receipt.md5=v_content_hash
          AND receipt.sha256=creation.sha256 AND receipt.md5=creation.md5
          AND receipt.size_bytes=creation.size_bytes AND receipt.etag=creation.etag
          AND receipt.version_id IS NOT DISTINCT FROM creation.version_id
          AND (receipt.mode='read' OR creation.operation_id=receipt.operation_id)
          AND creation.dataset_id=v_dataset_id AND creation.owner_kind='dataset'
          AND creation.origin='connector' AND creation.status='verified'
          AND creation.authority->>'kind'='dataset_import'
          AND creation.authority->>'job_id'=p_job_id::text
          AND creation.authority->>'entry_index'=p_index::text
          AND creation.authority->>'item_id'=v_entry.item_id::text
          AND creation.authority->>'publication_id'=p_publication_id::text
          AND pg_catalog.substr(creation.object_key,
              pg_catalog.char_length(COALESCE(creation.configuration->'original_ref'->>'key_prefix',''))+1)
              =v_entry.file_path
          AND receipt.result->>'width' IS NOT DISTINCT FROM p_completion->>'width'
          AND receipt.result->>'height' IS NOT DISTINCT FROM p_completion->>'height'
        FOR SHARE OF creation, receipt
    ) THEN
        RETURN aap_assets_v1._dataset_import_reply(NULL, 'operation_conflict');
    END IF;
    v_next_cursor := v_operation.cursor + 1;
$after$;
BEGIN
    SELECT pg_catalog.pg_get_functiondef(
        'aap_assets_v1.complete_dataset_import_entry(uuid,uuid,bigint,uuid,jsonb)'::regprocedure
    ) INTO v_definition;
    IF pg_catalog.strpos(v_definition,v_before)=0
       OR pg_catalog.strpos(v_definition,v_after)<>0
       OR pg_catalog.strpos(v_definition,v_fields_before)=0
       OR pg_catalog.strpos(v_definition,v_fields_after)<>0
       OR pg_catalog.strpos(v_definition,v_validation_before)=0
       OR pg_catalog.strpos(v_definition,v_validation_after)<>0 THEN
        RAISE EXCEPTION 'dataset import publication body does not match expected source';
    END IF;
    EXECUTE pg_catalog.replace(
        pg_catalog.replace(
            pg_catalog.replace(v_definition,v_fields_before,v_fields_after),
            v_validation_before,v_validation_after
        ),v_before,v_after
    );
END;
$patch$;
