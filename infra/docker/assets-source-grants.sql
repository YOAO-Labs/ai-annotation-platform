-- Run after migrations and module-data-grants as the migration role.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v assets_owner=assets_code -v asset_source_role=asset_source_client
--   -f infra/docker/assets-source-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.assets_owner', :'assets_owner', true);
SELECT set_config('aap.asset_source_role', :'asset_source_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    role_names text[] := ARRAY[
        current_setting('aap.assets_owner'),
        current_setting('aap.asset_source_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF current_setting('aap.migration_role') = current_setting('aap.assets_owner')
       OR current_setting('aap.migration_role') = current_setting('aap.asset_source_role')
       OR current_setting('aap.assets_owner') = current_setting('aap.asset_source_role') THEN
        RAISE EXCEPTION 'Migration, Assets owner and source LOGIN must be distinct';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe source role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing source role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'Unsafe source role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Source roles must not inherit or have members: %', role_name;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.assets_owner')
          AND role.rolcanlogin
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.asset_source_role')
          AND role.rolcanlogin
    ) THEN
        RAISE EXCEPTION 'Assets owner must be NOLOGIN and source role must LOGIN';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_source_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_source_role";

-- Remove any stale grants before applying the narrow source surface.
REVOKE ALL ON SCHEMA public FROM :"asset_source_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_source_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_source_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"asset_source_role";

DO $$
DECLARE
    namespace record;
    source_role text := current_setting('aap.asset_source_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, source_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, source_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, source_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, source_role
        );
    END LOOP;
END $$;

REVOKE ALL ON SCHEMA aap_assets_v1 FROM :"asset_source_role";
GRANT USAGE ON SCHEMA aap_assets_v1 TO :"asset_source_role";

-- The runtime can execute only Source and verified-original capabilities.
GRANT EXECUTE ON FUNCTION aap_assets_v1.reserve_dataset_upload(uuid,uuid,uuid,text,text),
    aap_assets_v1.begin_operator_auxiliary(jsonb),
    aap_assets_v1.open_operator_storage_operation(uuid),
    aap_assets_v1.record_operator_original_prepared(uuid,jsonb),
    aap_assets_v1.mark_operator_original_writing(uuid),
    aap_assets_v1.record_operator_original_written(uuid,jsonb),
    aap_assets_v1.record_operator_original_verified(uuid,jsonb),
    aap_assets_v1.fail_operator_original(uuid,text,text,jsonb),
    aap_assets_v1.close_operator_storage_operation(uuid,uuid,boolean),
    aap_assets_v1.lock_dataset_upload(uuid,uuid,uuid),
    aap_assets_v1.find_dataset_upload_duplicate(uuid,uuid,uuid,text),
    aap_assets_v1.complete_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer),
    aap_assets_v1.discard_dataset_upload(uuid,uuid,uuid,text),
    aap_assets_v1.delete_dataset(uuid,uuid,uuid,text,jsonb),
    aap_assets_v1.delete_dataset_item(uuid,uuid,uuid,uuid,text,jsonb),
    aap_assets_v1.lock_legacy_dataset_upload(uuid,uuid,uuid,uuid),
    aap_assets_v1.complete_legacy_dataset_upload(uuid,uuid,uuid,uuid,text,bigint,text,integer,integer),
    aap_assets_v1.begin_dataset_ingest(uuid,uuid,uuid,jsonb),
    aap_assets_v1.prepare_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb),
    aap_assets_v1.complete_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb),
    aap_assets_v1.read_dataset_ingest_layout(uuid,uuid,uuid,uuid,integer),
    aap_assets_v1.finish_dataset_ingest(uuid,uuid,uuid,jsonb),
    aap_assets_v1.list_storage_connections(uuid,timestamptz,uuid,integer),
    aap_assets_v1.read_storage_connection(uuid,uuid),
    aap_assets_v1.read_storage_connection_policy(uuid,jsonb),
    aap_assets_v1.capture_storage_connection(uuid,uuid,text),
    aap_assets_v1.read_storage_connection_receipt(uuid,uuid,text,uuid,jsonb),
    aap_assets_v1.create_storage_connection(uuid,uuid,jsonb,jsonb,text,text,jsonb,jsonb),
    aap_assets_v1.update_storage_connection_authorized(uuid,uuid,uuid,jsonb,jsonb,text,text,text,jsonb,jsonb,uuid,text[],jsonb),
    aap_assets_v1.begin_connection_probe(uuid,uuid,uuid,text[]),
    aap_assets_v1.finish_connection_probe(uuid,uuid,uuid,uuid,text[],jsonb,boolean),
    aap_assets_v1.delete_storage_connection(uuid,uuid,uuid,jsonb),
    aap_assets_v1.record_storage_connection_test(uuid,uuid,uuid,text,text,jsonb,jsonb,jsonb),
    aap_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb),
    aap_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean),
    aap_assets_v1.read_legacy_dataset_upload(uuid,uuid,uuid,uuid)
    TO :"asset_source_role";

-- Fail closed if the runtime role can access anything beyond the exact protocol.
DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.asset_source_role')
    );
    allowed_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('aap_assets_v1.begin_operator_auxiliary(jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.open_operator_storage_operation(uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.record_operator_original_prepared(uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.mark_operator_original_writing(uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.record_operator_original_written(uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.record_operator_original_verified(uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.fail_operator_original(uuid,text,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.close_operator_storage_operation(uuid,uuid,boolean)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.reserve_dataset_upload(uuid,uuid,uuid,text,text)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.lock_dataset_upload(uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.find_dataset_upload_duplicate(uuid,uuid,uuid,text)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.complete_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.discard_dataset_upload(uuid,uuid,uuid,text)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.delete_dataset(uuid,uuid,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.delete_dataset_item(uuid,uuid,uuid,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.lock_legacy_dataset_upload(uuid,uuid,uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.complete_legacy_dataset_upload(uuid,uuid,uuid,uuid,text,bigint,text,integer,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.begin_dataset_ingest(uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.prepare_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.complete_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_dataset_ingest_layout(uuid,uuid,uuid,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.finish_dataset_ingest(uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.list_storage_connections(uuid,timestamptz,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_storage_connection(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_storage_connection_policy(uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.capture_storage_connection(uuid,uuid,text)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_storage_connection_receipt(uuid,uuid,text,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.create_storage_connection(uuid,uuid,jsonb,jsonb,text,text,jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.update_storage_connection_authorized(uuid,uuid,uuid,jsonb,jsonb,text,text,text,jsonb,jsonb,uuid,text[],jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.begin_connection_probe(uuid,uuid,uuid,text[])')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.finish_connection_probe(uuid,uuid,uuid,uuid,text[],jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.delete_storage_connection(uuid,uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.record_storage_connection_test(uuid,uuid,uuid,text,text,jsonb,jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_legacy_dataset_upload(uuid,uuid,uuid,uuid)')::oid
    ];
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND ns.nspname <> 'aap_assets_v1'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Source LOGIN has access to a foreign module schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'CREATE')
    ) THEN
        RAISE EXCEPTION 'Source LOGIN can create objects in a schema';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = ns.oid
        CROSS JOIN (VALUES
            ('SELECT'::text), ('INSERT'), ('UPDATE'), ('DELETE'),
            ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
        ) AS privilege(name)
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND pg_catalog.has_table_privilege(role_oid, relation.oid, privilege.name)
    ) THEN
        RAISE EXCEPTION 'Source LOGIN has direct table privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = ns.oid
        CROSS JOIN (VALUES
            ('SELECT'::text), ('INSERT'), ('UPDATE'), ('REFERENCES')
        ) AS privilege(name)
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND pg_catalog.has_any_column_privilege(
              role_oid, relation.oid, privilege.name
          )
    ) THEN
        RAISE EXCEPTION 'Source LOGIN has direct column privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = ns.oid
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind = 'S'
          AND (
              pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'USAGE')
              OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'SELECT')
              OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'UPDATE')
          )
    ) THEN
        RAISE EXCEPTION 'Source LOGIN has direct sequence privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS ns
        JOIN pg_catalog.pg_proc AS proc ON proc.pronamespace = ns.oid
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND proc.prokind IN ('f', 'p')
          AND proc.oid <> ALL (allowed_functions)
          AND pg_catalog.has_function_privilege(role_oid, proc.oid, 'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Source LOGIN has an unapproved module function grant';
    END IF;
END $$;
COMMIT;
