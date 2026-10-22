-- Install fixed historical-audit projections for one trusted audit LOGIN.
-- This explicit installer supports legacy schemas before aap_identity_v1 exists;
-- it creates no login, Alembic revision, runtime selector, or database credential.
-- psql -v expected_database=... -v migration_role=... -v identity_owner=...
--      -v consumer_role=... -f identity-role-audit-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.audit_consumer', :'consumer_role', true);

DO $preflight$
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
END
$preflight$;

-- These temporary references make reuse checks compare PostgreSQL's own exact
-- deparse of the three static definitions without altering an existing view OID.
CREATE TEMP VIEW _aap_identity_role_audit_account_ref
WITH (security_barrier = true) AS
SELECT account_row.id, account_row.role, account_row.is_active
FROM public.users AS account_row;

CREATE TEMP VIEW _aap_identity_role_audit_invitation_ref
WITH (security_barrier = true) AS
SELECT invitation.id, invitation.project_id, invitation.role,
       pg_catalog.to_jsonb(invitation)->>'project_role' AS project_role,
       invitation.expires_at, invitation.accepted_at, invitation.revoked_at,
       invitation.invited_by
FROM public.user_invitations AS invitation;

CREATE TEMP VIEW _aap_identity_role_audit_capabilities_ref
WITH (security_barrier = true) AS
SELECT EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.user_invitations'::pg_catalog.regclass
      AND attribute.attname::text = 'project_role'
      AND attribute.attnum > 0 AND NOT attribute.attisdropped
) AS invitation_project_role;

DO $install$
DECLARE
    v_migration name := current_setting('aap.migration_role');
    v_identity name := current_setting('aap.identity_owner');
    v_consumer name := current_setting('aap.audit_consumer');
    v_migration_oid oid;
    v_identity_oid oid;
    v_consumer_oid oid;
    v_users_oid oid;
    v_invitations_oid oid;
    v_schema_oid oid;
    v_view_oid oid;
    v_reference_oids oid[] := ARRAY[
        pg_catalog.to_regclass('pg_temp._aap_identity_role_audit_account_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_identity_role_audit_invitation_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_identity_role_audit_capabilities_ref')::oid
    ];
    v_view_names text[] := ARRAY[
        'account_roles', 'invitation_facts', 'schema_capabilities'
    ];
    v_view_name text;
    v_missing_columns text;
    v_all_user_columns text;
    v_all_invitation_columns text;
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
       OR v_identity !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_consumer !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_identity = v_migration
       OR v_consumer IN (v_migration, v_identity, 'identity_api_consumer') THEN
        RAISE EXCEPTION 'Invalid or overlapping Identity audit roles';
    END IF;
    IF pg_catalog.array_position(v_reference_oids, NULL::oid) IS NOT NULL THEN
        RAISE EXCEPTION 'Failed to create Identity audit reference views';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(20261023, 301);

    SELECT role.oid INTO v_migration_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = v_migration;
    SELECT role.oid INTO v_identity_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = v_identity;
    SELECT role.oid INTO v_consumer_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = v_consumer;
    IF v_migration_oid IS NULL OR v_identity_oid IS NULL OR v_consumer_oid IS NULL THEN
        RAISE EXCEPTION 'Migration, Identity owner, and audit consumer roles must exist';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.oid = v_identity_oid
          AND NOT role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.oid = v_consumer_oid
          AND role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Identity owner must be NOLOGIN and audit consumer a restricted LOGIN';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = v_consumer_oid OR membership.roleid = v_consumer_oid
           OR membership.member = v_identity_oid
           OR (membership.roleid = v_identity_oid
               AND membership.member <> v_migration_oid)
    ) THEN
        RAISE EXCEPTION 'Audit consumer or Identity owner has an unsafe role membership';
    END IF;
    IF NOT pg_catalog.pg_has_role(v_migration_oid, v_identity_oid, 'MEMBER')
       AND NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_roles AS role
           WHERE role.oid = v_migration_oid AND role.rolsuper
       ) THEN
        RAISE EXCEPTION 'Migration role cannot transfer views to the Identity owner';
    END IF;

    -- Validate both legacy source tables and their minimum pre-0173 columns.
    IF EXISTS (
        SELECT 1
        FROM (VALUES
            ('users', 'id', 'uuid', true),
            ('users', 'role', 'character varying(50)', false),
            ('users', 'is_active', 'boolean', false),
            ('user_invitations', 'id', 'uuid', true),
            ('user_invitations', 'project_id', 'uuid', false),
            ('user_invitations', 'role', 'character varying(32)', true),
            ('user_invitations', 'expires_at', 'timestamp with time zone', true),
            ('user_invitations', 'accepted_at', 'timestamp with time zone', false),
            ('user_invitations', 'revoked_at', 'timestamp with time zone', false),
            ('user_invitations', 'invited_by', 'uuid', true)
        ) AS expected(table_name, column_name, type_name, must_be_not_null)
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
           OR (expected.must_be_not_null AND NOT attribute.attnotnull)
    ) THEN
        RAISE EXCEPTION 'Historical User or invitation source schema is incompatible';
    END IF;
    SELECT relation.oid INTO v_users_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'users';
    SELECT relation.oid INTO v_invitations_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'user_invitations';
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_index AS primary_key
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = primary_key.indrelid AND attribute.attname = 'id'
        WHERE primary_key.indrelid = v_users_oid AND primary_key.indisprimary
          AND primary_key.indnatts = 1 AND primary_key.indkey[0] = attribute.attnum
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_index AS primary_key
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = primary_key.indrelid AND attribute.attname = 'id'
        WHERE primary_key.indrelid = v_invitations_oid AND primary_key.indisprimary
          AND primary_key.indnatts = 1 AND primary_key.indkey[0] = attribute.attnum
    ) THEN
        RAISE EXCEPTION 'Historical User or invitation IDs must remain primary keys';
    END IF;

    IF NOT pg_catalog.has_schema_privilege(v_identity_oid, 'public', 'USAGE') THEN
        EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA public TO %I', v_identity);
    END IF;
    SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                 ORDER BY attribute.attnum)
    INTO v_missing_columns
    FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = v_users_oid
      AND attribute.attname = ANY (ARRAY['id', 'role', 'is_active'])
      AND attribute.attnum > 0 AND NOT attribute.attisdropped
      AND NOT pg_catalog.has_column_privilege(
          v_identity_oid, v_users_oid, attribute.attname, 'SELECT'
      );
    IF v_missing_columns IS NOT NULL THEN
        EXECUTE pg_catalog.format(
            'GRANT SELECT (%s) ON TABLE public.users TO %I', v_missing_columns, v_identity
        );
    END IF;
    IF NOT pg_catalog.has_table_privilege(v_identity_oid, v_invitations_oid, 'SELECT') THEN
        EXECUTE pg_catalog.format(
            'GRANT SELECT ON TABLE public.user_invitations TO %I', v_identity
        );
    END IF;

    SELECT namespace.oid INTO v_schema_oid FROM pg_catalog.pg_namespace AS namespace
    WHERE namespace.nspname = 'aap_identity_audit_v1';
    IF v_schema_oid IS NULL THEN
        EXECUTE pg_catalog.format(
            'CREATE SCHEMA aap_identity_audit_v1 AUTHORIZATION %I', v_identity
        );
        SELECT namespace.oid INTO v_schema_oid FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'aap_identity_audit_v1';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.oid = v_schema_oid AND namespace.nspowner = v_identity_oid
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS relation
        WHERE relation.relnamespace = v_schema_oid
          AND relation.relname <> ALL (v_view_names)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS routine
        WHERE routine.pronamespace = v_schema_oid
    ) THEN
        RAISE EXCEPTION 'aap_identity_audit_v1 has a foreign owner or unexpected object';
    END IF;

    FOR v_index IN 1..3 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('aap_identity_audit_v1.%I', v_view_name)
        );
        IF v_view_oid IS NULL THEN
            IF NOT pg_catalog.has_schema_privilege(v_migration_oid, v_schema_oid, 'CREATE')
               AND NOT v_granted_create THEN
                EXECUTE pg_catalog.format(
                    'GRANT CREATE ON SCHEMA aap_identity_audit_v1 TO %I', v_migration
                );
                v_granted_create := true;
            END IF;
            v_view_definition := pg_catalog.pg_get_viewdef(v_reference_oids[v_index], true);
            EXECUTE pg_catalog.format(
                'CREATE VIEW aap_identity_audit_v1.%I WITH (security_barrier = true) AS %s',
                v_view_name, v_view_definition
            );
            EXECUTE pg_catalog.format(
                'ALTER VIEW aap_identity_audit_v1.%I OWNER TO %I', v_view_name, v_identity
            );
            v_view_oid := pg_catalog.to_regclass(
                pg_catalog.format('aap_identity_audit_v1.%I', v_view_name)
            );
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_class AS relation
            WHERE relation.oid = v_view_oid AND relation.relkind = 'v'
              AND relation.relowner = v_identity_oid
              AND COALESCE(relation.reloptions, ARRAY[]::text[])
                  @> ARRAY['security_barrier=true']
        ) THEN
            RAISE EXCEPTION '%.% has an unexpected owner, kind, or security option',
                'aap_identity_audit_v1', v_view_name;
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
                v_expected_columns := ARRAY['id', 'role', 'is_active'];
                v_expected_types := ARRAY['uuid', 'character varying(50)', 'boolean'];
            WHEN 2 THEN
                v_expected_columns := ARRAY[
                    'id', 'project_id', 'role', 'project_role', 'expires_at',
                    'accepted_at', 'revoked_at', 'invited_by'
                ];
                v_expected_types := ARRAY[
                    'uuid', 'uuid', 'character varying(32)', 'text',
                    'timestamp with time zone', 'timestamp with time zone',
                    'timestamp with time zone', 'uuid'
                ];
            ELSE
                v_expected_columns := ARRAY['invitation_project_role'];
                v_expected_types := ARRAY['boolean'];
        END CASE;
        IF v_columns IS DISTINCT FROM v_expected_columns
           OR v_column_types IS DISTINCT FROM v_expected_types
           OR pg_catalog.pg_get_viewdef(v_view_oid, true) IS DISTINCT FROM
              pg_catalog.pg_get_viewdef(v_reference_oids[v_index], true) THEN
            RAISE EXCEPTION '%.% has an unexpected definition or shape',
                'aap_identity_audit_v1', v_view_name;
        END IF;
    END LOOP;
    IF v_granted_create THEN
        EXECUTE pg_catalog.format(
            'REVOKE CREATE ON SCHEMA aap_identity_audit_v1 FROM %I', v_migration
        );
    END IF;

    EXECUTE 'REVOKE ALL ON SCHEMA aap_identity_audit_v1 FROM PUBLIC';
    EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA aap_identity_audit_v1 FROM %I', v_consumer);
    SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                 ORDER BY attribute.attnum)
    INTO v_all_user_columns FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = v_users_oid AND attribute.attnum > 0
      AND NOT attribute.attisdropped;
    EXECUTE pg_catalog.format('REVOKE ALL ON TABLE public.users FROM %I', v_consumer);
    EXECUTE pg_catalog.format(
        'REVOKE ALL (%s) ON TABLE public.users FROM %I', v_all_user_columns, v_consumer
    );
    SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                 ORDER BY attribute.attnum)
    INTO v_all_invitation_columns FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = v_invitations_oid AND attribute.attnum > 0
      AND NOT attribute.attisdropped;
    EXECUTE pg_catalog.format(
        'REVOKE ALL ON TABLE public.user_invitations FROM %I', v_consumer
    );
    EXECUTE pg_catalog.format(
        'REVOKE ALL (%s) ON TABLE public.user_invitations FROM %I',
        v_all_invitation_columns, v_consumer
    );
    FOR v_index IN 1..3 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('aap_identity_audit_v1.%I', v_view_name)
        );
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                     ORDER BY attribute.attnum)
        INTO v_view_columns FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_view_oid AND attribute.attnum > 0
          AND NOT attribute.attisdropped;
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE aap_identity_audit_v1.%I FROM PUBLIC', v_view_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE aap_identity_audit_v1.%I FROM PUBLIC',
            v_view_columns, v_view_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE aap_identity_audit_v1.%I FROM %I',
            v_view_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE aap_identity_audit_v1.%I FROM %I',
            v_view_columns, v_view_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'GRANT SELECT ON TABLE aap_identity_audit_v1.%I TO %I',
            v_view_name, v_consumer
        );
    END LOOP;
    EXECUTE pg_catalog.format(
        'GRANT USAGE ON SCHEMA aap_identity_audit_v1 TO %I', v_consumer
    );

    IF pg_catalog.has_table_privilege(v_consumer_oid, v_users_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_users_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private public.users access';
    END IF;
    IF pg_catalog.has_table_privilege(v_consumer_oid, v_invitations_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_invitations_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private invitation access';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(v_consumer_oid, v_schema_oid, 'USAGE')
       OR pg_catalog.has_schema_privilege(v_consumer_oid, v_schema_oid, 'CREATE') THEN
        RAISE EXCEPTION 'Audit consumer has an unexpected Identity audit schema privilege';
    END IF;
    FOR v_index IN 1..3 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('aap_identity_audit_v1.%I', v_view_name)
        );
        IF NOT pg_catalog.has_table_privilege(v_consumer_oid, v_view_oid, 'SELECT')
           OR pg_catalog.has_table_privilege(
               v_consumer_oid, v_view_oid,
               'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
           ) OR pg_catalog.has_any_column_privilege(
               v_consumer_oid, v_view_oid, 'INSERT,UPDATE,REFERENCES'
           ) THEN
            RAISE EXCEPTION 'Audit consumer has an unexpected view privilege';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class AS relation
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(relation.relacl, pg_catalog.acldefault('r', relation.relowner))
            ) AS privilege
            WHERE relation.oid = v_view_oid AND privilege.grantee = 0
        ) THEN
            RAISE EXCEPTION 'PUBLIC retains access to an Identity audit relation';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(namespace.nspacl, pg_catalog.acldefault('n', namespace.nspowner))
        ) AS privilege
        WHERE namespace.oid = v_schema_oid AND privilege.grantee = 0
    ) THEN
        RAISE EXCEPTION 'PUBLIC retains access to aap_identity_audit_v1';
    END IF;
END
$install$;
-- aap-statement-break
DROP VIEW pg_temp._aap_identity_role_audit_capabilities_ref;
DROP VIEW pg_temp._aap_identity_role_audit_invitation_ref;
DROP VIEW pg_temp._aap_identity_role_audit_account_ref;
COMMIT;
