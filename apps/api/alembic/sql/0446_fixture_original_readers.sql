-- Legacy image/video readers use exact canonical source identities, so a
-- sealed fixture can fence its readers without draining neighbouring objects.
CREATE FUNCTION aap_assets_v1.fixture_media_original_run(p_kind text,p_content uuid,p_location uuid)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_run uuid; v_valid boolean;
BEGIN
    IF p_kind='dataset_asset' THEN
        SELECT resource.run_id INTO v_run FROM public.dataset_assets AS asset
        JOIN yoao_fixture_v1.fixture_run_resources AS resource
          ON resource.kind='item' AND resource.resource_id=asset.dataset_item_id
        WHERE asset.id=p_content;
        IF NOT FOUND THEN
            IF EXISTS (
                SELECT 1 FROM public.dataset_asset_locations AS source
                LEFT JOIN public.managed_original_creations AS creation ON creation.id=source.creation_id
                WHERE source.asset_id=p_content AND (
                    EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_original_targets AS target
                            WHERE target.location_id=source.location_id AND target.bucket=source.bucket)
                    OR (creation.id IS NOT NULL AND aap_assets_v1.is_fixture_original_source(
                        source.object_key,creation.storage_identity))
                )
            ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_original_scope_unknown'; END IF;
            RETURN NULL;
        END IF;
        SELECT EXISTS (
            SELECT 1 FROM public.dataset_asset_locations AS source
            JOIN yoao_fixture_v1.fixture_run_resources AS resource
              ON resource.run_id=v_run AND resource.kind='creation'
             AND resource.creation_id=source.creation_id AND resource.state='published'
            JOIN public.dataset_assets AS asset ON asset.id=source.asset_id AND asset.dataset_item_id=resource.item_id
            JOIN public.managed_original_creations AS creation ON creation.id=source.creation_id
            WHERE source.asset_id=p_content AND source.role='managed_original' AND source.reference_state='present'
              AND (source.location_id=p_location OR EXISTS (
                  SELECT 1 FROM public.storage_migration_read_publications AS publication
                  JOIN public.storage_migration_read_routes AS route ON route.publication_id=publication.id
                  WHERE publication.state='active' AND publication.source_location_id=source.location_id
                    AND publication.target_location_id=p_location AND route.bucket=source.bucket
                    AND route.object_key=source.object_key AND route.sha256=asset.checksum_value
                    AND route.size_bytes=asset.size_bytes AND route.source_etag=creation.etag
                    AND route.source_version_id IS NOT DISTINCT FROM creation.version_id
              ))
        ) INTO v_valid;
    ELSIF p_kind='task_original' THEN
        SELECT resource.run_id INTO v_run FROM public.task_originals AS original
        JOIN yoao_fixture_v1.fixture_run_resources AS resource
          ON resource.kind='task' AND resource.resource_id=original.task_id
        WHERE original.id=p_content;
        IF NOT FOUND THEN
            IF EXISTS (
                SELECT 1 FROM public.task_originals AS original WHERE original.id=p_content
                  AND aap_assets_v1.is_fixture_original_source(original.object_key,original.storage_identity)
            ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_original_scope_unknown'; END IF;
            RETURN NULL;
        END IF;
        SELECT EXISTS (
            SELECT 1 FROM public.task_originals AS original
            JOIN public.managed_original_creations AS creation ON creation.id=original.creation_id
            JOIN yoao_fixture_v1.fixture_run_resources AS resource
              ON resource.run_id=v_run AND resource.kind='task'
             AND resource.resource_id=original.task_id AND resource.state='published'
            WHERE original.id=p_content AND creation.status='verified'
              AND (original.location_id=p_location OR EXISTS (
                  SELECT 1 FROM public.storage_migration_read_publications AS publication
                  JOIN public.storage_migration_read_routes AS route ON route.publication_id=publication.id
                  WHERE publication.state='active' AND publication.source_location_id=original.location_id
                    AND publication.target_location_id=p_location AND route.bucket=original.bucket
                    AND route.object_key=original.object_key AND route.sha256=original.sha256
                    AND route.size_bytes=original.size_bytes AND route.source_etag=original.etag
                    AND route.source_version_id IS NOT DISTINCT FROM original.version_id
              ))
        ) INTO v_valid;
    ELSE
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_original_kind_invalid';
    END IF;
    IF NOT v_valid THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_original_identity_changed'; END IF;
    RETURN v_run;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_media_original_run(text,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_media_original_purpose(p_kind text,p_content uuid,p_location uuid,p_family text)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_run uuid; v_root uuid; v_state text; v_root_state text;
BEGIN
    IF p_content IS NULL OR p_location IS NULL OR p_family NOT IN ('image','video') THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_original_identity_invalid';
    END IF;
    v_run := aap_assets_v1.fixture_media_original_run(p_kind,p_content,p_location);
    IF v_run IS NULL THEN
        RETURN 'media_' || p_family || '_' || CASE p_kind WHEN 'dataset_asset' THEN 'a_' ELSE 't_' END
            || pg_catalog.replace(p_content::text,'-','');
    END IF;
    SELECT COALESCE(run.parent_run_id,run.id) INTO v_root FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id=v_run;
    SELECT state INTO v_root_state FROM yoao_fixture_v1.fixture_runs WHERE id=v_root AND parent_run_id IS NULL FOR SHARE;
    SELECT state INTO v_state FROM yoao_fixture_v1.fixture_runs
    WHERE id=v_run AND COALESCE(parent_run_id,id)=v_root FOR SHARE;
    IF v_state IS DISTINCT FROM 'open' OR v_root_state IS DISTINCT FROM 'open' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_original_read_closed';
    END IF;
    RETURN 'media_' || p_family || '_' || CASE p_kind WHEN 'dataset_asset' THEN 'a_' ELSE 't_' END
        || pg_catalog.replace(p_content::text,'-','');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_media_original_purpose(text,uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_original_read_permit()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_kind text; v_content uuid; v_family text; v_expected text;
BEGIN
    IF NEW.purpose !~ '^media_(image|video)_[at]_[0-9a-f]{32}$' THEN RETURN NEW; END IF;
    v_family := CASE WHEN pg_catalog.starts_with(NEW.purpose,'media_image_') THEN 'image' ELSE 'video' END;
    v_kind := CASE WHEN pg_catalog.split_part(NEW.purpose,'_',3)='a' THEN 'dataset_asset' ELSE 'task_original' END;
    v_content := pg_catalog.split_part(NEW.purpose,'_',4)::uuid;
    v_expected := aap_assets_v1.fixture_media_original_purpose(v_kind,v_content,NEW.location_id,v_family);
    IF v_expected IS DISTINCT FROM NEW.purpose THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_original_identity_changed';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_original_read_permit() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_original_read_permit BEFORE INSERT ON public.storage_operations
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_original_read_permit();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_dataset_legacy_original_readers_retired(p_run uuid,p_dataset uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        JOIN public.dataset_items AS item ON item.id=resource.resource_id
        JOIN public.dataset_assets AS asset ON asset.dataset_item_id=item.id
        JOIN public.storage_operations AS operation ON operation.purpose IN (
            'media_image_a_' || pg_catalog.replace(asset.id::text,'-',''),
            'media_video_a_' || pg_catalog.replace(asset.id::text,'-','')
        )
        WHERE resource.run_id=p_run AND resource.kind='item' AND item.dataset_id=p_dataset
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_dataset_legacy_original_readers_retired(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_task_legacy_original_readers_retired(p_run uuid,p_task uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        JOIN public.task_originals AS original ON original.task_id=resource.resource_id
        JOIN public.storage_operations AS operation ON operation.purpose IN (
            'media_image_t_' || pg_catalog.replace(original.id::text,'-',''),
            'media_video_t_' || pg_catalog.replace(original.id::text,'-','')
        )
        WHERE resource.run_id=p_run AND resource.kind='task' AND resource.resource_id=p_task
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_task_legacy_original_readers_retired(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.fixture_original_readers_empty(p_run_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()),5)<>'_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()),4)<>'_e2e' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture reader status requires a test database';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id=p_run_id AND run.state='sealing'
          AND ((run.family='owned' AND run.parent_run_id IS NOT NULL)
            OR (run.family IN ('screenshot','scale') AND run.parent_run_id IS NULL))
    ) THEN RETURN NULL; END IF;
    RETURN NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS source
        LEFT JOIN public.managed_original_creations AS creation ON creation.id=source.creation_id
        LEFT JOIN public.storage_operations AS operation ON operation.location_id=creation.location_id
          AND operation.purpose='media_original_' || pg_catalog.replace(creation.id::text,'-','')
          AND operation.node_id='aap-assets-media'
        WHERE source.run_id=p_run_id AND source.kind='creation'
          AND (creation.id IS NULL OR operation.id IS NOT NULL)
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_external_media_sources AS source
        WHERE source.run_id=p_run_id AND source.state<>'settled'
    ) AND NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS dataset
        WHERE dataset.run_id=p_run_id AND dataset.kind='dataset'
          AND aap_assets_v1.fixture_dataset_legacy_original_readers_retired(p_run_id,dataset.resource_id) IS DISTINCT FROM true
    ) AND NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS task
        WHERE task.run_id=p_run_id AND task.kind='task'
          AND aap_assets_v1.fixture_task_legacy_original_readers_retired(p_run_id,task.resource_id) IS DISTINCT FROM true
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_original_readers_empty(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.notify_fixture_legacy_original_reader()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_run uuid; v_content uuid;
BEGIN
    IF OLD.purpose !~ '^media_(image|video)_[at]_[0-9a-f]{32}$' THEN RETURN OLD; END IF;
    v_content := pg_catalog.split_part(OLD.purpose,'_',4)::uuid;
    IF pg_catalog.split_part(OLD.purpose,'_',3)='a' THEN
        SELECT resource.run_id INTO v_run FROM public.dataset_assets AS asset
        JOIN yoao_fixture_v1.fixture_run_resources AS resource
          ON resource.kind='item' AND resource.resource_id=asset.dataset_item_id
        WHERE asset.id=v_content;
    ELSE
        SELECT resource.run_id INTO v_run FROM public.task_originals AS original
        JOIN yoao_fixture_v1.fixture_run_resources AS resource
          ON resource.kind='task' AND resource.resource_id=original.task_id
        WHERE original.id=v_content;
    END IF;
    IF v_run IS NOT NULL THEN
        PERFORM pg_catalog.pg_notify('aap_fixture_reader_' || pg_catalog.replace(v_run::text,'-',''),'');
    END IF;
    RETURN OLD;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.notify_fixture_legacy_original_reader() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_legacy_original_reader_notify AFTER DELETE ON public.storage_operations
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.notify_fixture_legacy_original_reader();
