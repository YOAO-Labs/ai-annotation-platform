-- Install fixed historical-audit projections for one trusted audit LOGIN.
-- This explicit installer also works before the Workspace migration schema exists.
-- It creates no login or Alembic revision and does not advance the database.
-- psql -v expected_database=... -v migration_role=... -v workspace_owner=...
--      -v consumer_role=... -f workspace-role-audit-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config('aap.audit_consumer', :'consumer_role', true);

DO $preflight$
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
END
$preflight$;

-- Temporary references freeze the three exact view definitions without
-- replacing an existing owner view during an idempotent install.
CREATE TEMP VIEW _aap_workspace_role_audit_project_ref
WITH (security_barrier = true) AS
SELECT project.id, project.owner_id, project.status
FROM public.projects AS project;

CREATE TEMP VIEW _aap_workspace_role_audit_member_ref
WITH (security_barrier = true) AS
SELECT member.id, member.project_id, member.user_id, member.role
FROM public.project_members AS member;

CREATE TEMP VIEW _aap_workspace_role_audit_capabilities_ref
WITH (security_barrier = true) AS
SELECT EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.project_members'::pg_catalog.regclass
      AND attribute.attname::text = 'version'
      AND attribute.attnum > 0 AND NOT attribute.attisdropped
) AS member_version,
EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.project_members'::pg_catalog.regclass
      AND attribute.attname::text = 'updated_at'
      AND attribute.attnum > 0 AND NOT attribute.attisdropped
) AS member_updated_at;

DO $install$
DECLARE
    v_migration name := current_setting('aap.migration_role');
    v_workspace name := current_setting('aap.workspace_owner');
    v_consumer name := current_setting('aap.audit_consumer');
    v_migration_oid oid;
    v_workspace_oid oid;
    v_consumer_oid oid;
    v_projects_oid oid;
    v_members_oid oid;
    v_schema_oid oid;
    v_view_oid oid;
    v_reference_oids oid[] := ARRAY[
        pg_catalog.to_regclass('pg_temp._aap_workspace_role_audit_project_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_workspace_role_audit_member_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_workspace_role_audit_capabilities_ref')::oid
    ];
    v_view_names text[] := ARRAY[
        'project_facts', 'project_member_facts', 'schema_capabilities'
    ];
    v_view_name text;
    v_all_project_columns text;
    v_all_member_columns text;
    v_view_columns text;
    v_columns text[];
    v_column_types text[];
    v_expected_columns text[];
    v_expected_types text[];
    v_view_definition text;
    v_granted_create boolean := false;
    v_index integer;
BEGIN
    IF v_migration !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_workspace !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_consumer !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_workspace = v_migration
       OR v_consumer IN (
            v_migration, v_workspace, 'workspace_api_consumer',
            'identity_api_consumer'
       ) THEN
        RAISE EXCEPTION 'Invalid or overlapping Workspace audit roles';
    END IF;
    IF pg_catalog.array_position(v_reference_oids, NULL::oid) IS NOT NULL THEN
        RAISE EXCEPTION 'Failed to create Workspace audit reference views';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(20261023, 302);

    SELECT role.oid INTO v_migration_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_migration;
    SELECT role.oid INTO v_workspace_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_workspace;
    SELECT role.oid INTO v_consumer_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_consumer;
    IF v_migration_oid IS NULL OR v_workspace_oid IS NULL OR v_consumer_oid IS NULL THEN
        RAISE EXCEPTION 'Migration, Workspace owner, and audit consumer roles must exist';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.oid = v_workspace_oid
          AND NOT role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.oid = v_consumer_oid
          AND role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Workspace owner must be NOLOGIN and audit consumer a restricted LOGIN';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = v_consumer_oid
           OR membership.roleid = v_consumer_oid
           OR membership.member = v_workspace_oid
           OR (membership.roleid = v_workspace_oid
               AND membership.member <> v_migration_oid)
    ) THEN
        RAISE EXCEPTION 'Audit consumer or Workspace owner has an unsafe role membership';
    END IF;
    IF NOT pg_catalog.pg_has_role(v_migration_oid, v_workspace_oid, 'MEMBER')
       AND NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_roles AS role
           WHERE role.oid = v_migration_oid AND role.rolsuper
       ) THEN
        RAISE EXCEPTION 'Migration role cannot transfer views to the Workspace owner';
    END IF;

    -- Validate only stable historical columns. The two optional preparation
    -- fields are reported by schema_capabilities instead of being assumed.
    IF EXISTS (
        SELECT 1
        FROM (VALUES
            ('projects', 'id', 'uuid'),
            ('projects', 'owner_id', 'uuid'),
            ('projects', 'status', 'character varying(30)'),
            ('project_members', 'id', 'uuid'),
            ('project_members', 'project_id', 'uuid'),
            ('project_members', 'user_id', 'uuid'),
            ('project_members', 'role', 'character varying(30)')
        ) AS expected(table_name, column_name, type_name)
        LEFT JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.nspname = 'public'
        LEFT JOIN pg_catalog.pg_class AS relation
          ON relation.relnamespace = namespace.oid
         AND relation.relname = expected.table_name
        LEFT JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = relation.oid
         AND attribute.attname = expected.column_name
         AND attribute.attnum > 0 AND NOT attribute.attisdropped
        WHERE relation.oid IS NULL OR relation.relkind <> 'r'
           OR relation.relowner <> v_migration_oid
           OR attribute.attnum IS NULL
           OR pg_catalog.format_type(attribute.atttypid, attribute.atttypmod)
                IS DISTINCT FROM expected.type_name
    ) THEN
        RAISE EXCEPTION 'Historical Project or ProjectMember source schema is incompatible';
    END IF;
    SELECT relation.oid INTO v_projects_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'projects';
    SELECT relation.oid INTO v_members_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'project_members';
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_index AS primary_key
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = primary_key.indrelid
         AND attribute.attname = 'id'
        WHERE primary_key.indrelid = v_projects_oid
          AND primary_key.indisprimary AND primary_key.indnatts = 1
          AND primary_key.indkey[0] = attribute.attnum
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_index AS primary_key
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = primary_key.indrelid
         AND attribute.attname = 'id'
        WHERE primary_key.indrelid = v_members_oid
          AND primary_key.indisprimary AND primary_key.indnatts = 1
          AND primary_key.indkey[0] = attribute.attnum
    ) THEN
        RAISE EXCEPTION 'Historical Project and ProjectMember IDs must remain primary keys';
    END IF;

    IF NOT pg_catalog.has_schema_privilege(v_workspace_oid, 'public', 'USAGE') THEN
        EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA public TO %I', v_workspace);
    END IF;
    EXECUTE pg_catalog.format(
        'GRANT SELECT (id, owner_id, status) ON TABLE public.projects TO %I',
        v_workspace
    );
    EXECUTE pg_catalog.format(
        'GRANT SELECT (id, project_id, user_id, role) '
        'ON TABLE public.project_members TO %I',
        v_workspace
    );

    SELECT namespace.oid INTO v_schema_oid
    FROM pg_catalog.pg_namespace AS namespace
    WHERE namespace.nspname = 'aap_workspace_audit_v1';
    IF v_schema_oid IS NULL THEN
        EXECUTE pg_catalog.format(
            'CREATE SCHEMA aap_workspace_audit_v1 AUTHORIZATION %I', v_workspace
        );
        SELECT namespace.oid INTO v_schema_oid
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'aap_workspace_audit_v1';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.oid = v_schema_oid
          AND namespace.nspowner = v_workspace_oid
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS relation
        WHERE relation.relnamespace = v_schema_oid
          AND relation.relname <> ALL (v_view_names)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS routine
        WHERE routine.pronamespace = v_schema_oid
    ) THEN
        RAISE EXCEPTION 'aap_workspace_audit_v1 has a foreign owner or unexpected object';
    END IF;

    FOR v_index IN 1..3 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('aap_workspace_audit_v1.%I', v_view_name)
        );
        IF v_view_oid IS NULL THEN
            IF NOT pg_catalog.has_schema_privilege(
                   v_migration_oid, v_schema_oid, 'CREATE'
               ) AND NOT v_granted_create THEN
                EXECUTE pg_catalog.format(
                    'GRANT CREATE ON SCHEMA aap_workspace_audit_v1 TO %I',
                    v_migration
                );
                v_granted_create := true;
            END IF;
            v_view_definition := pg_catalog.pg_get_viewdef(
                v_reference_oids[v_index], true
            );
            EXECUTE pg_catalog.format(
                'CREATE VIEW aap_workspace_audit_v1.%I '
                'WITH (security_barrier = true) AS %s',
                v_view_name, v_view_definition
            );
            EXECUTE pg_catalog.format(
                'ALTER VIEW aap_workspace_audit_v1.%I OWNER TO %I',
                v_view_name, v_workspace
            );
            v_view_oid := pg_catalog.to_regclass(
                pg_catalog.format('aap_workspace_audit_v1.%I', v_view_name)
            );
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_class AS relation
            WHERE relation.oid = v_view_oid AND relation.relkind = 'v'
              AND relation.relowner = v_workspace_oid
              AND COALESCE(relation.reloptions, ARRAY[]::text[])
                  @> ARRAY['security_barrier=true']
        ) THEN
            RAISE EXCEPTION '%.% has an unexpected owner, kind, or security option',
                'aap_workspace_audit_v1', v_view_name;
        END IF;
        SELECT pg_catalog.array_agg(attribute.attname::text ORDER BY attribute.attnum),
               pg_catalog.array_agg(
                   pg_catalog.format_type(attribute.atttypid, attribute.atttypmod)
                   ORDER BY attribute.attnum
               )
        INTO v_columns, v_column_types
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_view_oid
          AND attribute.attnum > 0 AND NOT attribute.attisdropped;
        CASE v_index
            WHEN 1 THEN
                v_expected_columns := ARRAY['id', 'owner_id', 'status'];
                v_expected_types := ARRAY[
                    'uuid', 'uuid', 'character varying(30)'
                ];
            WHEN 2 THEN
                v_expected_columns := ARRAY[
                    'id', 'project_id', 'user_id', 'role'
                ];
                v_expected_types := ARRAY[
                    'uuid', 'uuid', 'uuid', 'character varying(30)'
                ];
            ELSE
                v_expected_columns := ARRAY[
                    'member_version', 'member_updated_at'
                ];
                v_expected_types := ARRAY['boolean', 'boolean'];
        END CASE;
        IF v_columns IS DISTINCT FROM v_expected_columns
           OR v_column_types IS DISTINCT FROM v_expected_types
           OR pg_catalog.pg_get_viewdef(v_view_oid, true) IS DISTINCT FROM
              pg_catalog.pg_get_viewdef(v_reference_oids[v_index], true) THEN
            RAISE EXCEPTION '%.% has an unexpected definition or shape',
                'aap_workspace_audit_v1', v_view_name;
        END IF;
    END LOOP;
    IF v_granted_create THEN
        EXECUTE pg_catalog.format(
            'REVOKE CREATE ON SCHEMA aap_workspace_audit_v1 FROM %I', v_migration
        );
    END IF;

    EXECUTE 'REVOKE ALL ON SCHEMA aap_workspace_audit_v1 FROM PUBLIC';
    EXECUTE pg_catalog.format(
        'REVOKE ALL ON SCHEMA aap_workspace_audit_v1 FROM %I', v_consumer
    );
    SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                 ORDER BY attribute.attnum)
    INTO v_all_project_columns
    FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = v_projects_oid
      AND attribute.attnum > 0 AND NOT attribute.attisdropped;
    SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                 ORDER BY attribute.attnum)
    INTO v_all_member_columns
    FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = v_members_oid
      AND attribute.attnum > 0 AND NOT attribute.attisdropped;
    EXECUTE pg_catalog.format(
        'REVOKE ALL ON TABLE public.projects FROM %I', v_consumer
    );
    EXECUTE pg_catalog.format(
        'REVOKE ALL (%s) ON TABLE public.projects FROM %I',
        v_all_project_columns, v_consumer
    );
    EXECUTE pg_catalog.format(
        'REVOKE ALL ON TABLE public.project_members FROM %I', v_consumer
    );
    EXECUTE pg_catalog.format(
        'REVOKE ALL (%s) ON TABLE public.project_members FROM %I',
        v_all_member_columns, v_consumer
    );

    FOR v_index IN 1..3 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('aap_workspace_audit_v1.%I', v_view_name)
        );
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                     ORDER BY attribute.attnum)
        INTO v_view_columns
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_view_oid
          AND attribute.attnum > 0 AND NOT attribute.attisdropped;
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE aap_workspace_audit_v1.%I FROM PUBLIC',
            v_view_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE aap_workspace_audit_v1.%I FROM PUBLIC',
            v_view_columns, v_view_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE aap_workspace_audit_v1.%I FROM %I',
            v_view_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE aap_workspace_audit_v1.%I FROM %I',
            v_view_columns, v_view_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'GRANT SELECT ON TABLE aap_workspace_audit_v1.%I TO %I',
            v_view_name, v_consumer
        );
    END LOOP;
    EXECUTE pg_catalog.format(
        'GRANT USAGE ON SCHEMA aap_workspace_audit_v1 TO %I', v_consumer
    );

    IF pg_catalog.has_table_privilege(
           v_consumer_oid, v_projects_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_projects_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private public.projects access';
    END IF;
    IF pg_catalog.has_table_privilege(
           v_consumer_oid, v_members_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_members_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private public.project_members access';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
           v_consumer_oid, v_schema_oid, 'USAGE'
       ) OR pg_catalog.has_schema_privilege(
           v_consumer_oid, v_schema_oid, 'CREATE'
       ) THEN
        RAISE EXCEPTION 'Audit consumer has an unexpected Workspace audit schema privilege';
    END IF;
    FOR v_index IN 1..3 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('aap_workspace_audit_v1.%I', v_view_name)
        );
        IF NOT pg_catalog.has_table_privilege(
               v_consumer_oid, v_view_oid, 'SELECT'
           ) OR pg_catalog.has_table_privilege(
               v_consumer_oid, v_view_oid,
               'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
           ) OR pg_catalog.has_any_column_privilege(
               v_consumer_oid, v_view_oid, 'INSERT,UPDATE,REFERENCES'
           ) THEN
            RAISE EXCEPTION 'Audit consumer has an unexpected Workspace audit view privilege';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class AS relation
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    relation.relacl,
                    pg_catalog.acldefault('r', relation.relowner)
                )
            ) AS privilege
            WHERE relation.oid = v_view_oid AND privilege.grantee = 0
        ) THEN
            RAISE EXCEPTION 'PUBLIC retains access to a Workspace audit relation';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(
                namespace.nspacl,
                pg_catalog.acldefault('n', namespace.nspowner)
            )
        ) AS privilege
        WHERE namespace.oid = v_schema_oid AND privilege.grantee = 0
    ) THEN
        RAISE EXCEPTION 'PUBLIC retains access to aap_workspace_audit_v1';
    END IF;
END
$install$;
-- aap-statement-break
DROP VIEW pg_temp._aap_workspace_role_audit_capabilities_ref;
DROP VIEW pg_temp._aap_workspace_role_audit_member_ref;
DROP VIEW pg_temp._aap_workspace_role_audit_project_ref;
COMMIT;
