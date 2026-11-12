-- The existing reclaim fence hashes Python json.dumps(..., ensure_ascii=True,
-- separators=(',', ':')). Keep those exact bytes for independently deployed readers.
CREATE FUNCTION aap_assets_v1._managed_reclaim_json_string(p_value text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
STRICT
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_result text := '"';
    v_char text;
    v_code integer;
    v_offset integer;
    v_index integer;
BEGIN
    FOR v_index IN 1..pg_catalog.char_length(p_value) LOOP
        v_char := pg_catalog.substr(p_value, v_index, 1);
        v_code := pg_catalog.ascii(v_char);
        IF v_code = 34 OR v_code = 92 THEN
            v_result := v_result || pg_catalog.chr(92) || v_char;
        ELSIF v_code = 8 THEN
            v_result := v_result || pg_catalog.chr(92) || 'b';
        ELSIF v_code = 9 THEN
            v_result := v_result || pg_catalog.chr(92) || 't';
        ELSIF v_code = 10 THEN
            v_result := v_result || pg_catalog.chr(92) || 'n';
        ELSIF v_code = 12 THEN
            v_result := v_result || pg_catalog.chr(92) || 'f';
        ELSIF v_code = 13 THEN
            v_result := v_result || pg_catalog.chr(92) || 'r';
        ELSIF v_code < 32 OR v_code >= 127 THEN
            IF v_code <= 65535 THEN
                v_result := v_result || pg_catalog.chr(92) || 'u' || pg_catalog.lpad(pg_catalog.to_hex(v_code), 4, '0');
            ELSE
                v_offset := v_code - 65536;
                v_result := v_result || pg_catalog.chr(92) || 'u'
                    || pg_catalog.lpad(pg_catalog.to_hex(55296 + (v_offset / 1024)), 4, '0')
                    || pg_catalog.chr(92) || 'u'
                    || pg_catalog.lpad(pg_catalog.to_hex(56320 + (v_offset % 1024)), 4, '0');
            END IF;
        ELSE
            v_result := v_result || v_char;
        END IF;
    END LOOP;
    RETURN v_result || '"';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._managed_reclaim_json_string(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._managed_reclaim_lock_identity(p_bucket text, p_object_key text)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT '["managed-original-retention",'
        || aap_assets_v1._managed_reclaim_json_string(p_bucket) || ','
        || aap_assets_v1._managed_reclaim_json_string(p_object_key) || ']'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._managed_reclaim_lock_identity(text,text) FROM PUBLIC;
