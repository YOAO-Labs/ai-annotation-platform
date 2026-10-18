-- Run after migrations and module-data-grants as the migration role.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v assets_owner=assets_code -v asset_catalog_role=asset_catalog_client
--   -f infra/docker/assets-catalog-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.assets_owner', :'assets_owner', true);
SELECT set_config('aap.asset_catalog_role', :'asset_catalog_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    role_names text[] := ARRAY[
        current_setting('aap.assets_owner'),
        current_setting('aap.asset_catalog_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF current_setting('aap.migration_role') = current_setting('aap.assets_owner')
       OR current_setting('aap.migration_role') = current_setting('aap.asset_catalog_role')
       OR current_setting('aap.assets_owner') = current_setting('aap.asset_catalog_role') THEN
        RAISE EXCEPTION 'Migration, Assets owner and catalog LOGIN must be distinct';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe catalog role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing catalog role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'Unsafe catalog role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Catalog roles must not inherit or have members: %', role_name;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.assets_owner')
          AND role.rolcanlogin
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.asset_catalog_role')
          AND role.rolcanlogin
    ) THEN
        RAISE EXCEPTION 'Assets owner must be NOLOGIN and catalog role must LOGIN';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_catalog_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_catalog_role";

-- Remove any stale grants before applying the narrow catalog surface.
REVOKE ALL ON SCHEMA public FROM :"asset_catalog_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_catalog_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_catalog_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"asset_catalog_role";

DO $$
DECLARE
    namespace record;
    catalog_role text := current_setting('aap.asset_catalog_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, catalog_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, catalog_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, catalog_role
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, catalog_role
        );
    END LOOP;
END $$;

REVOKE ALL ON SCHEMA aap_assets_v1 FROM :"asset_catalog_role";
GRANT USAGE ON SCHEMA aap_assets_v1 TO :"asset_catalog_role";

-- The NOLOGIN Assets owner may read only catalog facts and write catalog fields.
GRANT SELECT (
    id, display_id, name, description, data_type, is_temporal, file_count,
    created_by, metadata, created_at, updated_at
), INSERT (
    id, display_id, name, description, data_type, is_temporal, file_count,
    created_by, metadata
), UPDATE (name, description, metadata, updated_at)
    ON public.datasets TO :"assets_owner";
GRANT USAGE ON SEQUENCE public.display_seq_datasets TO :"assets_owner";
GRANT SELECT (
    operation_id, actor_id, operation_kind, dataset_id, request, result, created_at
), INSERT (
    operation_id, actor_id, operation_kind, dataset_id, request, result
)
    ON public.dataset_catalog_operations TO :"assets_owner";

-- Function owners are NOLOGIN; the runtime receives only the frozen public calls.
GRANT CREATE ON SCHEMA aap_assets_v1 TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._catalog_failure(text) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._catalog_actor(uuid, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._catalog_dataset_summary(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.list_catalog_datasets(
    uuid, text, text, boolean, integer, bigint
) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.get_catalog_dataset(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.list_catalog_items(uuid, uuid, integer, bigint)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.create_catalog_dataset(uuid, uuid, jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.update_catalog_dataset(uuid, uuid, uuid, jsonb)
    OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA aap_assets_v1 FROM :"assets_owner";

GRANT EXECUTE ON FUNCTION aap_assets_v1.list_catalog_datasets(
    uuid, text, text, boolean, integer, bigint
), aap_assets_v1.get_catalog_dataset(uuid, uuid),
    aap_assets_v1.list_catalog_items(uuid, uuid, integer, bigint),
    aap_assets_v1.create_catalog_dataset(uuid, uuid, jsonb),
    aap_assets_v1.update_catalog_dataset(uuid, uuid, uuid, jsonb),
    aap_assets_v1.list_dataset_projects(uuid, uuid),
    aap_assets_v1.list_project_datasets(uuid, uuid)
    TO :"asset_catalog_role";

GRANT EXECUTE ON FUNCTION
    aap_assets_v1.guard_spatial_actor(uuid,text),
    aap_assets_v1.list_spatial_scenes(uuid,uuid,timestamp with time zone,uuid,integer),
    aap_assets_v1.get_spatial_scene(uuid,uuid),
    aap_assets_v1.read_spatial_layout(uuid,uuid,uuid,uuid,integer),
    aap_assets_v1.read_spatial_items(uuid,uuid[],uuid,integer),
    aap_assets_v1.read_spatial_poses(uuid,uuid,integer,integer),
    aap_assets_v1.read_spatial_pose_pairs(jsonb),
    aap_assets_v1.read_spatial_calibrations(uuid[],boolean,uuid,integer,integer),
    aap_assets_v1.lock_spatial_calibration_facts(uuid,uuid,text,uuid,jsonb),
    aap_assets_v1.mutate_spatial_scene(uuid,uuid,uuid,jsonb,jsonb),
    aap_assets_v1.apply_spatial_layout(uuid,uuid,uuid,text,boolean,jsonb,jsonb),
    aap_assets_v1.read_spatial_layout_receipt(uuid,uuid,uuid,text,boolean),
    aap_assets_v1.upsert_spatial_poses(uuid,uuid,jsonb),
    aap_assets_v1.create_spatial_scene(uuid,uuid,uuid,text,text,jsonb,uuid),
    aap_assets_v1.assign_spatial_items(uuid,uuid,jsonb),
    aap_assets_v1.set_spatial_scene_source(uuid,uuid,text,jsonb),
    aap_assets_v1.update_spatial_calibration(uuid,uuid,uuid,text,integer,text,jsonb,text,text,jsonb,jsonb),
    aap_assets_v1.read_pointcloud_preparation(uuid,uuid,integer),
    aap_assets_v1.apply_pointcloud_preparation(uuid,jsonb,jsonb,jsonb),
    aap_assets_v1.read_spatial_camera_target(uuid,uuid,text),
    aap_assets_v1.record_spatial_calibration_provenance(uuid,uuid,jsonb),
    aap_assets_v1.set_spatial_dataset_axis(uuid,uuid,text)
    TO :"asset_catalog_role";

-- Fail closed if the runtime role can access anything beyond the exact protocol.
DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.asset_catalog_role')
    );
    allowed_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure(
            'aap_assets_v1.list_catalog_datasets(uuid,text,text,boolean,integer,bigint)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.get_catalog_dataset(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.list_catalog_items(uuid,uuid,integer,bigint)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.create_catalog_dataset(uuid,uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.update_catalog_dataset(uuid,uuid,uuid,jsonb)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.list_dataset_projects(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure(
            'aap_assets_v1.list_project_datasets(uuid,uuid)'
        )::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.guard_spatial_actor(uuid,text)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.list_spatial_scenes(uuid,uuid,timestamp with time zone,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.get_spatial_scene(uuid,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_spatial_layout(uuid,uuid,uuid,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_spatial_items(uuid,uuid[],uuid,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_spatial_poses(uuid,uuid,integer,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_spatial_pose_pairs(jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_spatial_calibrations(uuid[],boolean,uuid,integer,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.lock_spatial_calibration_facts(uuid,uuid,text,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.mutate_spatial_scene(uuid,uuid,uuid,jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.apply_spatial_layout(uuid,uuid,uuid,text,boolean,jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_spatial_layout_receipt(uuid,uuid,uuid,text,boolean)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.upsert_spatial_poses(uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.create_spatial_scene(uuid,uuid,uuid,text,text,jsonb,uuid)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.assign_spatial_items(uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.set_spatial_scene_source(uuid,uuid,text,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.update_spatial_calibration(uuid,uuid,uuid,text,integer,text,jsonb,text,text,jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_pointcloud_preparation(uuid,uuid,integer)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.apply_pointcloud_preparation(uuid,jsonb,jsonb,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.read_spatial_camera_target(uuid,uuid,text)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.record_spatial_calibration_provenance(uuid,uuid,jsonb)')::oid,
        pg_catalog.to_regprocedure('aap_assets_v1.set_spatial_dataset_axis(uuid,uuid,text)')::oid
    ];
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND ns.nspname <> 'aap_assets_v1'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Catalog LOGIN has access to a foreign module schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'CREATE')
    ) THEN
        RAISE EXCEPTION 'Catalog LOGIN can create objects in a schema';
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
        RAISE EXCEPTION 'Catalog LOGIN has direct table privileges';
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
        RAISE EXCEPTION 'Catalog LOGIN has direct column privileges';
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
        RAISE EXCEPTION 'Catalog LOGIN has direct sequence privileges';
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
        RAISE EXCEPTION 'Catalog LOGIN has an unapproved module function grant';
    END IF;
END $$;
COMMIT;
