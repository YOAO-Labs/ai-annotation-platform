CREATE OR REPLACE FUNCTION aap_assets_v1.write_native_pyramid_member(
    p_operation uuid,p_etag text,p_version text
) RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_member aap_assets_v1.image_pyramid_native_members%ROWTYPE;
    v_scope jsonb;
    v_key text;
    v_key_digest bytea;
BEGIN
    IF p_etag IS NULL OR p_etag='' OR pg_catalog.length(p_etag)>1024
       OR (p_version IS NOT NULL AND (p_version='' OR p_version='null' OR pg_catalog.length(p_version)>1024)) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT * INTO v_member FROM aap_assets_v1.image_pyramid_native_members
    WHERE operation_id=p_operation FOR UPDATE;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF v_member.state='written' THEN
        IF v_member.etag IS DISTINCT FROM p_etag OR v_member.version_id IS DISTINCT FROM p_version THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id',v_member.output_id,'operation_id',v_member.operation_id,
            'relative_key',v_member.relative_key,'sha256',v_member.sha256,
            'size_bytes',v_member.size_bytes,'state',v_member.state
        ), NULL);
    END IF;
    IF v_member.state<>'submitted' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    UPDATE aap_assets_v1.image_pyramid_native_members
    SET state='written',etag=p_etag,version_id=p_version,updated_at=pg_catalog.clock_timestamp()
    WHERE operation_id=p_operation AND state='submitted';
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    v_scope := aap_assets_v1._native_pyramid_fixture_scope(v_member.output_id);
    IF v_scope IS NOT NULL THEN
        SELECT (storage_ref ->> 'prefix') || v_member.relative_key INTO v_key
        FROM public.image_pyramid_outputs WHERE id=v_member.output_id;
        v_key_digest := pg_catalog.sha256(pg_catalog.convert_to(v_key,'UTF8'));
        INSERT INTO aap_assets_v1.fixture_media_versions(
            id,run_id,dataset_id,item_id,task_id,execution_kind,execution_id,execution_token,
            owner_kind,owner_id,object_key,key_digest,sha256,size_bytes,state,etag,version_id
        ) VALUES (
            p_operation,(v_scope ->> 'run_id')::uuid,(v_scope ->> 'dataset_id')::uuid,
            (v_scope ->> 'item_id')::uuid,(v_scope ->> 'task_id')::uuid,'pyramid',
            (v_scope ->> 'job_id')::uuid,(v_scope ->> 'lease_token')::uuid,
            'pyramid_output',v_member.output_id,v_key,
            v_key_digest,v_member.sha256,v_member.size_bytes,'written',p_etag,p_version
        );
    END IF;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id',v_member.output_id,'operation_id',v_member.operation_id,
        'relative_key',v_member.relative_key,'sha256',v_member.sha256,
        'size_bytes',v_member.size_bytes,'state','written'
    ), NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.write_native_pyramid_member(uuid,text,text) FROM PUBLIC;
