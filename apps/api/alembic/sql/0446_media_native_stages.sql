-- Native temporary directories belong to the same durable output operation.
-- Their exact path and node survive permit deletion as an immutable tombstone.
ALTER TABLE public.storage_operations ADD COLUMN native_stage jsonb;
-- aap-statement-break
CREATE TABLE aap_assets_v1.media_native_stages (
    operation_id uuid PRIMARY KEY,
    location_id uuid NOT NULL,
    node_id text NOT NULL,
    output_kind text NOT NULL CHECK (output_kind IN ('pyramid_output','video_output')),
    output_id uuid NOT NULL,
    path text NOT NULL,
    device text,
    inode text,
    state text NOT NULL CHECK (state IN ('prepared','opened','settled')),
    CHECK (path <> ''),
    CHECK ((state='prepared' AND device IS NULL AND inode IS NULL)
        OR (state IN ('opened','settled') AND device IS NOT NULL AND inode IS NOT NULL
            AND device ~ '^[0-9]+$' AND inode ~ '^[0-9]+$'))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.media_native_stages FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_media_native_stage_tombstone()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.operation_id,NEW.location_id,NEW.node_id,NEW.output_kind,NEW.output_id,NEW.path)
       IS DISTINCT FROM (OLD.operation_id,OLD.location_id,OLD.node_id,OLD.output_kind,OLD.output_id,OLD.path)
       OR (OLD.device IS NOT NULL AND (NEW.device,NEW.inode) IS DISTINCT FROM (OLD.device,OLD.inode))
       OR NOT ((OLD.state='prepared' AND NEW.state IN ('prepared','opened'))
            OR (OLD.state='opened' AND NEW.state IN ('opened','settled'))
            OR (OLD.state='settled' AND NEW.state='settled')) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_native_stage_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_media_native_stage_tombstone() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER media_native_stage_immutable BEFORE UPDATE OR DELETE ON aap_assets_v1.media_native_stages
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_media_native_stage_tombstone();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.track_media_native_stage()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_stage jsonb; v_output uuid; v_existing aap_assets_v1.media_native_stages%ROWTYPE;
BEGIN
    IF TG_OP='DELETE' THEN
        IF EXISTS (SELECT 1 FROM aap_assets_v1.media_native_stages
                   WHERE operation_id=OLD.id AND state <> 'settled') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_native_stage_unsettled';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.native_stage IS NOT NULL
       AND (NEW.id,NEW.location_id,NEW.node_id,NEW.purpose)
           IS DISTINCT FROM (OLD.id,OLD.location_id,OLD.node_id,OLD.purpose) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_native_stage_owner_changed';
    END IF;
    IF NEW.native_stage IS NOT DISTINCT FROM OLD.native_stage THEN RETURN NEW; END IF;
    v_stage := NEW.native_stage;
    IF pg_catalog.jsonb_typeof(v_stage) IS DISTINCT FROM 'object'
       OR NOT (v_stage ?& ARRAY['schema','kind','output_id','path','state','device','inode'])
       OR (v_stage - ARRAY['schema','kind','output_id','path','state','device','inode']) <> '{}'::jsonb
       OR v_stage -> 'schema' IS DISTINCT FROM '1'::jsonb
       OR v_stage ->> 'kind' NOT IN ('pyramid_output','video_output')
       OR pg_catalog.jsonb_typeof(v_stage -> 'output_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_stage -> 'path') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_stage -> 'state') IS DISTINCT FROM 'string'
       OR (v_stage ->> 'path') !~ ('^/([^/]+/)*aap-media-stage-' || NEW.id::text || '$')
       OR (v_stage ->> 'path') ~ '(^|/)[.][.]?(/|$)' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_native_stage_invalid';
    END IF;
    BEGIN v_output := (v_stage ->> 'output_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_native_stage_invalid';
    END;
    IF v_stage ->> 'output_id' IS DISTINCT FROM v_output::text
       OR (v_stage ->> 'kind'='pyramid_output' AND NEW.purpose IS DISTINCT FROM
           'image_pyramid_' || pg_catalog.replace(v_output::text,'-',''))
       OR (v_stage ->> 'kind'='video_output' AND NEW.purpose IS DISTINCT FROM
           'video_derivative_' || pg_catalog.replace(v_output::text,'-','')) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_native_stage_invalid';
    END IF;
    SELECT * INTO v_existing FROM aap_assets_v1.media_native_stages
    WHERE operation_id=NEW.id FOR UPDATE;
    IF NOT FOUND THEN
        IF OLD.native_stage IS NOT NULL OR v_stage ->> 'state' IS DISTINCT FROM 'prepared'
           OR NOT ((v_stage ->> 'kind'='pyramid_output' AND EXISTS (
               SELECT 1 FROM public.image_pyramid_outputs AS output
               WHERE output.id=v_output AND output.state='building'
                 AND output.storage_ref ->> 'location_id'=NEW.location_id::text))
             OR (v_stage ->> 'kind'='video_output' AND EXISTS (
               SELECT 1 FROM public.video_derivative_outputs AS output
               WHERE output.id=v_output AND output.state='building'
                 AND output.storage_ref ->> 'location_id'=NEW.location_id::text))) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_native_stage_owner_changed';
        END IF;
        INSERT INTO aap_assets_v1.media_native_stages(operation_id,location_id,node_id,output_kind,output_id,path,state)
        VALUES (NEW.id,NEW.location_id,NEW.node_id,v_stage ->> 'kind',v_output,v_stage ->> 'path','prepared');
        IF v_stage -> 'device' IS DISTINCT FROM 'null'::jsonb
           OR v_stage -> 'inode' IS DISTINCT FROM 'null'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_native_stage_invalid';
        END IF;
    ELSE
        IF (v_existing.location_id,v_existing.node_id,v_existing.output_kind,v_existing.output_id,v_existing.path)
           IS DISTINCT FROM (NEW.location_id,NEW.node_id,v_stage ->> 'kind',v_output,v_stage ->> 'path') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_native_stage_owner_changed';
        END IF;
        UPDATE aap_assets_v1.media_native_stages
        SET state=v_stage ->> 'state',device=v_stage ->> 'device',inode=v_stage ->> 'inode'
        WHERE operation_id=NEW.id;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.track_media_native_stage() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER media_native_stage_tracking BEFORE UPDATE OR DELETE ON public.storage_operations
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.track_media_native_stage();
