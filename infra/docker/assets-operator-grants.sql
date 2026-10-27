-- Run after migrations, module-data-grants and assets-catalog-grants as the migration role.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v assets_owner=assets_code -v asset_operator_role=asset_operator_client
--   -f infra/docker/assets-operator-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.assets_owner', :'assets_owner', true);
SELECT set_config('aap.asset_operator_role', :'asset_operator_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    role_names text[] := ARRAY[
        current_setting('aap.assets_owner'),
        current_setting('aap.asset_operator_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF current_setting('aap.migration_role') = current_setting('aap.assets_owner')
       OR current_setting('aap.migration_role') = current_setting('aap.asset_operator_role')
       OR current_setting('aap.assets_owner') = current_setting('aap.asset_operator_role') THEN
        RAISE EXCEPTION 'Migration, Assets owner and operator LOGIN must be distinct';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe operator role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing operator role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls OR role.rolinherit)
        ) THEN
            RAISE EXCEPTION 'Unsafe operator role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Operator roles must not inherit or have members: %', role_name;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.assets_owner')
          AND role.rolcanlogin
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.asset_operator_role')
          AND role.rolcanlogin
    ) THEN
        RAISE EXCEPTION 'Assets owner must be NOLOGIN and operator role must LOGIN';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_operator_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_operator_role";
REVOKE ALL ON SCHEMA public FROM :"asset_operator_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_operator_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_operator_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"asset_operator_role";

DO $$
DECLARE
    namespace record;
    operator_role text := current_setting('aap.asset_operator_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, operator_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, operator_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, operator_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, operator_role
        );
    END LOOP;
END $$;

REVOKE ALL ON SCHEMA aap_assets_v1 FROM :"asset_operator_role";
GRANT USAGE ON SCHEMA aap_assets_v1 TO :"asset_operator_role";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.publish_operator_item_original(jsonb),
    aap_assets_v1.operator_item_original_facts(uuid,uuid),
    aap_assets_v1.operator_auxiliary_pointer(uuid,uuid,uuid),
    aap_assets_v1.begin_operator_auxiliary(jsonb),
    aap_assets_v1.publish_operator_auxiliary(uuid),
    aap_assets_v1.operator_auxiliary_owner(uuid,text,jsonb,uuid),
    aap_assets_v1.operator_auxiliary_material(uuid,uuid),
    aap_assets_v1.operator_item_original_material(uuid,uuid),
    aap_assets_v1.bind_operator_auxiliary_item(uuid,uuid,uuid),
    aap_assets_v1.bind_operator_auxiliary_scene(uuid,uuid,uuid),
    aap_assets_v1.operator_dataset_state(uuid),
    aap_assets_v1.operator_scene_state(uuid,uuid),
    aap_assets_v1.open_operator_storage_operation(uuid),
    aap_assets_v1.record_operator_original_prepared(uuid,jsonb),
    aap_assets_v1.mark_operator_original_writing(uuid),
    aap_assets_v1.record_operator_original_written(uuid,jsonb),
    aap_assets_v1.record_operator_original_verified(uuid,jsonb),
    aap_assets_v1.fail_operator_original(uuid,text,text,jsonb),
    aap_assets_v1.close_operator_storage_operation(uuid,uuid,boolean),
    aap_assets_v1.create_operator_image_dataset(uuid,uuid,text,text),
    aap_assets_v1.ensure_operator_nuscenes_dataset(uuid,uuid,jsonb),
    aap_assets_v1.append_operator_items(uuid,uuid,jsonb),
    aap_assets_v1.patch_operator_items(uuid,uuid,jsonb),
    aap_assets_v1.recount_operator_items(uuid,uuid),
    aap_assets_v1.read_operator_items(uuid,uuid,integer)
    TO :"asset_operator_role";

-- Fail closed if the operator LOGIN can access anything beyond the exact protocol.
DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.asset_operator_role')
    );
    allowed_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure(
            'aap_assets_v1.publish_operator_item_original(jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.operator_item_original_facts(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.operator_auxiliary_pointer(uuid,uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.begin_operator_auxiliary(jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.publish_operator_auxiliary(uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.operator_auxiliary_owner(uuid,text,jsonb,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.operator_auxiliary_material(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.operator_item_original_material(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.bind_operator_auxiliary_item(uuid,uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.bind_operator_auxiliary_scene(uuid,uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.operator_dataset_state(uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.operator_scene_state(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.open_operator_storage_operation(uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.record_operator_original_prepared(uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.mark_operator_original_writing(uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.record_operator_original_written(uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.record_operator_original_verified(uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.fail_operator_original(uuid,text,text,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.close_operator_storage_operation(uuid,uuid,boolean)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.create_operator_image_dataset(uuid,uuid,text,text)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.ensure_operator_nuscenes_dataset(uuid,uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.append_operator_items(uuid,uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.patch_operator_items(uuid,uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.recount_operator_items(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.read_operator_items(uuid,uuid,integer)'
        )::oid
    ];
BEGIN
    IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(role_oid, current_database(), 'TEMP') THEN
        RAISE EXCEPTION 'Operator LOGIN can create database or temporary objects';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND ns.nspname <> 'aap_assets_v1'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Operator LOGIN has access to a foreign module schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'CREATE')
    ) THEN
        RAISE EXCEPTION 'Operator LOGIN can create objects in a schema';
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
        RAISE EXCEPTION 'Operator LOGIN has direct table privileges';
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
        RAISE EXCEPTION 'Operator LOGIN has direct column privileges';
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
        RAISE EXCEPTION 'Operator LOGIN has direct sequence privileges';
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
        RAISE EXCEPTION 'Operator LOGIN has an unapproved module function grant';
    END IF;
END $$;
COMMIT;
