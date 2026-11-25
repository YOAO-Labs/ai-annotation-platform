\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.grant_section_applied', '0', true);

\if :{?grant_accounts}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.identity_account_role', :'identity_account_role', true);
SELECT set_config('yoao.identity_presence_role', :'identity_presence_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    owner_oid oid;
    migration_oid oid;
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.identity_account_role'),
        current_setting('yoao.identity_presence_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT pg_catalog.count(DISTINCT name)
        FROM pg_catalog.unnest(role_names) AS expanded(name)) <> 3
       OR current_setting('yoao.migration_role') = ANY(role_names) THEN
        RAISE EXCEPTION 'Migration, Identity owner, account and presence roles must be distinct';
    END IF;
    IF current_setting('yoao.identity_account_role') <> 'yoao_identity_account_client'
       OR current_setting('yoao.identity_presence_role') <> 'yoao_identity_presence_client' THEN
        RAISE EXCEPTION 'Self-account and presence must use their restricted LOGIN roles';
    END IF;

    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe Identity role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing Identity role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'Unsafe Identity role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Identity roles must have no role memberships: %', role_name;
        END IF;
    END LOOP;

    SELECT role.oid INTO owner_oid
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.identity_owner');
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.oid = owner_oid AND (role.rolcanlogin OR role.rolinherit)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = ANY(ARRAY[
            current_setting('yoao.identity_account_role'),
            current_setting('yoao.identity_presence_role')
        ]) AND (NOT role.rolcanlogin OR role.rolinherit)
    ) THEN
        RAISE EXCEPTION 'Identity owner must be NOLOGIN and clients LOGIN NOINHERIT';
    END IF;
    SELECT role.oid INTO migration_oid
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.migration_role');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Missing migration role';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'yoao_identity_accounts_v1'
          AND namespace.nspowner = migration_oid
    ) THEN
        RAISE EXCEPTION 'Identity account schema must remain owned by the migration role';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'yoao_identity_accounts_v1'
          AND function.prokind = 'f' AND function.proowner <> owner_oid
    ) THEN
        RAISE EXCEPTION 'Identity account functions must be owned by the Identity code owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        owner_oid,
        pg_catalog.to_regprocedure(
            'yoao_audit_v1.record_identity_account_event(text,jsonb,jsonb,jsonb)'
        )::oid,
        'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        owner_oid,
        pg_catalog.to_regprocedure('yoao_audit_v1.valid_request_context(jsonb)')::oid,
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Identity code owner cannot call the Audit-owned account event capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_account_role", :"identity_presence_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_account_role", :"identity_presence_role";
DO $$
DECLARE
    namespace record;
    role_name text;
    client_roles text[] := ARRAY[
        current_setting('yoao.identity_account_role'),
        current_setting('yoao.identity_presence_role')
    ];
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        FOREACH role_name IN ARRAY client_roles LOOP
            EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        END LOOP;
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_accounts_v1
    TO :"identity_account_role", :"identity_presence_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_accounts_v1.account_policy(),
    yoao_identity_accounts_v1.prepare_account_command(jsonb),
    yoao_identity_accounts_v1.apply_account_command(jsonb)
    TO :"identity_account_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz)
    TO :"identity_presence_role";

DO $$
DECLARE
    role_entry record;
    role_oid oid;
    audit_function oid := pg_catalog.to_regprocedure(
        'yoao_audit_v1.record_identity_account_event(text,jsonb,jsonb,jsonb)'
    )::oid;
    callable_count integer;
BEGIN
    IF audit_function IS NULL THEN
        RAISE EXCEPTION 'Identity account Audit function is missing';
    END IF;
    FOR role_entry IN
        SELECT role_name, allowed_functions
        FROM (VALUES
            (
                current_setting('yoao.identity_account_role'),
                ARRAY[
                    pg_catalog.to_regprocedure('yoao_identity_accounts_v1.account_policy()')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_accounts_v1.prepare_account_command(jsonb)')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_accounts_v1.apply_account_command(jsonb)')::oid
                ]::oid[]
            ),
            (
                current_setting('yoao.identity_presence_role'),
                ARRAY[
                    pg_catalog.to_regprocedure(
                        'yoao_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz)'
                    )::oid
                ]::oid[]
            )
        ) AS expected(role_name, allowed_functions)
    LOOP
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_entry.role_name;
        IF pg_catalog.array_position(role_entry.allowed_functions, NULL::oid) IS NOT NULL THEN
            RAISE EXCEPTION 'Identity account protocol function is missing for %', role_entry.role_name;
        END IF;
        IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE')
           OR pg_catalog.has_database_privilege(role_oid, current_database(), 'TEMP') THEN
            RAISE EXCEPTION 'Identity account LOGIN has database CREATE or TEMP: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
              AND namespace.nspname <> 'yoao_identity_accounts_v1'
              AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
        ) THEN
            RAISE EXCEPTION 'Identity account LOGIN has a foreign module schema: %', role_entry.role_name;
        END IF;
        IF pg_catalog.has_schema_privilege(role_oid, 'yoao_audit_v1', 'USAGE')
           OR pg_catalog.has_function_privilege(role_oid, audit_function, 'EXECUTE') THEN
            RAISE EXCEPTION 'Identity account LOGIN has direct Audit capability: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE')
        ) THEN
            RAISE EXCEPTION 'Identity account LOGIN can create schema objects: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
            CROSS JOIN (VALUES
                ('SELECT'::text), ('INSERT'), ('UPDATE'), ('DELETE'),
                ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
            ) AS privilege(name)
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.has_table_privilege(role_oid, relation.oid, privilege.name)
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
            CROSS JOIN (VALUES
                ('SELECT'::text), ('INSERT'), ('UPDATE'), ('REFERENCES')
            ) AS privilege(name)
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.has_any_column_privilege(role_oid, relation.oid, privilege.name)
        ) THEN
            RAISE EXCEPTION 'Identity account LOGIN has direct table privileges: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind = 'S'
              AND (pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'USAGE')
                   OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'SELECT')
                   OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'UPDATE'))
        ) THEN
            RAISE EXCEPTION 'Identity account LOGIN has direct sequence privileges: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
            WHERE namespace.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
              AND (namespace.nspname <> 'yoao_identity_accounts_v1'
                   OR function.oid <> ALL (role_entry.allowed_functions))
              AND function.prokind IN ('f', 'p')
              AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE')
        ) THEN
            RAISE EXCEPTION 'Identity account LOGIN has an unapproved module function: %', role_entry.role_name;
        END IF;
        SELECT pg_catalog.count(*) INTO callable_count
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'yoao_identity_accounts_v1'
          AND function.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
        IF callable_count <> pg_catalog.cardinality(role_entry.allowed_functions) THEN
            RAISE EXCEPTION 'Identity account LOGIN has the wrong function count: %', role_entry.role_name;
        END IF;
    END LOOP;
END $$;
\endif

\if :{?grant_actor_status}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.status_role', :'status_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.identity_owner'));
    v_status oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname=current_setting('yoao.status_role'));
    v_function oid := pg_catalog.to_regprocedure(
        'yoao_identity_v1.read_actor_facts(uuid)'
    );
BEGIN
    IF current_database()<>current_setting('yoao.expected_database')
       OR current_user<>current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF v_owner IS NULL OR v_status IS NULL
       OR v_owner=v_status
       OR current_user::pg_catalog.regrole=v_status THEN
        RAISE EXCEPTION 'Identity actor-status roles are missing or overlap';
    END IF;
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_roles AS role
        WHERE role.oid=v_owner AND NOT role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_roles AS role
        WHERE role.oid=v_status AND role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member IN (v_owner,v_status)
           OR membership.roleid=v_status
           OR (membership.roleid=v_owner AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Unsafe Identity actor-status role profile';
    END IF;
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='s'
          AND function.prokind='f'
          AND function.proretset
          AND function.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
    ) THEN
        RAISE EXCEPTION 'Identity active-status source function is missing or changed';
    END IF;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"status_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"status_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role name := current_setting('yoao.status_role');
BEGIN
    FOR v_schema IN
        SELECT namespace.nspname
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I',
            v_schema.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            v_schema.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            v_schema.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            v_schema.nspname,v_role);
    END LOOP;
END;
$revoke$;

GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"status_role";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid)
    TO :"status_role";

DO $verify$
DECLARE
    v_status oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname=current_setting('yoao.status_role'));
    v_allowed oid := pg_catalog.to_regprocedure(
        'yoao_identity_v1.read_actor_facts(uuid)'
    );
    v_relation record;
BEGIN
    IF v_allowed IS NULL
       OR NOT pg_catalog.has_function_privilege(v_status,v_allowed,'EXECUTE')
       OR pg_catalog.has_database_privilege(v_status,current_database(),'CREATE,TEMP') THEN
        RAISE EXCEPTION 'Identity actor-status LOGIN is missing its exact function profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=function.pronamespace
        JOIN pg_catalog.pg_type AS return_type
          ON return_type.oid=function.prorettype
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND function.prokind IN ('f','p','a','w')
          AND NOT (return_type.typnamespace='pg_catalog'::regnamespace
              AND return_type.typname IN ('trigger','event_trigger'))
          AND NOT EXISTS (
              SELECT FROM pg_catalog.pg_depend AS dependency
              WHERE dependency.classid='pg_catalog.pg_proc'::regclass
                AND dependency.objid=function.oid
                AND dependency.refclassid='pg_catalog.pg_extension'::regclass
                AND dependency.deptype='e'
          )
          AND (pg_catalog.left(namespace.nspname,4)='aap_' OR pg_catalog.left(namespace.nspname,5)='yoao_'
               OR function.prosecdef
               OR EXISTS (
                   SELECT FROM pg_catalog.aclexplode(COALESCE(function.proacl,
                       pg_catalog.acldefault('f',function.proowner))) AS acl
                   WHERE acl.privilege_type='EXECUTE' AND acl.grantee<>0
                     AND pg_catalog.pg_has_role(v_status,acl.grantee,'USAGE')
               ))
          AND function.oid<>v_allowed
          AND pg_catalog.has_function_privilege(v_status,function.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Identity actor-status LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema','public','yoao_identity_v1')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_status,namespace.oid,'USAGE')
               OR pg_catalog.has_schema_privilege(v_status,namespace.oid,'CREATE'))
    ) OR pg_catalog.has_schema_privilege(v_status,'public','CREATE')
      OR pg_catalog.has_schema_privilege(v_status,'yoao_identity_v1','CREATE') THEN
        RAISE EXCEPTION 'Identity actor-status LOGIN has foreign schema privileges';
    END IF;
    FOR v_relation IN
        SELECT relation.oid, relation.relkind
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r','p','v','m','f','S')
    LOOP
        IF (v_relation.relkind <> 'S' AND (
                pg_catalog.has_table_privilege(v_status,v_relation.oid,
                    'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR pg_catalog.has_any_column_privilege(v_status,v_relation.oid,
                    'SELECT,INSERT,UPDATE,REFERENCES')
            )) OR (v_relation.relkind = 'S' AND
                pg_catalog.has_sequence_privilege(v_status,v_relation.oid,
                    'SELECT,UPDATE,USAGE')) THEN
            RAISE EXCEPTION 'Identity actor-status LOGIN has direct data privileges';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=v_allowed AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Identity actor-status function is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_admin_passwords}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config(
    'yoao.identity_admin_password_role', :'identity_admin_password_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.identity_admin_password_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.audit_owner'),
        current_setting('yoao.identity_admin_password_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_identity_admin_password_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM unnest(role_names) AS candidate(name)) <> 4 THEN
        RAISE EXCEPTION 'Unexpected Identity admin-password role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Identity admin-password roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    audit_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.audit_owner')
    );
    admin_password_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_admin_password_role')
    );
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL
       OR audit_oid IS NULL OR admin_password_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Identity admin-password code owner or client';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_admin_passwords_v1.admin_password_policy()'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_admin_passwords_v1.execute_admin_password_command(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.admin_password_target_in_owned_project(uuid,uuid)'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_audit_v1.record_identity_admin_password_reset(jsonb,uuid,jsonb,jsonb)'
           )::oid) IS DISTINCT FROM audit_oid THEN
        RAISE EXCEPTION 'An Identity admin-password capability has the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.admin_password_target_in_owned_project(uuid,uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_audit_v1.record_identity_admin_password_reset(jsonb,uuid,jsonb,jsonb)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_admin_password_reset_receipts',
        'password_state_fingerprint', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        audit_oid, 'public.audit_logs', 'detail_json', 'INSERT'
    ) THEN
        RAISE EXCEPTION 'Identity owner is missing a reset receipt, Workspace or Audit capability';
    END IF;
    IF pg_catalog.has_table_privilege(
           identity_oid, 'public.projects', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.project_members', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           admin_password_oid, 'public.identity_admin_password_reset_receipts',
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           admin_password_oid, 'public.users', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) THEN
        RAISE EXCEPTION 'Identity admin-password owner/client has raw table access';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_admin_password_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_admin_password_role";
DO $$
DECLARE
    namespace record;
    column_grant record;
    role_name text := current_setting('yoao.identity_admin_password_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
    END LOOP;
    FOR column_grant IN
        SELECT grant_row.table_schema, grant_row.table_name,
               grant_row.column_name, grant_row.privilege_type
        FROM information_schema.column_privileges AS grant_row
        WHERE grant_row.grantee = role_name
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE %s (%I) ON TABLE %I.%I FROM %I',
            column_grant.privilege_type,
            column_grant.column_name,
            column_grant.table_schema,
            column_grant.table_name,
            role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_admin_passwords_v1
    TO :"identity_admin_password_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_admin_passwords_v1.admin_password_policy(),
    yoao_identity_admin_passwords_v1.execute_admin_password_command(jsonb)
    TO :"identity_admin_password_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_admin_password_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_identity_admin_passwords_v1.admin_password_policy()',
        'yoao_identity_admin_passwords_v1.execute_admin_password_command(jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND function.prorettype NOT IN (
          'trigger'::pg_catalog.regtype, 'event_trigger'::pg_catalog.regtype
      )
      AND NOT EXISTS (
          SELECT 1
          FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Identity admin-password LOGIN has unexpected functions: %', actual_functions;
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
           role_oid, 'yoao_identity_admin_passwords_v1', 'USAGE'
       ) OR pg_catalog.has_schema_privilege(
           role_oid, 'yoao_identity_admin_passwords_v1', 'CREATE'
       ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
       OR pg_catalog.has_database_privilege(
           role_oid, current_database(), 'CREATE,TEMP'
       ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
                  'pg_catalog', 'information_schema',
                  'yoao_identity_admin_passwords_v1', 'public'
              )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(role_oid, relation.oid, 'SELECT')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'INSERT')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'UPDATE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'DELETE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'TRUNCATE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'REFERENCES')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'TRIGGER')
               OR pg_catalog.has_any_column_privilege(
                   role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
               ))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS sequence ON sequence.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND sequence.relkind = 'S'
          AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
    ) OR pg_catalog.has_database_privilege(
        role_oid, current_database(), 'CREATE,TEMP'
    ) THEN
        RAISE EXCEPTION 'Identity admin-password LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_auth}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.identity_auth_role', :'identity_auth_role', true);
SELECT set_config('yoao.identity_command_role', :'identity_command_role', true);
SELECT set_config('yoao.identity_fence_role', :'identity_fence_role', true);
SELECT set_config('yoao.identity_operator_role', :'identity_operator_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.identity_auth_role'),
        current_setting('yoao.identity_command_role'),
        current_setting('yoao.identity_fence_role'),
        current_setting('yoao.identity_operator_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT pg_catalog.count(DISTINCT name)
        FROM pg_catalog.unnest(role_names) AS expanded(name)) <> 5
       OR current_setting('yoao.migration_role') = ANY(role_names) THEN
        RAISE EXCEPTION 'Migration, Identity owner and client roles must be distinct';
    END IF;

    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe Identity role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing Identity role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'Unsafe Identity role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Identity roles must have no role memberships: %', role_name;
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
          AND (role.rolcanlogin OR role.rolinherit)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = ANY(ARRAY[
            current_setting('yoao.identity_auth_role'),
            current_setting('yoao.identity_command_role'),
            current_setting('yoao.identity_fence_role'),
            current_setting('yoao.identity_operator_role')
        ]) AND (NOT role.rolcanlogin OR role.rolinherit)
    ) THEN
        RAISE EXCEPTION 'Identity owner must be NOLOGIN and clients LOGIN NOINHERIT';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_auth_role", :"identity_command_role",
         :"identity_fence_role", :"identity_operator_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_auth_role", :"identity_command_role",
       :"identity_fence_role", :"identity_operator_role";
DO $$
DECLARE
    namespace record;
    role_name text;
    client_roles text[] := ARRAY[
        current_setting('yoao.identity_auth_role'),
        current_setting('yoao.identity_command_role'),
        current_setting('yoao.identity_fence_role'),
        current_setting('yoao.identity_operator_role')
    ];
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        FOREACH role_name IN ARRAY client_roles LOOP
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
            );
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name
            );
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name
            );
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name
            );
        END LOOP;
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_v1 TO
    :"identity_auth_role", :"identity_command_role",
    :"identity_fence_role", :"identity_operator_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_v1.authentication_policy(),
    yoao_identity_v1.read_actor_context(uuid, text),
    yoao_identity_v1.read_jwt_principal(uuid, uuid, bigint, timestamptz, boolean),
    yoao_identity_v1.read_api_key_candidates(text, uuid, integer),
    yoao_identity_v1.confirm_api_key_principal(uuid, text)
    TO :"identity_auth_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_v1.get_credential_generation(uuid),
    yoao_identity_v1.advance_credential_generation(uuid),
    yoao_identity_v1.revoke_jti(uuid, timestamptz),
    yoao_identity_v1.is_jti_revoked(uuid),
    yoao_identity_v1.retire_account_credentials(uuid)
    TO :"identity_command_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_v1.lock_current_principal(uuid, text, uuid, bigint, timestamptz, text[])
    TO :"identity_fence_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_v1.purge_expired_revocations(integer)
    TO :"identity_operator_role";

DO $$
DECLARE
    role_entry record;
    role_oid oid;
    callable_count integer;
BEGIN
    FOR role_entry IN
        SELECT role_name, allowed_functions
        FROM (VALUES
            (
                current_setting('yoao.identity_auth_role'),
                ARRAY[
                    pg_catalog.to_regprocedure('yoao_identity_v1.authentication_policy()')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_v1.read_actor_context(uuid,text)')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_v1.read_jwt_principal(uuid,uuid,bigint,timestamptz,boolean)')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_v1.read_api_key_candidates(text,uuid,integer)')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_v1.confirm_api_key_principal(uuid,text)')::oid
                ]::oid[]
            ),
            (
                current_setting('yoao.identity_command_role'),
                ARRAY[
                    pg_catalog.to_regprocedure('yoao_identity_v1.get_credential_generation(uuid)')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_v1.advance_credential_generation(uuid)')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_v1.revoke_jti(uuid,timestamptz)')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_v1.is_jti_revoked(uuid)')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_v1.retire_account_credentials(uuid)')::oid
                ]::oid[]
            ),
            (
                current_setting('yoao.identity_fence_role'),
                ARRAY[
                    pg_catalog.to_regprocedure('yoao_identity_v1.lock_current_principal(uuid,text,uuid,bigint,timestamptz,text[])')::oid
                ]::oid[]
            ),
            (
                current_setting('yoao.identity_operator_role'),
                ARRAY[
                    pg_catalog.to_regprocedure('yoao_identity_v1.purge_expired_revocations(integer)')::oid
                ]::oid[]
            )
        ) AS expected(role_name, allowed_functions)
    LOOP
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_entry.role_name;

        IF pg_catalog.array_position(role_entry.allowed_functions, NULL::oid) IS NOT NULL THEN
            RAISE EXCEPTION 'Identity protocol function is missing for role %', role_entry.role_name;
        END IF;
        IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE')
           OR pg_catalog.has_database_privilege(role_oid, current_database(), 'TEMP') THEN
            RAISE EXCEPTION 'Identity LOGIN has database CREATE or TEMP privilege: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS ns
            WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
              AND ns.nspname <> 'yoao_identity_v1'
              AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'USAGE')
        ) THEN
            RAISE EXCEPTION 'Identity LOGIN has access to a foreign module schema: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS ns
            WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
              AND ns.nspname NOT LIKE 'pg_toast%'
              AND ns.nspname NOT LIKE 'pg_temp_%'
              AND pg_catalog.has_schema_privilege(role_oid, ns.oid, 'CREATE')
        ) THEN
            RAISE EXCEPTION 'Identity LOGIN can create schema objects: %', role_entry.role_name;
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
        ) OR EXISTS (
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
              AND pg_catalog.has_any_column_privilege(role_oid, relation.oid, privilege.name)
        ) THEN
            RAISE EXCEPTION 'Identity LOGIN has direct table privileges: %', role_entry.role_name;
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
            RAISE EXCEPTION 'Identity LOGIN has direct sequence privileges: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS proc
            JOIN pg_catalog.pg_namespace AS ns ON ns.oid = proc.pronamespace
            WHERE ns.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
              AND (ns.nspname <> 'yoao_identity_v1'
                   OR proc.oid <> ALL (role_entry.allowed_functions))
              AND proc.prokind IN ('f', 'p')
              AND pg_catalog.has_function_privilege(role_oid, proc.oid, 'EXECUTE')
        ) THEN
            RAISE EXCEPTION 'Identity LOGIN has an unapproved module function grant: %', role_entry.role_name;
        END IF;
        SELECT pg_catalog.count(*) INTO callable_count
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid = proc.pronamespace
        WHERE ns.nspname = 'yoao_identity_v1'
          AND proc.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(role_oid, proc.oid, 'EXECUTE');
        IF callable_count <> pg_catalog.cardinality(role_entry.allowed_functions) THEN
            RAISE EXCEPTION 'Identity LOGIN has the wrong function count: %', role_entry.role_name;
        END IF;
    END LOOP;
END $$;
\endif

\if :{?grant_briefs}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.identity_briefs_role', :'identity_briefs_role', true);

DO $$
DECLARE
    role_name text := current_setting('yoao.identity_briefs_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.identity_briefs_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_identity_briefs_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM unnest(role_names) AS candidate(name)) <> 3 THEN
        RAISE EXCEPTION 'Unexpected Identity briefs role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Identity briefs roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    briefs_role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_briefs_role')
    );
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR briefs_role_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Identity briefs code owner or client';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_briefs_v1.briefs_policy()'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_briefs_v1.execute_briefs_command(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.identity_brief_project_roles(uuid,uuid[])'
           )::oid) IS DISTINCT FROM workspace_oid THEN
        RAISE EXCEPTION 'An Identity briefs capability has the wrong owner';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
        identity_oid, 'yoao_workspace_v1', 'USAGE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.identity_brief_project_roles(uuid,uuid[])'
        )::oid, 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Identity owner cannot call the Workspace briefs capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_briefs_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_briefs_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.identity_briefs_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_briefs_v1
    TO :"identity_briefs_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_briefs_v1.briefs_policy(),
    yoao_identity_briefs_v1.execute_briefs_command(jsonb)
    TO :"identity_briefs_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_briefs_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_identity_briefs_v1.briefs_policy()',
        'yoao_identity_briefs_v1.execute_briefs_command(jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname,
                 pg_catalog.oidvectortypes(function.proargtypes)
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    JOIN pg_catalog.pg_type AS return_type
      ON return_type.oid = function.prorettype
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND NOT (
          return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND return_type.typname IN ('trigger', 'event_trigger')
      )
      AND NOT EXISTS (
          SELECT 1
          FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Identity briefs LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema', 'yoao_identity_briefs_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
      OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation
          ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(
                   role_oid, relation.oid,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               ) OR pg_catalog.has_any_column_privilege(
                   role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
               ))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS sequence
          ON sequence.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND sequence.relkind = 'S'
          AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
    ) OR pg_catalog.has_database_privilege(
        role_oid, current_database(), 'CREATE,TEMP'
    ) THEN
        RAISE EXCEPTION 'Identity briefs LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_deactivation}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config('yoao.notifications_owner', :'notifications_owner', true);
SELECT set_config('yoao.identity_deactivation_role', :'identity_deactivation_role', true);
SELECT set_config(
    'yoao.identity_deactivation_operator_role', :'identity_deactivation_operator_role', true
);
SELECT set_config('yoao.notification_publish_role', :'notification_publish_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    identity_oid oid;
    audit_oid oid;
    notifications_oid oid;
    migration_oid oid;
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.audit_owner'),
        current_setting('yoao.notifications_owner'),
        current_setting('yoao.identity_deactivation_role'),
        current_setting('yoao.identity_deactivation_operator_role'),
        current_setting('yoao.notification_publish_role')
    ];
    expected_database text := current_setting('yoao.expected_database');
BEGIN
    IF current_database() <> expected_database
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF (SELECT pg_catalog.count(DISTINCT name)
        FROM pg_catalog.unnest(role_names) AS expanded(name)) <> 6
       OR current_setting('yoao.migration_role') = ANY(role_names) THEN
        RAISE EXCEPTION 'Migration, owners and deactivation clients must be distinct';
    END IF;
    IF current_setting('yoao.identity_deactivation_role') <> 'yoao_identity_deactivation_client'
       OR current_setting('yoao.identity_deactivation_operator_role') <> 'yoao_identity_deactivation_operator'
       OR current_setting('yoao.notification_publish_role') <> 'yoao_notification_publish_client' THEN
        RAISE EXCEPTION 'Unexpected deactivation client role name';
    END IF;

    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe deactivation role name';
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role WHERE role.rolname = role_name;
        IF NOT FOUND OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Unsafe or missing deactivation role';
        END IF;
    END LOOP;

    SELECT role.oid INTO identity_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.identity_owner');
    SELECT role.oid INTO audit_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.audit_owner');
    SELECT role.oid INTO notifications_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.notifications_owner');
    SELECT role.oid INTO migration_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.migration_role');
    SELECT role.oid INTO role_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.identity_deactivation_role');
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.oid IN (identity_oid, audit_oid, notifications_oid)
          AND (role.rolcanlogin OR role.rolinherit)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname IN (
            current_setting('yoao.identity_deactivation_role'),
            current_setting('yoao.identity_deactivation_operator_role'),
            current_setting('yoao.notification_publish_role')
        ) AND (NOT role.rolcanlogin OR role.rolinherit)
    ) THEN
        RAISE EXCEPTION 'Owners must be NOLOGIN and deactivation clients LOGIN NOINHERIT';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'yoao_identity_deactivation_v1'
          AND namespace.nspowner = migration_oid
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'yoao_identity_deactivation_v1'
          AND function.prokind = 'f' AND function.proowner <> identity_oid
    ) THEN
        RAISE EXCEPTION 'Identity deactivation schema or function owner is unexpected';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_v1.lock_super_admin_invariant()'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_v1.super_admin_status(uuid)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_audit_v1.record_identity_deactivation_event(text,jsonb,jsonb,jsonb)'
           )::oid) IS DISTINCT FROM audit_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_notifications_v1.create_identity_deactivation_notifications(uuid,text,jsonb,uuid[])'
           )::oid) IS DISTINCT FROM notifications_oid THEN
        RAISE EXCEPTION 'A shared deactivation capability has the wrong owner';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS function
        WHERE function.oid IN (
            pg_catalog.to_regprocedure('yoao_notifications_v1.identity_delivery_policy()')::oid,
            pg_catalog.to_regprocedure('yoao_notifications_v1.claim_identity_deliveries(jsonb)')::oid,
            pg_catalog.to_regprocedure('yoao_notifications_v1.finish_identity_delivery(jsonb)')::oid
        ) AND function.proowner <> notifications_oid
    ) THEN
        RAISE EXCEPTION 'Notifications publisher functions have the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid,
        pg_catalog.to_regprocedure(
            'yoao_audit_v1.record_identity_deactivation_event(text,jsonb,jsonb,jsonb)'
        )::oid,
        'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,
        pg_catalog.to_regprocedure(
            'yoao_notifications_v1.create_identity_deactivation_notifications(uuid,text,jsonb,uuid[])'
        )::oid,
        'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,
        pg_catalog.to_regprocedure('yoao_identity_v1.is_jti_revoked(uuid)')::oid,
        'EXECUTE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'deactivation_requested_at', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_reason', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'is_active', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_account_presence', 'status', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_at', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_by', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'deactivation_reason', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'deactivation_scheduled_at', 'UPDATE'
    ) OR pg_catalog.has_table_privilege(
        identity_oid, 'public.notifications', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
    ) OR pg_catalog.has_table_privilege(
        identity_oid, 'public.notification_preferences', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        audit_oid, 'public.audit_logs', 'actor_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        audit_oid, 'public.audit_logs', 'actor_email', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        audit_oid, 'public.audit_logs', 'actor_role', 'UPDATE'
    ) THEN
        RAISE EXCEPTION 'Identity owner is missing deactivation capabilities';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_deactivation_role", :"identity_deactivation_operator_role",
         :"notification_publish_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_deactivation_role", :"identity_deactivation_operator_role",
       :"notification_publish_role";
DO $$
DECLARE
    namespace record;
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        current_setting('yoao.identity_deactivation_role'),
        current_setting('yoao.identity_deactivation_operator_role'),
        current_setting('yoao.notification_publish_role')
    ] LOOP
        FOR namespace IN
            SELECT ns.nspname
            FROM pg_catalog.pg_namespace AS ns
            WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
              AND ns.nspname NOT LIKE 'pg_toast%'
              AND ns.nspname NOT LIKE 'pg_temp_%'
        LOOP
            EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        END LOOP;
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_deactivation_v1
    TO :"identity_deactivation_role", :"identity_deactivation_operator_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_deactivation_v1.deactivation_policy(),
    yoao_identity_deactivation_v1.execute_deactivation_command(jsonb)
    TO :"identity_deactivation_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_deactivation_v1.due_policy(),
    yoao_identity_deactivation_v1.process_due_deactivations(jsonb)
    TO :"identity_deactivation_operator_role";
GRANT USAGE ON SCHEMA yoao_notifications_v1 TO :"notification_publish_role";
GRANT EXECUTE ON FUNCTION
    yoao_notifications_v1.identity_delivery_policy(),
    yoao_notifications_v1.claim_identity_deliveries(jsonb),
    yoao_notifications_v1.finish_identity_delivery(jsonb)
    TO :"notification_publish_role";

DO $$
DECLARE
    role_name text;
    role_oid oid;
    allowed_schema text;
    allowed_functions text[];
    actual_functions text[];
    expected_functions text[];
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        current_setting('yoao.identity_deactivation_role'),
        current_setting('yoao.identity_deactivation_operator_role'),
        current_setting('yoao.notification_publish_role')
    ] LOOP
        IF role_name = current_setting('yoao.identity_deactivation_role') THEN
            allowed_schema := 'yoao_identity_deactivation_v1';
            allowed_functions := ARRAY[
                'yoao_identity_deactivation_v1.deactivation_policy()',
                'yoao_identity_deactivation_v1.execute_deactivation_command(jsonb)'
            ];
        ELSIF role_name = current_setting('yoao.identity_deactivation_operator_role') THEN
            allowed_schema := 'yoao_identity_deactivation_v1';
            allowed_functions := ARRAY[
                'yoao_identity_deactivation_v1.due_policy()',
                'yoao_identity_deactivation_v1.process_due_deactivations(jsonb)'
            ];
        ELSE
            allowed_schema := 'yoao_notifications_v1';
            allowed_functions := ARRAY[
                'yoao_notifications_v1.identity_delivery_policy()',
                'yoao_notifications_v1.claim_identity_deliveries(jsonb)',
                'yoao_notifications_v1.finish_identity_delivery(jsonb)'
            ];
        END IF;
        SELECT role.oid INTO role_oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        SELECT COALESCE(pg_catalog.array_agg(
            namespace.nspname || '.' || function.proname || '('
            || pg_catalog.oidvectortypes(function.proargtypes) || ')'
            ORDER BY namespace.nspname || '.' || function.proname || '('
            || pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ), ARRAY[]::text[]) INTO actual_functions
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE (left(namespace.nspname, 4) = 'aap_'
               OR left(namespace.nspname, 5) = 'yoao_')
          AND function.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
        SELECT COALESCE(pg_catalog.array_agg(item.signature ORDER BY item.signature), ARRAY[]::text[])
        INTO expected_functions
        FROM pg_catalog.unnest(allowed_functions) AS item(signature);
        IF actual_functions IS DISTINCT FROM expected_functions THEN
            RAISE EXCEPTION 'Unexpected function allowlist for %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
              AND namespace.nspname <> allowed_schema
              AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
        ) THEN
            RAISE EXCEPTION 'Unexpected foreign module schema for %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_proc AS function ON function.pronamespace = namespace.oid
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND left(namespace.nspname, 4) <> 'aap_'
              AND left(namespace.nspname, 5) <> 'yoao_'
              AND function.prokind IN ('f', 'p')
              AND function.prosecdef
              AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE')
        ) THEN
            RAISE EXCEPTION 'Unexpected non-module SECURITY DEFINER capability for %', role_name;
        END IF;
        IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE,TEMP')
           OR EXISTS (
               SELECT 1 FROM pg_catalog.pg_namespace AS namespace
               WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
                 AND namespace.nspname NOT LIKE 'pg_toast%'
                 AND namespace.nspname NOT LIKE 'pg_temp_%'
                 AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE')
           ) OR EXISTS (
               SELECT 1
               FROM pg_catalog.pg_namespace AS namespace
               JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
               WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
                 AND namespace.nspname NOT LIKE 'pg_toast%'
                 AND namespace.nspname NOT LIKE 'pg_temp_%'
                 AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
                 AND (pg_catalog.has_table_privilege(role_oid, relation.oid,
                          'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                   OR pg_catalog.has_any_column_privilege(role_oid, relation.oid,
                          'SELECT,INSERT,UPDATE,REFERENCES'))
           ) THEN
            RAISE EXCEPTION 'Unexpected direct data/schema access for %', role_name;
        END IF;
    END LOOP;
END $$;
\endif

\if :{?grant_deletions}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config(
    'yoao.identity_deletion_role', :'identity_deletion_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.identity_deletion_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.labeling_owner'),
        current_setting('yoao.audit_owner'),
        current_setting('yoao.identity_deletion_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_identity_deletion_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM unnest(role_names) AS candidate(name)) <> 5 THEN
        RAISE EXCEPTION 'Unexpected Identity deletion role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Identity deletion roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    labeling_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.labeling_owner')
    );
    audit_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.audit_owner')
    );
    deletion_role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_deletion_role')
    );
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR labeling_oid IS NULL
       OR audit_oid IS NULL OR deletion_role_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Identity deletion code owner or client';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_deletions_v1.deletion_policy()'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_deletions_v1.execute_deletion_command(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.lock_identity_deletion_scope(uuid,uuid)'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.identity_deletion_transfer_covered(uuid,uuid[])'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.identity_deletion_work(uuid)'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.transfer_identity_deletion_work(uuid,uuid)'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_audit_v1.record_identity_deletion_event(jsonb,uuid,jsonb,jsonb)'
           )::oid) IS DISTINCT FROM audit_oid THEN
        RAISE EXCEPTION 'An Identity deletion capability has the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.lock_identity_deletion_scope(uuid,uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.identity_deletion_transfer_covered(uuid,uuid[])'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_labeling_v1.identity_deletion_work(uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_labeling_v1.transfer_identity_deletion_work(uuid,uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_audit_v1.record_identity_deletion_event(jsonb,uuid,jsonb,jsonb)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'operation_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'actor_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'operation', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'target_user_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'transfer_to_user_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'request_fingerprint', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'derivation_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'target_generation', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'target_is_active', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'result', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'result', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'is_active', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_kind', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_at', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_by', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_reason', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        audit_oid, 'public.audit_logs', 'detail_json', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.tasks', 'assignee_id', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.tasks', 'assigned_at', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.tasks', 'updated_at', 'UPDATE'
    ) THEN
        RAISE EXCEPTION 'Identity deletion owners are missing a receipt, Workspace, Labeling or Audit capability';
    END IF;
    IF pg_catalog.has_table_privilege(
           identity_oid, 'public.projects', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.project_members', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.tasks', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.task_batches', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.projects', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.project_members', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.tasks', 'SELECT,INSERT,UPDATE,REFERENCES'
    ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.task_batches', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.task_locks', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.task_locks', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_table_privilege(
           deletion_role_oid, 'public.identity_deletion_operation_receipts',
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           deletion_role_oid, 'public.users', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) THEN
        RAISE EXCEPTION 'Identity deletion owner/client has raw table access';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_deletion_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_deletion_role";
DO $$
DECLARE
    namespace record;
    column_grant record;
    role_name text := current_setting('yoao.identity_deletion_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
    END LOOP;
    FOR column_grant IN
        SELECT grant_row.table_schema, grant_row.table_name,
               grant_row.column_name, grant_row.privilege_type
        FROM information_schema.column_privileges AS grant_row
        WHERE grant_row.grantee = role_name
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE %s (%I) ON TABLE %I.%I FROM %I',
            column_grant.privilege_type,
            column_grant.column_name,
            column_grant.table_schema,
            column_grant.table_name,
            role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_deletions_v1
    TO :"identity_deletion_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_deletions_v1.deletion_policy(),
    yoao_identity_deletions_v1.execute_deletion_command(jsonb)
    TO :"identity_deletion_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_deletion_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_identity_deletions_v1.deletion_policy()',
        'yoao_identity_deletions_v1.execute_deletion_command(jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND function.prorettype NOT IN (
          'trigger'::pg_catalog.regtype, 'event_trigger'::pg_catalog.regtype
      )
      AND NOT EXISTS (
          SELECT 1
          FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Identity deletion LOGIN has unexpected functions: %', actual_functions;
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
           role_oid, 'yoao_identity_deletions_v1', 'USAGE'
       ) OR pg_catalog.has_schema_privilege(
           role_oid, 'yoao_identity_deletions_v1', 'CREATE'
       ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
       OR pg_catalog.has_database_privilege(
           role_oid, current_database(), 'CREATE,TEMP'
       ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
                  'pg_catalog', 'information_schema',
                  'yoao_identity_deletions_v1', 'public'
              )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(role_oid, relation.oid, 'SELECT')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'INSERT')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'UPDATE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'DELETE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'TRUNCATE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'REFERENCES')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'TRIGGER')
               OR pg_catalog.has_any_column_privilege(
                   role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
               ))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS sequence ON sequence.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND sequence.relkind = 'S'
          AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
    ) THEN
        RAISE EXCEPTION 'Identity deletion LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_directory}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config('yoao.identity_directory_role', :'identity_directory_role', true);

DO $$
DECLARE
    role_name text := current_setting('yoao.identity_directory_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.labeling_owner'),
        current_setting('yoao.audit_owner'),
        current_setting('yoao.identity_directory_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_identity_directory_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM unnest(role_names) AS candidate(name)) <> 5 THEN
        RAISE EXCEPTION 'Unexpected Identity directory role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Identity directory roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    labeling_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.labeling_owner')
    );
    audit_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.audit_owner')
    );
    directory_role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_directory_role')
    );
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR labeling_oid IS NULL
       OR audit_oid IS NULL OR directory_role_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Identity directory code owner or client';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_directory_v1.directory_policy()'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_directory_v1.execute_directory_command(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.identity_directory_owned_member_ids(uuid)'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.identity_directory_project_member_ids(uuid)'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.identity_directory_lifecycle_blocked(uuid,uuid[])'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.identity_directory_project_refs(uuid[])'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_audit_v1.record_identity_directory_export(jsonb,jsonb,jsonb)'
           )::oid) IS DISTINCT FROM audit_oid THEN
        RAISE EXCEPTION 'An Identity directory cross-owner function has the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.identity_directory_owned_member_ids(uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.identity_directory_project_member_ids(uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.identity_directory_lifecycle_blocked(uuid,uuid[])'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid, pg_catalog.to_regprocedure(
            'yoao_labeling_v1.identity_directory_project_refs(uuid[])'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_audit_v1.record_identity_directory_export(jsonb,jsonb,jsonb)'
        )::oid, 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Identity directory owner is missing a projection or Audit capability';
    END IF;
    IF pg_catalog.has_table_privilege(
           identity_oid, 'public.projects', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.project_members', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.task_batches', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.tasks', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.task_locks', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) THEN
        RAISE EXCEPTION 'Identity directory owner has raw foreign-table access';
    END IF;
    IF NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_directory_export_receipts',
        'operation_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_directory_export_receipts',
        'result', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        audit_oid, 'public.audit_logs', 'detail_json', 'INSERT'
    ) THEN
        RAISE EXCEPTION 'Identity directory receipt or Audit projection is not granted';
    END IF;
    IF pg_catalog.has_table_privilege(
        directory_role_oid, 'public.identity_directory_export_receipts',
        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
    ) OR pg_catalog.has_table_privilege(
        directory_role_oid, 'public.users', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
    ) THEN
        RAISE EXCEPTION 'Identity directory LOGIN can access raw tables';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_directory_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_directory_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.identity_directory_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_directory_v1 TO :"identity_directory_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_directory_v1.directory_policy(),
    yoao_identity_directory_v1.execute_directory_command(jsonb)
    TO :"identity_directory_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_directory_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_identity_directory_v1.directory_policy()',
        'yoao_identity_directory_v1.execute_directory_command(jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    WHERE (left(namespace.nspname, 4) = 'aap_'
           OR left(namespace.nspname, 5) = 'yoao_')
      AND function.prokind IN ('f', 'p')
      AND function.prorettype NOT IN (
          'trigger'::pg_catalog.regtype, 'event_trigger'::pg_catalog.regtype
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Identity directory LOGIN has unexpected functions: %', actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE (left(namespace.nspname, 4) = 'aap_'
               OR left(namespace.nspname, 5) = 'yoao_')
          AND namespace.nspname <> 'yoao_identity_directory_v1'
          AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(
                   role_oid, relation.oid,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               ) OR pg_catalog.has_any_column_privilege(
                   role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
               ))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS sequence ON sequence.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND sequence.relkind = 'S'
          AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
    ) OR pg_catalog.has_database_privilege(
        role_oid, current_database(), 'CREATE,TEMP'
    ) THEN
        RAISE EXCEPTION 'Identity directory LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_e2e_fixture}
SELECT set_config('yoao.grant_section_applied', '1', true);
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.e2e_fixture_operator', :'consumer_role', true);

DO $profile$
DECLARE
    v_consumer name := current_setting('yoao.e2e_fixture_operator');
    v_identity name := current_setting('yoao.identity_owner');
    v_signature text;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR (right(lower(current_database()),5)<>'_test' AND right(lower(current_database()),4)<>'_e2e')
       OR v_consumer IN (current_user, v_identity, 'identity_api_consumer')
       OR pg_has_role(v_consumer, v_identity, 'MEMBER')
       OR pg_has_role(v_consumer, current_user, 'MEMBER') THEN
        RAISE EXCEPTION 'Invalid Identity E2E fixture operator ownership';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_consumer AND rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT 1 FROM pg_auth_members
        WHERE member=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
           OR (roleid=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
               AND member<>(SELECT oid FROM pg_roles
                            WHERE rolname=current_setting('yoao.migration_role')))
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_identity AND NOT rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'E2E fixture operator requires non-admin, non-inheriting LOGIN';
    END IF;
    IF EXISTS (SELECT 1 FROM unnest(ARRAY['public.users','public.identity_account_presence','public.user_invitations','public.password_reset_tokens','public.organization_members']) private(name) WHERE has_table_privilege(v_consumer,private.name,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR has_any_column_privilege(v_consumer,private.name,'SELECT,INSERT,UPDATE,REFERENCES')) THEN
        RAISE EXCEPTION 'Apply the ordinary Identity consumer fence first';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_roles owner ON owner.oid=c.relowner
        WHERE c.oid='yoao_identity_fixture_v1.account_profiles'::regclass
          AND c.relkind='v' AND owner.rolname=v_identity
          AND COALESCE(c.reloptions @> ARRAY['security_barrier=true'],false)
          AND ARRAY(SELECT a.attname::text FROM pg_attribute a
                    WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
                    ORDER BY a.attnum)=ARRAY['id','email','name','role','group_name','group_id','status','is_active','disabled_kind','disabled_at','disabled_by','disabled_reason','email_verified_at','last_login_at','last_seen_at','password_admin_reset_at','deactivation_requested_at','deactivation_scheduled_at','preferences','avatar_ref','created_at']
    ) THEN
        RAISE EXCEPTION 'Unexpected E2E account read relation';
    END IF;
    EXECUTE format('GRANT USAGE ON SCHEMA yoao_identity_fixture_v1 TO %I',v_consumer);
    EXECUTE format('GRANT SELECT ON yoao_identity_fixture_v1.account_profiles TO %I',v_consumer);
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_identity_fixture_v1.create_account(uuid,text,text,text,text)',
        'yoao_identity_fixture_v1.mark_manual_inactive(uuid,timestamptz,uuid)',
        'yoao_identity_fixture_v1.create_invitations(jsonb)',
        'yoao_identity_fixture_v1.delete_accounts(boolean,text[])',
        'yoao_identity_fixture_v1.delete_reset_tokens(uuid[])',
        'yoao_identity_fixture_v1.delete_invitations(uuid[])',
        'yoao_identity_fixture_v1.delete_organization_members(uuid[])'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_roles owner ON owner.oid=p.proowner
            WHERE p.oid=v_signature::regprocedure AND p.prosecdef AND p.provolatile='v'
              AND owner.rolname=v_identity
        ) THEN
            RAISE EXCEPTION 'Unexpected E2E fixture function owner or security mode';
        END IF;
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I', v_signature, v_consumer);
    END LOOP;
END $profile$;
\endif

\if :{?grant_groups}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config('yoao.identity_group_role', :'identity_group_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    identity_oid oid;
    workspace_oid oid;
    audit_oid oid;
    migration_oid oid;
    workspace_function_oid oid;
    audit_function_oid oid;
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.audit_owner'),
        current_setting('yoao.identity_group_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT pg_catalog.count(DISTINCT name)
        FROM pg_catalog.unnest(role_names) AS expanded(name)) <> 4
       OR current_setting('yoao.migration_role') = ANY(role_names) THEN
        RAISE EXCEPTION 'Migration, owners and group client must be distinct';
    END IF;
    IF current_setting('yoao.identity_group_role') <> 'yoao_identity_group_client' THEN
        RAISE EXCEPTION 'Identity groups must use yoao_identity_group_client';
    END IF;

    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe Identity group role name';
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role WHERE role.rolname = role_name;
        IF NOT FOUND OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Unsafe or missing Identity group role';
        END IF;
    END LOOP;

    SELECT role.oid INTO identity_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.identity_owner');
    SELECT role.oid INTO workspace_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.workspace_owner');
    SELECT role.oid INTO audit_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.audit_owner');
    SELECT role.oid INTO migration_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.migration_role');
    SELECT role.oid INTO role_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.identity_group_role');
    workspace_function_oid := pg_catalog.to_regprocedure(
        'yoao_workspace_v1.lock_group_assignment_scope(uuid,uuid[],boolean)'
    )::oid;
    audit_function_oid := pg_catalog.to_regprocedure(
        'yoao_audit_v1.record_identity_group_event(text,jsonb,jsonb,jsonb)'
    )::oid;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.oid IN (identity_oid, workspace_oid, audit_oid)
          AND (role.rolcanlogin OR role.rolinherit)
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.oid = role_oid AND role.rolcanlogin AND NOT role.rolinherit
    ) THEN
        RAISE EXCEPTION 'Identity owners must be NOLOGIN and group client LOGIN NOINHERIT';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'yoao_identity_groups_v1'
          AND namespace.nspowner = migration_oid
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'yoao_identity_groups_v1'
          AND function.prokind = 'f' AND function.proowner <> identity_oid
    ) THEN
        RAISE EXCEPTION 'Identity group schema or function owner is unexpected';
    END IF;
    IF workspace_function_oid IS NULL OR audit_function_oid IS NULL
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = workspace_function_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = audit_function_oid) IS DISTINCT FROM audit_oid THEN
        RAISE EXCEPTION 'Workspace or Audit group capability has the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid,
        workspace_function_oid,
        'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,
        audit_function_oid,
        'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,
        pg_catalog.to_regprocedure('yoao_audit_v1.valid_request_context(jsonb)')::oid,
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Identity owner is missing Workspace or Audit capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"identity_group_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"identity_group_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.identity_group_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_groups_v1 TO :"identity_group_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_groups_v1.group_policy(),
    yoao_identity_groups_v1.execute_group_command(jsonb)
    TO :"identity_group_role";

DO $$
DECLARE
    role_oid oid;
    allowed_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_identity_groups_v1.group_policy()')::oid,
        pg_catalog.to_regprocedure('yoao_identity_groups_v1.execute_group_command(jsonb)')::oid
    ];
    actual_functions oid[];
BEGIN
    SELECT role.oid INTO role_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.identity_group_role');
    IF pg_catalog.array_position(allowed_functions, NULL::oid) IS NOT NULL THEN
        RAISE EXCEPTION 'Identity group entrypoint is missing';
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(function.oid ORDER BY function.oid), ARRAY[]::oid[])
    INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
    WHERE (left(namespace.nspname, 4) = 'aap_'
           OR left(namespace.nspname, 5) = 'yoao_')
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM (
        SELECT pg_catalog.array_agg(allowed.oid ORDER BY allowed.oid)
        FROM pg_catalog.unnest(allowed_functions) AS allowed(oid)
    ) THEN
        RAISE EXCEPTION 'Identity group LOGIN has an unexpected module function capability';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND namespace.nspname <> 'yoao_identity_groups_v1'
          AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Identity group LOGIN has a foreign module schema';
    END IF;
    IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE,TEMP') THEN
        RAISE EXCEPTION 'Identity group LOGIN has database CREATE or TEMP';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE')
    ) THEN
        RAISE EXCEPTION 'Identity group LOGIN can create schema objects';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(role_oid, relation.oid,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
            OR pg_catalog.has_any_column_privilege(role_oid, relation.oid,
                   'SELECT,INSERT,UPDATE,REFERENCES'))
    ) THEN
        RAISE EXCEPTION 'Identity group LOGIN has direct relation access';
    END IF;
END $$;
\endif

\if :{?grant_invitations}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.settings_owner', :'settings_owner', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config('yoao.identity_invitation_role', :'identity_invitation_role', true);
SELECT set_config('yoao.identity_mail_v2_role', :'identity_mail_v2_role', true);

DO $$
DECLARE
    role_name text;
    role_names text[] := ARRAY[
        current_setting('yoao.identity_invitation_role'),
        current_setting('yoao.identity_mail_v2_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF current_setting('yoao.identity_invitation_role') <> 'yoao_identity_invitation_client'
       OR current_setting('yoao.identity_mail_v2_role') <> 'yoao_identity_mail_v2_client'
       OR (SELECT count(DISTINCT candidate.name) FROM unnest(role_names) AS candidate(name)) <> 2
       OR EXISTS (
           SELECT 1 FROM unnest(role_names) AS candidate(name)
           WHERE candidate.name = ANY(ARRAY[
               current_setting('yoao.identity_owner'), current_setting('yoao.workspace_owner'),
               current_setting('yoao.labeling_owner'), current_setting('yoao.settings_owner'),
               current_setting('yoao.audit_owner'), current_setting('yoao.migration_role')
           ])
       ) THEN
        RAISE EXCEPTION 'Unexpected Identity invitation role names';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name AND role.rolcanlogin
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
        ) OR pg_catalog.pg_has_role(role_name,current_user,'MEMBER')
          OR EXISTS (
              SELECT 1 FROM pg_catalog.pg_auth_members AS membership
              WHERE membership.member = (
                  SELECT oid FROM pg_catalog.pg_roles WHERE rolname = role_name
              ) OR membership.roleid = (
                  SELECT oid FROM pg_catalog.pg_roles WHERE rolname = role_name
              )
          ) THEN
            RAISE EXCEPTION 'Unsafe or inheriting Identity invitation role: %', role_name;
        END IF;
    END LOOP;
END $$;

DO $$
DECLARE
    identity_oid oid := (SELECT oid FROM pg_catalog.pg_roles
                         WHERE rolname = current_setting('yoao.identity_owner'));
    workspace_oid oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname = current_setting('yoao.workspace_owner'));
    labeling_oid oid := (SELECT oid FROM pg_catalog.pg_roles
                         WHERE rolname = current_setting('yoao.labeling_owner'));
    settings_oid oid := (SELECT oid FROM pg_catalog.pg_roles
                         WHERE rolname = current_setting('yoao.settings_owner'));
    audit_oid oid := (SELECT oid FROM pg_catalog.pg_roles
                      WHERE rolname = current_setting('yoao.audit_owner'));
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR labeling_oid IS NULL
       OR settings_oid IS NULL OR audit_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Identity invitation code owner';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_invitations_v1.invitation_policy()'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_invitations_v1.prepare_invitation_command(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_invitations_v1.apply_invitation_command(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_invitations_v1._incompatible_role_detail(text,text)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_groups_v1.resolve_invitation_group(uuid,text)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_audit_v1.record_identity_invitation_event(text,jsonb,jsonb,jsonb,jsonb)'
           )::oid) IS DISTINCT FROM audit_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_settings_v1.invitation_policy(jsonb)'
           )::oid) IS DISTINCT FROM settings_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.invitation_work_summary(uuid,uuid,text)'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.grant_invitation_membership(uuid,uuid,text,uuid,uuid)'
           )::oid) IS DISTINCT FROM workspace_oid THEN
        RAISE EXCEPTION 'An invitation cross-owner capability has the wrong owner';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        WHERE function.oid IN (
            pg_catalog.to_regprocedure('yoao_identity_mail_v2.mail_policy()')::oid,
            pg_catalog.to_regprocedure('yoao_identity_mail_v2.claim_mail(jsonb)')::oid,
            pg_catalog.to_regprocedure('yoao_identity_mail_v2.renew_mail(jsonb)')::oid,
            pg_catalog.to_regprocedure('yoao_identity_mail_v2.finish_mail(jsonb)')::oid
        ) AND function.proowner <> identity_oid
    ) THEN
        RAISE EXCEPTION 'Mail v2 functions have the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure(
            'yoao_audit_v1.record_identity_invitation_event(text,jsonb,jsonb,jsonb,jsonb)'
        )::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('yoao_settings_v1.invitation_policy(jsonb)')::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('yoao_labeling_v1.invitation_work_summary(uuid,uuid,text)')::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('yoao_workspace_v1.invitation_projects(uuid[])')::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('yoao_workspace_v1.lock_invitation_project(uuid,uuid)')::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('yoao_workspace_v1.read_invitation_membership(uuid,uuid)')::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('yoao_workspace_v1.grant_invitation_membership(uuid,uuid,text,uuid,uuid)')::oid,'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,pg_catalog.to_regprocedure('yoao_identity_groups_v1.resolve_invitation_group(uuid,text)')::oid,'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Identity owner is missing an invitation owner capability';
    END IF;
    IF pg_catalog.has_table_privilege(identity_oid,'public.projects','SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
       OR pg_catalog.has_table_privilege(identity_oid,'public.project_members','SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
       OR pg_catalog.has_table_privilege(identity_oid,'public.task_batches','SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
       OR pg_catalog.has_table_privilege(identity_oid,'public.system_settings','SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
       OR pg_catalog.has_table_privilege(identity_oid,'public.audit_logs','SELECT,INSERT,UPDATE,DELETE,TRUNCATE') THEN
        RAISE EXCEPTION 'Identity invitation owner has raw foreign-table access';
    END IF;
    IF NOT pg_catalog.has_column_privilege(
        identity_oid,'public.identity_invitation_operation_receipts','operation_id','SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid,'public.identity_invitation_operation_receipts','operation_id','INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid,'public.user_invitations','token','SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid,'public.user_invitations','token','UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid,'public.project_members','role','INSERT'
    ) THEN
        RAISE EXCEPTION 'Missing Identity invitation private state or Workspace membership capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_invitation_role", :"identity_mail_v2_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_invitation_role", :"identity_mail_v2_role";
DO $$
DECLARE
    namespace record;
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        current_setting('yoao.identity_invitation_role'),
        current_setting('yoao.identity_mail_v2_role')
    ] LOOP
        FOR namespace IN
            SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
            WHERE ns.nspname NOT IN ('pg_catalog','information_schema')
              AND ns.nspname NOT LIKE 'pg_toast%'
              AND ns.nspname NOT LIKE 'pg_temp_%'
        LOOP
            EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I',namespace.nspname,role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',namespace.nspname,role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',namespace.nspname,role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',namespace.nspname,role_name);
        END LOOP;
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_invitations_v1 TO :"identity_invitation_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_invitations_v1.invitation_policy(),
    yoao_identity_invitations_v1.prepare_invitation_command(jsonb),
    yoao_identity_invitations_v1.apply_invitation_command(jsonb)
    TO :"identity_invitation_role";
GRANT USAGE ON SCHEMA yoao_identity_mail_v2 TO :"identity_mail_v2_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_mail_v2.mail_policy(),
    yoao_identity_mail_v2.claim_mail(jsonb),
    yoao_identity_mail_v2.renew_mail(jsonb),
    yoao_identity_mail_v2.finish_mail(jsonb)
    TO :"identity_mail_v2_role";

DO $$
DECLARE
    role_name text;
    role_oid oid;
    allowed_schema text;
    actual_functions text[];
    expected_functions text[];
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        current_setting('yoao.identity_invitation_role'),
        current_setting('yoao.identity_mail_v2_role')
    ] LOOP
        role_oid := (SELECT role.oid FROM pg_catalog.pg_roles AS role WHERE role.rolname=role_name);
        IF role_name = current_setting('yoao.identity_invitation_role') THEN
            allowed_schema := 'yoao_identity_invitations_v1';
            expected_functions := ARRAY[
                'yoao_identity_invitations_v1.apply_invitation_command(jsonb)',
                'yoao_identity_invitations_v1.invitation_policy()',
                'yoao_identity_invitations_v1.prepare_invitation_command(jsonb)'
            ];
        ELSE
            allowed_schema := 'yoao_identity_mail_v2';
            expected_functions := ARRAY[
                'yoao_identity_mail_v2.claim_mail(jsonb)',
                'yoao_identity_mail_v2.finish_mail(jsonb)',
                'yoao_identity_mail_v2.mail_policy()',
                'yoao_identity_mail_v2.renew_mail(jsonb)'
            ];
        END IF;
        SELECT COALESCE(pg_catalog.array_agg(
            namespace.nspname || '.' || function.proname || '('
            || pg_catalog.oidvectortypes(function.proargtypes) || ')'
            ORDER BY namespace.nspname, function.proname
        ),ARRAY[]::text[]) INTO actual_functions
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace
        WHERE (left(namespace.nspname,4)='aap_' OR left(namespace.nspname,5)='yoao_')
          AND function.prokind IN ('f','p')
          AND pg_catalog.has_function_privilege(role_oid,function.oid,'EXECUTE');
        IF actual_functions IS DISTINCT FROM expected_functions THEN
            RAISE EXCEPTION 'Identity invitation role has unexpected function surface: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            WHERE (left(namespace.nspname,4) = 'aap_'
                   OR left(namespace.nspname,5) = 'yoao_')
              AND namespace.nspname <> allowed_schema
              AND pg_catalog.has_schema_privilege(role_oid,namespace.oid,'USAGE')
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace=namespace.oid
            WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r','p','v','m','f')
              AND (pg_catalog.has_table_privilege(role_oid,relation.oid,
                       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR pg_catalog.has_any_column_privilege(role_oid,relation.oid,
                       'SELECT,INSERT,UPDATE,REFERENCES'))
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_proc AS function ON function.pronamespace=namespace.oid
            WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND left(namespace.nspname,4) <> 'aap_'
              AND left(namespace.nspname,5) <> 'yoao_'
              AND function.prokind IN ('f','p') AND function.prosecdef
              AND pg_catalog.has_function_privilege(role_oid,function.oid,'EXECUTE')
        ) OR pg_catalog.has_database_privilege(role_oid,current_database(),'CREATE,TEMP') THEN
            RAISE EXCEPTION 'Identity invitation role has non-function capabilities: %', role_name;
        END IF;
    END LOOP;
END $$;
\endif

\if :{?grant_keys}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.identity_key_role', :'identity_key_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    owner_oid oid;
    migration_oid oid;
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.identity_key_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT pg_catalog.count(DISTINCT name)
        FROM pg_catalog.unnest(role_names) AS expanded(name)) <> 2
       OR current_setting('yoao.migration_role') = ANY(role_names) THEN
        RAISE EXCEPTION 'Migration, Identity owner and API-key client must be distinct';
    END IF;
    IF current_setting('yoao.identity_key_role') <> 'yoao_identity_key_client' THEN
        RAISE EXCEPTION 'API-key management must use the yoao_identity_key_client LOGIN';
    END IF;

    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe Identity role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing Identity role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'Unsafe Identity role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Identity roles must have no role memberships: %', role_name;
        END IF;
    END LOOP;

    SELECT role.oid INTO owner_oid
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.identity_owner');
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.oid = owner_oid AND (role.rolcanlogin OR role.rolinherit)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_key_role')
          AND (NOT role.rolcanlogin OR role.rolinherit)
    ) THEN
        RAISE EXCEPTION 'Identity owner must be NOLOGIN and API-key client LOGIN NOINHERIT';
    END IF;
    SELECT role.oid INTO migration_oid
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.migration_role');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Missing migration role';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'yoao_identity_keys_v1'
          AND namespace.nspowner = migration_oid
    ) THEN
        RAISE EXCEPTION 'API-key schema must remain owned by the migration role';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'yoao_identity_keys_v1'
          AND function.prokind = 'f' AND function.proowner <> owner_oid
    ) THEN
        RAISE EXCEPTION 'API-key functions must be owned by the Identity code owner';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"identity_key_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"identity_key_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.identity_key_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_keys_v1 TO :"identity_key_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_keys_v1.key_policy(),
    yoao_identity_keys_v1.execute_key_command(jsonb)
    TO :"identity_key_role";

DO $$
DECLARE
    role_name text := current_setting('yoao.identity_key_role');
    role_oid oid;
    allowed_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_identity_keys_v1.key_policy()')::oid,
        pg_catalog.to_regprocedure('yoao_identity_keys_v1.execute_key_command(jsonb)')::oid
    ];
    callable_count integer;
BEGIN
    SELECT role.oid INTO role_oid
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = role_name;
    IF pg_catalog.array_position(allowed_functions, NULL::oid) IS NOT NULL THEN
        RAISE EXCEPTION 'Identity API-key protocol function is missing';
    END IF;
    IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(role_oid, current_database(), 'TEMP') THEN
        RAISE EXCEPTION 'Identity API-key LOGIN has database CREATE or TEMP privilege';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND namespace.nspname <> 'yoao_identity_keys_v1'
          AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Identity API-key LOGIN has access to a foreign module schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE')
    ) THEN
        RAISE EXCEPTION 'Identity API-key LOGIN can create schema objects';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        CROSS JOIN (VALUES
            ('SELECT'::text), ('INSERT'), ('UPDATE'), ('DELETE'),
            ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
        ) AS privilege(name)
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND pg_catalog.has_table_privilege(role_oid, relation.oid, privilege.name)
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        CROSS JOIN (VALUES ('SELECT'::text), ('INSERT'), ('UPDATE'), ('REFERENCES')) AS privilege(name)
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND pg_catalog.has_any_column_privilege(role_oid, relation.oid, privilege.name)
    ) THEN
        RAISE EXCEPTION 'Identity API-key LOGIN has direct table privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind = 'S'
          AND (pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'USAGE')
               OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'SELECT')
               OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'UPDATE'))
    ) THEN
        RAISE EXCEPTION 'Identity API-key LOGIN has direct sequence privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND (namespace.nspname <> 'yoao_identity_keys_v1'
               OR function.oid <> ALL (allowed_functions))
          AND function.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Identity API-key LOGIN has an unapproved module function grant';
    END IF;
    SELECT pg_catalog.count(*) INTO callable_count
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
    WHERE namespace.nspname = 'yoao_identity_keys_v1'
      AND function.prokind IN ('f', 'p')
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF callable_count <> pg_catalog.cardinality(allowed_functions) THEN
        RAISE EXCEPTION 'Identity API-key LOGIN has the wrong function count';
    END IF;
END $$;
\endif

\if :{?grant_offboarding}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config(
    'yoao.identity_offboarding_role', :'identity_offboarding_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.identity_offboarding_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.labeling_owner'),
        current_setting('yoao.audit_owner'),
        current_setting('yoao.identity_offboarding_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_identity_offboarding_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM unnest(role_names) AS candidate(name)) <> 5 THEN
        RAISE EXCEPTION 'Unexpected Identity offboarding role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Identity offboarding roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    labeling_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.labeling_owner')
    );
    audit_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.audit_owner')
    );
    offboarding_role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_offboarding_role')
    );
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR labeling_oid IS NULL
       OR audit_oid IS NULL OR offboarding_role_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Identity offboarding code owner or client';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_offboarding_v1.offboarding_policy()'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_offboarding_v1.apply_offboarding_command(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_offboarding_v1.prepare_offboarding_command(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.lock_identity_offboarding_scope(uuid,uuid[],uuid[])'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.identity_offboarding_replay_scope(uuid,uuid[])'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.identity_offboarding_facts(uuid)'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.transfer_identity_offboarding_owners(uuid,jsonb)'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.lock_identity_offboarding_work(uuid,uuid[])'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.identity_offboarding_facts(uuid)'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.transfer_identity_offboarding_work(uuid,jsonb)'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.release_identity_offboarding_task_locks(uuid)'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_audit_v1.record_identity_offboarding_event(jsonb,uuid,jsonb,jsonb)'
           )::oid) IS DISTINCT FROM audit_oid THEN
        RAISE EXCEPTION 'An Identity offboarding capability has the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.lock_identity_offboarding_scope(uuid,uuid[],uuid[])'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.identity_offboarding_replay_scope(uuid,uuid[])'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.identity_offboarding_facts(uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.transfer_identity_offboarding_owners(uuid,jsonb)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_labeling_v1.lock_identity_offboarding_work(uuid,uuid[])'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_labeling_v1.identity_offboarding_facts(uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_labeling_v1.transfer_identity_offboarding_work(uuid,jsonb)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_labeling_v1.release_identity_offboarding_task_locks(uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_audit_v1.record_identity_offboarding_event(jsonb,uuid,jsonb,jsonb)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'operation_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'actor_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'operation', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'target_user_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'request_fingerprint', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'derivation_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'target_generation', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'target_is_active', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'result', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'scope_basis', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'operation_id', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'actor_id', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'operation', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'target_user_id', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'request_fingerprint', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'derivation_id', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'target_generation', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'target_is_active', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'result', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'scope_basis', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_offboarding_operation_receipts',
        'audit_id', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'is_active', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_account_presence', 'status', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_kind', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_at', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_by', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_reason', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'owner_id', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'updated_at', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.project_members', 'id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.task_batches', 'name', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.task_batches', 'assigned_user_ids', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.task_batches', 'annotator_id', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.task_batches', 'reviewer_id', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.task_batches', 'assigned_user_ids', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.tasks', 'assignee_id', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.tasks', 'reviewer_id', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.tasks', 'assigned_at', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.tasks', 'updated_at', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.task_locks', 'id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.task_locks', 'id', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        audit_oid, 'public.audit_logs', 'detail_json', 'INSERT'
    ) THEN
        RAISE EXCEPTION 'Identity offboarding owners are missing a receipt, Workspace, Labeling or Audit capability';
    END IF;
    IF pg_catalog.has_table_privilege(
           identity_oid, 'public.projects', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.project_members', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.tasks', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.task_batches', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.projects', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.project_members', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.tasks', 'SELECT,INSERT,UPDATE,REFERENCES'
    ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.task_batches', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.task_locks', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.task_locks', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_table_privilege(
           offboarding_role_oid, 'public.identity_offboarding_operation_receipts',
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           offboarding_role_oid, 'public.users', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) THEN
        RAISE EXCEPTION 'Identity offboarding owner/client has raw table access';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_offboarding_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_offboarding_role";
DO $$
DECLARE
    namespace record;
    column_grant record;
    role_name text := current_setting('yoao.identity_offboarding_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
    END LOOP;
    FOR column_grant IN
        SELECT grant_row.table_schema, grant_row.table_name,
               grant_row.column_name, grant_row.privilege_type
        FROM information_schema.column_privileges AS grant_row
        WHERE grant_row.grantee = role_name
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE %s (%I) ON TABLE %I.%I FROM %I',
            column_grant.privilege_type,
            column_grant.column_name,
            column_grant.table_schema,
            column_grant.table_name,
            role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_offboarding_v1
    TO :"identity_offboarding_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_offboarding_v1.offboarding_policy(),
    yoao_identity_offboarding_v1.apply_offboarding_command(jsonb),
    yoao_identity_offboarding_v1.prepare_offboarding_command(jsonb)
    TO :"identity_offboarding_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_offboarding_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_identity_offboarding_v1.apply_offboarding_command(jsonb)',
        'yoao_identity_offboarding_v1.offboarding_policy()',
        'yoao_identity_offboarding_v1.prepare_offboarding_command(jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND function.prorettype NOT IN (
          'trigger'::pg_catalog.regtype, 'event_trigger'::pg_catalog.regtype
      )
      AND NOT EXISTS (
          SELECT 1
          FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Identity offboarding LOGIN has unexpected functions: %', actual_functions;
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
           role_oid, 'yoao_identity_offboarding_v1', 'USAGE'
       ) OR pg_catalog.has_schema_privilege(
           role_oid, 'yoao_identity_offboarding_v1', 'CREATE'
       ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
       OR pg_catalog.has_database_privilege(
           role_oid, current_database(), 'CREATE,TEMP'
       ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
                  'pg_catalog', 'information_schema',
                  'yoao_identity_offboarding_v1', 'public'
              )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(role_oid, relation.oid, 'SELECT')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'INSERT')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'UPDATE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'DELETE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'TRUNCATE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'REFERENCES')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'TRIGGER')
               OR pg_catalog.has_any_column_privilege(
                   role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
               ))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS sequence ON sequence.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND sequence.relkind = 'S'
          AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
    ) THEN
        RAISE EXCEPTION 'Identity offboarding LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_platform_roles}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config(
    'yoao.identity_platform_role_role', :'identity_platform_role_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.identity_platform_role_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.labeling_owner'),
        current_setting('yoao.audit_owner'),
        current_setting('yoao.identity_platform_role_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_identity_platform_role_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM unnest(role_names) AS candidate(name)) <> 5 THEN
        RAISE EXCEPTION 'Unexpected Identity platform-role role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Identity platform-role roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    labeling_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.labeling_owner')
    );
    audit_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.audit_owner')
    );
    platform_role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_platform_role_role')
    );
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR labeling_oid IS NULL
       OR audit_oid IS NULL OR platform_role_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Identity platform-role code owner or client';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_platform_roles_v1.platform_role_policy()'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_platform_roles_v1.execute_platform_role_command(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.identity_platform_role_facts(uuid)'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.lock_identity_platform_role_owned_projects(uuid)'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_labeling_v1.identity_platform_role_facts(uuid,uuid[])'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_audit_v1.record_identity_platform_role_change(jsonb,uuid,jsonb,text,text,text)'
           )::oid) IS DISTINCT FROM audit_oid THEN
        RAISE EXCEPTION 'An Identity platform-role capability has the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.identity_platform_role_facts(uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.lock_identity_platform_role_owned_projects(uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_labeling_v1.identity_platform_role_facts(uuid,uuid[])'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_audit_v1.record_identity_platform_role_change(jsonb,uuid,jsonb,text,text,text)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_platform_role_operation_receipts',
        'result', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'role', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        audit_oid, 'public.audit_logs', 'detail_json', 'INSERT'
    ) THEN
        RAISE EXCEPTION 'Identity owner is missing a role receipt, Workspace, Labeling or Audit capability';
    END IF;
    IF pg_catalog.has_table_privilege(
           identity_oid, 'public.projects', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.project_members', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.tasks', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.task_batches', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.projects', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.project_members', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.tasks', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.task_batches', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_table_privilege(
           platform_role_oid, 'public.identity_platform_role_operation_receipts',
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           platform_role_oid, 'public.users', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) THEN
        RAISE EXCEPTION 'Identity platform-role owner/client has raw table access';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_platform_role_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_platform_role_role";
DO $$
DECLARE
    namespace record;
    column_grant record;
    role_name text := current_setting('yoao.identity_platform_role_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
    END LOOP;
    FOR column_grant IN
        SELECT grant_row.table_schema, grant_row.table_name,
               grant_row.column_name, grant_row.privilege_type
        FROM information_schema.column_privileges AS grant_row
        WHERE grant_row.grantee = role_name
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE %s (%I) ON TABLE %I.%I FROM %I',
            column_grant.privilege_type,
            column_grant.column_name,
            column_grant.table_schema,
            column_grant.table_name,
            role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_platform_roles_v1
    TO :"identity_platform_role_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_platform_roles_v1.platform_role_policy(),
    yoao_identity_platform_roles_v1.execute_platform_role_command(jsonb)
    TO :"identity_platform_role_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_platform_role_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_identity_platform_roles_v1.execute_platform_role_command(jsonb)',
        'yoao_identity_platform_roles_v1.platform_role_policy()'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND function.prorettype NOT IN (
          'trigger'::pg_catalog.regtype, 'event_trigger'::pg_catalog.regtype
      )
      AND NOT EXISTS (
          SELECT 1
          FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Identity platform-role LOGIN has unexpected functions: %', actual_functions;
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
           role_oid, 'yoao_identity_platform_roles_v1', 'USAGE'
       ) OR pg_catalog.has_schema_privilege(
           role_oid, 'yoao_identity_platform_roles_v1', 'CREATE'
       ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
       OR pg_catalog.has_database_privilege(
           role_oid, current_database(), 'CREATE,TEMP'
       ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
                  'pg_catalog', 'information_schema',
                  'yoao_identity_platform_roles_v1', 'public'
              )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(role_oid, relation.oid, 'SELECT')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'INSERT')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'UPDATE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'DELETE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'TRUNCATE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'REFERENCES')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'TRIGGER')
               OR pg_catalog.has_any_column_privilege(
                   role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
               ))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS sequence ON sequence.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND sequence.relkind = 'S'
          AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
    ) THEN
        RAISE EXCEPTION 'Identity platform-role LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_provisioning}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config(
    'yoao.identity_provisioning_role', :'identity_provisioning_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.identity_provisioning_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.audit_owner'),
        current_setting('yoao.identity_provisioning_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_identity_provisioning_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM unnest(role_names) AS candidate(name)) <> 3 THEN
        RAISE EXCEPTION 'Unexpected Identity provisioning role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Identity provisioning roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    audit_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.audit_owner')
    );
    client_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_provisioning_role')
    );
BEGIN
    IF identity_oid IS NULL OR audit_oid IS NULL OR client_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Identity provisioning code owner or client';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_v1.read_provisioning_account(text)'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_v1.create_provisioning_account(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_v1._initialize_account_presence()'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_audit_v1.record_identity_bootstrap_admin(uuid,text,text)'
           )::oid) IS DISTINCT FROM audit_oid THEN
        RAISE EXCEPTION 'An Identity provisioning function has the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_audit_v1.record_identity_bootstrap_admin(uuid,text,text)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_schema_privilege(
        identity_oid, 'yoao_audit_v1', 'USAGE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'email', 'SELECT'
    ) OR EXISTS (
        SELECT 1
        FROM (VALUES
            ('id'), ('email'), ('name'), ('password_hash'),
            ('credential_generation'), ('role'), ('group_name'), ('group_id'),
            ('is_active'), ('email_verified_at')
        ) AS required(column_name)
        WHERE NOT pg_catalog.has_column_privilege(
            identity_oid, 'public.users', required.column_name, 'INSERT'
        )
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_account_presence', 'user_id', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_account_preferences', 'user_id', 'INSERT'
    ) THEN
        RAISE EXCEPTION 'Identity owner lacks the provisioning capabilities';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_provisioning_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_provisioning_role";

DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.identity_provisioning_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"identity_provisioning_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_v1.create_provisioning_account(jsonb),
    yoao_identity_v1.create_seed_persona_account(jsonb),
    yoao_identity_v1.read_provisioning_account(text)
    TO :"identity_provisioning_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_provisioning_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_identity_v1.create_provisioning_account(jsonb)',
        'yoao_identity_v1.create_seed_persona_account(jsonb)',
        'yoao_identity_v1.read_provisioning_account(text)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname,
                 pg_catalog.oidvectortypes(function.proargtypes)
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    JOIN pg_catalog.pg_type AS return_type
      ON return_type.oid = function.prorettype
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND NOT (
          return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND return_type.typname IN ('trigger', 'event_trigger')
      )
      AND NOT EXISTS (
          SELECT 1
          FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Identity provisioning LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
        role_oid, 'yoao_identity_v1', 'USAGE'
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_identity_v1', 'CREATE'
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'public', 'CREATE'
    ) OR pg_catalog.has_database_privilege(
        role_oid, current_database(), 'CREATE,TEMP'
    ) THEN
        RAISE EXCEPTION 'Identity provisioning LOGIN has unsafe DDL privileges';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema', 'yoao_identity_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation
          ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(
                   role_oid, relation.oid,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               ) OR pg_catalog.has_any_column_privilege(
                   role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
               ))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS sequence
          ON sequence.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND sequence.relkind = 'S'
          AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
    ) THEN
        RAISE EXCEPTION 'Identity provisioning LOGIN has raw application privileges';
    END IF;
END $$;
\endif

\if :{?grant_recovery}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config('yoao.settings_owner', :'settings_owner', true);
SELECT set_config('yoao.identity_recovery_role', :'identity_recovery_role', true);
SELECT set_config('yoao.identity_mail_role', :'identity_mail_role', true);
SELECT set_config('yoao.identity_mail_settings_role', :'identity_mail_settings_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    migration_oid oid;
    owner_oid oid;
    audit_oid oid;
    settings_oid oid;
    audit_function oid := pg_catalog.to_regprocedure(
        'yoao_audit_v1.record_identity_recovery_event(text,jsonb,jsonb,jsonb)'
    )::oid;
    audit_context_function oid := pg_catalog.to_regprocedure(
        'yoao_audit_v1.valid_request_context(jsonb)'
    )::oid;
    recovery_settings_function oid := pg_catalog.to_regprocedure(
        'yoao_settings_v1.identity_recovery_policy(jsonb)'
    )::oid;
    mail_settings_function oid := pg_catalog.to_regprocedure(
        'yoao_settings_v1.mail_delivery_policy(jsonb)'
    )::oid;
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.audit_owner'),
        current_setting('yoao.settings_owner'),
        current_setting('yoao.identity_recovery_role'),
        current_setting('yoao.identity_mail_role'),
        current_setting('yoao.identity_mail_settings_role')
    ];
    client_roles text[] := ARRAY[
        current_setting('yoao.identity_recovery_role'),
        current_setting('yoao.identity_mail_role'),
        current_setting('yoao.identity_mail_settings_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT pg_catalog.count(DISTINCT name)
        FROM pg_catalog.unnest(role_names) AS expanded(name)) <> 6
       OR current_setting('yoao.migration_role') = ANY(role_names) THEN
        RAISE EXCEPTION 'Migration, Identity, Audit, Settings and client roles must be distinct';
    END IF;
    IF current_setting('yoao.identity_recovery_role') <> 'yoao_identity_recovery_client'
       OR current_setting('yoao.identity_mail_role') <> 'yoao_identity_mail_client'
       OR current_setting('yoao.identity_mail_settings_role') <> 'yoao_identity_mail_settings_client' THEN
        RAISE EXCEPTION 'Recovery and credential mail must use their fixed LOGIN roles';
    END IF;

    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe Identity recovery role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing Identity recovery role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Identity recovery roles must be restricted and have no memberships: %', role_name;
        END IF;
    END LOOP;

    SELECT role.oid INTO migration_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.migration_role');
    SELECT role.oid INTO owner_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.identity_owner');
    SELECT role.oid INTO audit_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.audit_owner');
    SELECT role.oid INTO settings_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.settings_owner');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Missing Identity, Audit or Settings code owner';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.oid IN (owner_oid, audit_oid, settings_oid)
          AND (role.rolcanlogin OR role.rolinherit)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = ANY(client_roles)
          AND (NOT role.rolcanlogin OR role.rolinherit)
    ) THEN
        RAISE EXCEPTION 'Code owners must be NOLOGIN and recovery/mail clients LOGIN NOINHERIT';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'yoao_identity_recovery_v1'
          AND namespace.nspowner = migration_oid
    ) THEN
        RAISE EXCEPTION 'Identity recovery schema must remain owned by the migration role';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'yoao_identity_recovery_v1'
          AND function.prokind = 'f' AND function.proowner <> owner_oid
    ) THEN
        RAISE EXCEPTION 'Identity recovery functions must be owned by the Identity code owner';
    END IF;
    IF audit_function IS NULL OR audit_context_function IS NULL
       OR recovery_settings_function IS NULL OR mail_settings_function IS NULL
       OR NOT pg_catalog.has_function_privilege(owner_oid, audit_function, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(owner_oid, audit_context_function, 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(owner_oid, recovery_settings_function, 'EXECUTE')
       OR NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_proc AS function
           WHERE function.oid = audit_function AND function.proowner = audit_oid
    ) OR NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_proc AS function
           WHERE function.oid = recovery_settings_function
             AND function.proowner = settings_oid
       ) OR NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_proc AS function
           WHERE function.oid = mail_settings_function
             AND function.proowner = settings_oid
       ) OR pg_catalog.has_function_privilege(owner_oid, mail_settings_function, 'EXECUTE') THEN
        RAISE EXCEPTION 'Identity recovery owner lacks its typed Audit or Settings capability';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        settings_oid,
        mail_settings_function,
        'EXECUTE'
    ) OR pg_catalog.has_function_privilege(
        current_setting('yoao.identity_mail_settings_role'),
        recovery_settings_function,
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Mail settings must remain a separate single Settings capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_recovery_role", :"identity_mail_role", :"identity_mail_settings_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_recovery_role", :"identity_mail_role", :"identity_mail_settings_role";
DO $$
DECLARE
    namespace record;
    role_name text;
    client_roles text[] := ARRAY[
        current_setting('yoao.identity_recovery_role'),
        current_setting('yoao.identity_mail_role'),
        current_setting('yoao.identity_mail_settings_role')
    ];
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        FOREACH role_name IN ARRAY client_roles LOOP
            EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
            EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        END LOOP;
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_recovery_v1
    TO :"identity_recovery_role", :"identity_mail_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_recovery_v1.recovery_policy(),
    yoao_identity_recovery_v1.prepare_recovery_command(jsonb),
    yoao_identity_recovery_v1.apply_recovery_command(jsonb),
    yoao_identity_recovery_v1.read_registration_status(jsonb)
    TO :"identity_recovery_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_recovery_v1.mail_policy(),
    yoao_identity_recovery_v1.claim_mail(jsonb),
    yoao_identity_recovery_v1.renew_mail(jsonb),
    yoao_identity_recovery_v1.finish_mail(jsonb)
    TO :"identity_mail_role";
GRANT USAGE ON SCHEMA yoao_settings_v1 TO :"identity_mail_settings_role";
GRANT EXECUTE ON FUNCTION yoao_settings_v1.mail_delivery_policy(jsonb),
    yoao_settings_v1.claim_smtp_test(),
    yoao_settings_v1.finish_smtp_test(uuid,uuid,text,text,text,integer)
    TO :"identity_mail_settings_role";

DO $$
DECLARE
    role_entry record;
    role_oid oid;
    callable_count integer;
    recovery_schema oid := pg_catalog.to_regnamespace('yoao_identity_recovery_v1');
    settings_schema oid := pg_catalog.to_regnamespace('yoao_settings_v1');
BEGIN
    IF recovery_schema IS NULL OR settings_schema IS NULL THEN
        RAISE EXCEPTION 'Identity recovery or Settings schema is missing';
    END IF;
    FOR role_entry IN
        SELECT role_name, allowed_schema, allowed_functions
        FROM (VALUES
            (
                current_setting('yoao.identity_recovery_role'),
                'yoao_identity_recovery_v1'::text,
                ARRAY[
                    pg_catalog.to_regprocedure('yoao_identity_recovery_v1.recovery_policy()')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_recovery_v1.prepare_recovery_command(jsonb)')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_recovery_v1.apply_recovery_command(jsonb)')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_recovery_v1.read_registration_status(jsonb)')::oid
                ]::oid[]
            ),
            (
                current_setting('yoao.identity_mail_role'),
                'yoao_identity_recovery_v1'::text,
                ARRAY[
                    pg_catalog.to_regprocedure('yoao_identity_recovery_v1.mail_policy()')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_recovery_v1.claim_mail(jsonb)')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_recovery_v1.renew_mail(jsonb)')::oid,
                    pg_catalog.to_regprocedure('yoao_identity_recovery_v1.finish_mail(jsonb)')::oid
                ]::oid[]
            ),
            (
                current_setting('yoao.identity_mail_settings_role'),
                'yoao_settings_v1'::text,
                ARRAY[
                    pg_catalog.to_regprocedure('yoao_settings_v1.mail_delivery_policy(jsonb)')::oid,
                    pg_catalog.to_regprocedure('yoao_settings_v1.claim_smtp_test()')::oid,
                    pg_catalog.to_regprocedure('yoao_settings_v1.finish_smtp_test(uuid,uuid,text,text,text,integer)')::oid
                ]::oid[]
            )
        ) AS expected(role_name, allowed_schema, allowed_functions)
    LOOP
        SELECT role.oid INTO role_oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_entry.role_name;
        IF pg_catalog.array_position(role_entry.allowed_functions, NULL::oid) IS NOT NULL
           OR pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE')
           OR pg_catalog.has_database_privilege(role_oid, current_database(), 'TEMP') THEN
            RAISE EXCEPTION 'Unsafe or incomplete Identity recovery client: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
              AND namespace.nspname <> role_entry.allowed_schema
              AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
        ) THEN
            RAISE EXCEPTION 'Identity recovery client has a foreign module schema: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_namespace AS namespace
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE')
        ) THEN
            RAISE EXCEPTION 'Identity recovery client can create schema objects: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
            CROSS JOIN (VALUES ('SELECT'::text), ('INSERT'), ('UPDATE'), ('DELETE'), ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) AS privilege(name)
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.has_table_privilege(role_oid, relation.oid, privilege.name)
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
            CROSS JOIN (VALUES ('SELECT'::text), ('INSERT'), ('UPDATE'), ('REFERENCES')) AS privilege(name)
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.has_any_column_privilege(role_oid, relation.oid, privilege.name)
        ) THEN
            RAISE EXCEPTION 'Identity recovery client has direct table privileges: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
            WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND relation.relkind = 'S'
              AND (pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'USAGE')
                   OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'SELECT')
                   OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'UPDATE'))
        ) THEN
            RAISE EXCEPTION 'Identity recovery client has direct sequence privileges: %', role_entry.role_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
            WHERE namespace.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
              AND (namespace.nspname <> role_entry.allowed_schema
                   OR function.oid <> ALL (role_entry.allowed_functions))
              AND function.prokind IN ('f', 'p')
              AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE')
        ) THEN
            RAISE EXCEPTION 'Identity recovery client has an unapproved module function: %', role_entry.role_name;
        END IF;
        SELECT pg_catalog.count(*) INTO callable_count
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = role_entry.allowed_schema
          AND function.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
        IF callable_count <> pg_catalog.cardinality(role_entry.allowed_functions) THEN
            RAISE EXCEPTION 'Identity recovery client has the wrong function count: %', role_entry.role_name;
        END IF;
    END LOOP;
END $$;
\endif

\if :{?grant_role_audit}
SELECT set_config('yoao.grant_section_applied', '1', true);
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.audit_consumer', :'consumer_role', true);

DO $preflight$
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
END
$preflight$;

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
    v_migration name := current_setting('yoao.migration_role');
    v_identity name := current_setting('yoao.identity_owner');
    v_consumer name := current_setting('yoao.audit_consumer');
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
    WHERE namespace.nspname = 'yoao_identity_audit_v1';
    IF v_schema_oid IS NULL THEN
        EXECUTE pg_catalog.format(
            'CREATE SCHEMA yoao_identity_audit_v1 AUTHORIZATION %I', v_identity
        );
        SELECT namespace.oid INTO v_schema_oid FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'yoao_identity_audit_v1';
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
        RAISE EXCEPTION 'yoao_identity_audit_v1 has a foreign owner or unexpected object';
    END IF;

    FOR v_index IN 1..3 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('yoao_identity_audit_v1.%I', v_view_name)
        );
        IF v_view_oid IS NULL THEN
            IF NOT pg_catalog.has_schema_privilege(v_migration_oid, v_schema_oid, 'CREATE')
               AND NOT v_granted_create THEN
                EXECUTE pg_catalog.format(
                    'GRANT CREATE ON SCHEMA yoao_identity_audit_v1 TO %I', v_migration
                );
                v_granted_create := true;
            END IF;
            v_view_definition := pg_catalog.pg_get_viewdef(v_reference_oids[v_index], true);
            EXECUTE pg_catalog.format(
                'CREATE VIEW yoao_identity_audit_v1.%I WITH (security_barrier = true) AS %s',
                v_view_name, v_view_definition
            );
            EXECUTE pg_catalog.format(
                'ALTER VIEW yoao_identity_audit_v1.%I OWNER TO %I', v_view_name, v_identity
            );
            v_view_oid := pg_catalog.to_regclass(
                pg_catalog.format('yoao_identity_audit_v1.%I', v_view_name)
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
                'yoao_identity_audit_v1', v_view_name;
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
                'yoao_identity_audit_v1', v_view_name;
        END IF;
    END LOOP;
    IF v_granted_create THEN
        EXECUTE pg_catalog.format(
            'REVOKE CREATE ON SCHEMA yoao_identity_audit_v1 FROM %I', v_migration
        );
    END IF;

    EXECUTE 'REVOKE ALL ON SCHEMA yoao_identity_audit_v1 FROM PUBLIC';
    EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA yoao_identity_audit_v1 FROM %I', v_consumer);
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
            pg_catalog.format('yoao_identity_audit_v1.%I', v_view_name)
        );
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                     ORDER BY attribute.attnum)
        INTO v_view_columns FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_view_oid AND attribute.attnum > 0
          AND NOT attribute.attisdropped;
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE yoao_identity_audit_v1.%I FROM PUBLIC', v_view_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE yoao_identity_audit_v1.%I FROM PUBLIC',
            v_view_columns, v_view_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE yoao_identity_audit_v1.%I FROM %I',
            v_view_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE yoao_identity_audit_v1.%I FROM %I',
            v_view_columns, v_view_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'GRANT SELECT ON TABLE yoao_identity_audit_v1.%I TO %I',
            v_view_name, v_consumer
        );
    END LOOP;
    EXECUTE pg_catalog.format(
        'GRANT USAGE ON SCHEMA yoao_identity_audit_v1 TO %I', v_consumer
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
            pg_catalog.format('yoao_identity_audit_v1.%I', v_view_name)
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
        RAISE EXCEPTION 'PUBLIC retains access to yoao_identity_audit_v1';
    END IF;
END
$install$;
DROP VIEW pg_temp._aap_identity_role_audit_capabilities_ref;
DROP VIEW pg_temp._aap_identity_role_audit_invitation_ref;
DROP VIEW pg_temp._aap_identity_role_audit_account_ref;
\endif

\if :{?grant_scale_seed}
SELECT set_config('yoao.grant_section_applied', '1', true);
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.seed_operator', :'consumer_role', true);

DO $profile$
DECLARE
    v_consumer name := current_setting('yoao.seed_operator');
    v_identity name := current_setting('yoao.identity_owner');
    v_signature text;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR v_consumer IN (current_user, v_identity, 'identity_api_consumer')
       OR pg_has_role(v_consumer, v_identity, 'MEMBER')
       OR pg_has_role(v_consumer, current_user, 'MEMBER') THEN
        RAISE EXCEPTION 'Invalid Identity seed operator ownership';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_consumer AND rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT 1 FROM pg_auth_members
        WHERE member=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
           OR (roleid=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
               AND member<>(SELECT oid FROM pg_roles
                            WHERE rolname=current_setting('yoao.migration_role')))
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_identity AND NOT rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Seed operator requires non-admin, non-inheriting LOGIN';
    END IF;
    IF has_table_privilege(v_consumer, 'public.users',
        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR has_any_column_privilege(v_consumer, 'public.users',
        'SELECT,INSERT,UPDATE,REFERENCES')
       OR has_table_privilege(v_consumer, 'public.identity_account_presence',
        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR has_any_column_privilege(v_consumer, 'public.identity_account_presence',
        'SELECT,INSERT,UPDATE,REFERENCES') THEN
        RAISE EXCEPTION 'Apply the ordinary Identity consumer fence first';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_identity_v1.read_scale_seed_super_admin()',
        'yoao_identity_v1.read_scale_seed_annotator(uuid[])',
        'yoao_identity_v1.read_first_seed_account()'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_roles owner ON owner.oid=p.proowner
            WHERE p.oid=v_signature::regprocedure AND p.prosecdef AND p.provolatile='s'
              AND owner.rolname=v_identity
        ) THEN
            RAISE EXCEPTION 'Unexpected seed function owner or security mode';
        END IF;
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I', v_signature, v_consumer);
    END LOOP;
END $profile$;
\endif

\if :{?grant_seed_operator}
SELECT set_config('yoao.grant_section_applied', '1', true);
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.seed_operator', :'consumer_role', true);

DO $profile$
DECLARE
    v_consumer name := current_setting('yoao.seed_operator');
    v_identity name := current_setting('yoao.identity_owner');
    v_signature text;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR v_consumer IN (current_user, v_identity, 'identity_api_consumer')
       OR pg_has_role(v_consumer, v_identity, 'MEMBER')
       OR pg_has_role(v_consumer, current_user, 'MEMBER') THEN
        RAISE EXCEPTION 'Invalid Identity seed operator ownership';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_consumer AND rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT 1 FROM pg_auth_members
        WHERE member=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
           OR (roleid=(SELECT oid FROM pg_roles WHERE rolname=v_consumer)
               AND member<>(SELECT oid FROM pg_roles
                            WHERE rolname=current_setting('yoao.migration_role')))
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname=v_identity AND NOT rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Seed operator requires non-admin, non-inheriting LOGIN';
    END IF;
    IF has_table_privilege(v_consumer, 'public.users',
        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR has_any_column_privilege(v_consumer, 'public.users',
        'SELECT,INSERT,UPDATE,REFERENCES')
       OR has_table_privilege(v_consumer, 'public.identity_account_presence',
        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR has_any_column_privilege(v_consumer, 'public.identity_account_presence',
        'SELECT,INSERT,UPDATE,REFERENCES') THEN
        RAISE EXCEPTION 'Apply the ordinary Identity consumer fence first';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_identity_v1.read_provisioning_account(text)',
        'yoao_identity_v1.create_seed_persona_account(jsonb)'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_roles owner ON owner.oid=p.proowner
            WHERE p.oid=v_signature::regprocedure AND p.prosecdef
              AND owner.rolname=v_identity
        ) THEN
            RAISE EXCEPTION 'Unexpected seed function owner or security mode';
        END IF;
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I', v_signature, v_consumer);
    END LOOP;
END $profile$;
\endif

\if :{?grant_sessions}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.identity_session_role', :'identity_session_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    owner_oid oid;
    migration_oid oid;
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.identity_session_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT pg_catalog.count(DISTINCT name)
        FROM pg_catalog.unnest(role_names) AS expanded(name)) <> 2
       OR current_setting('yoao.migration_role') = ANY(role_names) THEN
        RAISE EXCEPTION 'Migration, Identity owner and session client must be distinct';
    END IF;
    IF current_setting('yoao.identity_session_role') <> 'yoao_identity_session_client' THEN
        RAISE EXCEPTION 'Identity sessions must use the yoao_identity_session_client LOGIN';
    END IF;

    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe Identity role name: %', role_name;
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Missing Identity role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'Unsafe Identity role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Identity roles must have no role memberships: %', role_name;
        END IF;
    END LOOP;

    SELECT role.oid INTO owner_oid
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.identity_owner');
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.oid = owner_oid AND (role.rolcanlogin OR role.rolinherit)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_session_role')
          AND (NOT role.rolcanlogin OR role.rolinherit)
    ) THEN
        RAISE EXCEPTION 'Identity owner must be NOLOGIN and session client LOGIN NOINHERIT';
    END IF;
    SELECT role.oid INTO migration_oid
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('yoao.migration_role');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Missing migration role';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'yoao_identity_sessions_v1'
          AND namespace.nspowner = migration_oid
    ) THEN
        RAISE EXCEPTION 'Identity session schema must remain owned by the migration role';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'yoao_identity_sessions_v1'
          AND function.prokind = 'f' AND function.proowner <> owner_oid
    ) THEN
        RAISE EXCEPTION 'Identity session functions must be owned by the Identity code owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        owner_oid,
        pg_catalog.to_regprocedure(
            'yoao_audit_v1.record_identity_session_event(text,jsonb,jsonb,jsonb)'
        )::oid,
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Identity code owner cannot call the Audit-owned session event capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"identity_session_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"identity_session_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.identity_session_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_sessions_v1 TO :"identity_session_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_sessions_v1.session_policy(),
    yoao_identity_sessions_v1.read_login_candidate(text),
    yoao_identity_sessions_v1.execute_session_command(jsonb)
    TO :"identity_session_role";

DO $$
DECLARE
    role_name text := current_setting('yoao.identity_session_role');
    role_oid oid;
    allowed_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_identity_sessions_v1.session_policy()')::oid,
        pg_catalog.to_regprocedure('yoao_identity_sessions_v1.read_login_candidate(text)')::oid,
        pg_catalog.to_regprocedure('yoao_identity_sessions_v1.execute_session_command(jsonb)')::oid
    ];
    audit_function oid := pg_catalog.to_regprocedure(
        'yoao_audit_v1.record_identity_session_event(text,jsonb,jsonb,jsonb)'
    )::oid;
    callable_count integer;
BEGIN
    SELECT role.oid INTO role_oid
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = role_name;
    IF pg_catalog.array_position(allowed_functions, NULL::oid) IS NOT NULL
       OR audit_function IS NULL THEN
        RAISE EXCEPTION 'Identity session protocol function is missing';
    END IF;
    IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(role_oid, current_database(), 'TEMP') THEN
        RAISE EXCEPTION 'Identity session LOGIN has database CREATE or TEMP privilege';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND namespace.nspname <> 'yoao_identity_sessions_v1'
          AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Identity session LOGIN has access to a foreign module schema';
    END IF;
    IF pg_catalog.has_schema_privilege(role_oid, 'yoao_audit_v1', 'USAGE')
       OR pg_catalog.has_function_privilege(role_oid, audit_function, 'EXECUTE') THEN
        RAISE EXCEPTION 'Identity session LOGIN has direct Audit capability';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE')
    ) THEN
        RAISE EXCEPTION 'Identity session LOGIN can create schema objects';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        CROSS JOIN (VALUES
            ('SELECT'::text), ('INSERT'), ('UPDATE'), ('DELETE'),
            ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
        ) AS privilege(name)
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND pg_catalog.has_table_privilege(role_oid, relation.oid, privilege.name)
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        CROSS JOIN (VALUES
            ('SELECT'::text), ('INSERT'), ('UPDATE'), ('REFERENCES')
        ) AS privilege(name)
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND pg_catalog.has_any_column_privilege(role_oid, relation.oid, privilege.name)
    ) THEN
        RAISE EXCEPTION 'Identity session LOGIN has direct table privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind = 'S'
          AND (pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'USAGE')
               OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'SELECT')
               OR pg_catalog.has_sequence_privilege(role_oid, relation.oid, 'UPDATE'))
    ) THEN
        RAISE EXCEPTION 'Identity session LOGIN has direct sequence privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname ~ '^(aap|yoao)_[a-z0-9_]+_v1$'
          AND (namespace.nspname <> 'yoao_identity_sessions_v1'
               OR function.oid <> ALL (allowed_functions))
          AND function.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Identity session LOGIN has an unapproved module function grant';
    END IF;
    SELECT pg_catalog.count(*) INTO callable_count
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
    WHERE namespace.nspname = 'yoao_identity_sessions_v1'
      AND function.prokind IN ('f', 'p')
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF callable_count <> pg_catalog.cardinality(allowed_functions) THEN
        RAISE EXCEPTION 'Identity session LOGIN has the wrong function count';
    END IF;
END $$;
\endif

\if :{?grant_suspensions}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config(
    'yoao.identity_suspension_role', :'identity_suspension_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.identity_suspension_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.audit_owner'),
        current_setting('yoao.identity_suspension_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_identity_suspension_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM unnest(role_names) AS candidate(name)) <> 4 THEN
        RAISE EXCEPTION 'Unexpected Identity suspension role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Identity suspension roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.workspace_owner')
    );
    audit_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.audit_owner')
    );
    suspension_role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_suspension_role')
    );
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL
       OR audit_oid IS NULL OR suspension_role_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Identity suspension code owner or client';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'yoao_identity_suspensions_v1.suspension_policy()'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_identity_suspensions_v1.execute_suspension_command(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_workspace_v1.lock_identity_suspension_scope(uuid,uuid,text)'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'yoao_audit_v1.record_identity_suspension_event(text,jsonb,uuid,jsonb,jsonb)'
           )::oid) IS DISTINCT FROM audit_oid THEN
        RAISE EXCEPTION 'An Identity suspension capability has the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_workspace_v1.lock_identity_suspension_scope(uuid,uuid,text)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'yoao_audit_v1.record_identity_suspension_event(text,jsonb,uuid,jsonb,jsonb)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_suspension_operation_receipts',
        'result', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'is_active', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_kind', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_at', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_by', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_reason', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        audit_oid, 'public.audit_logs', 'detail_json', 'INSERT'
    ) THEN
        RAISE EXCEPTION 'Identity owner is missing a suspension receipt, Workspace or Audit capability';
    END IF;
    IF pg_catalog.has_table_privilege(
           identity_oid, 'public.projects', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.project_members', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.tasks', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.task_batches', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.projects', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.project_members', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.tasks', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.task_batches', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_table_privilege(
           suspension_role_oid, 'public.identity_suspension_operation_receipts',
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           suspension_role_oid, 'public.users', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) THEN
        RAISE EXCEPTION 'Identity suspension owner/client has raw table access';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_suspension_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_suspension_role";
DO $$
DECLARE
    namespace record;
    column_grant record;
    role_name text := current_setting('yoao.identity_suspension_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
    END LOOP;
    FOR column_grant IN
        SELECT grant_row.table_schema, grant_row.table_name,
               grant_row.column_name, grant_row.privilege_type
        FROM information_schema.column_privileges AS grant_row
        WHERE grant_row.grantee = role_name
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE %s (%I) ON TABLE %I.%I FROM %I',
            column_grant.privilege_type,
            column_grant.column_name,
            column_grant.table_schema,
            column_grant.table_name,
            role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA yoao_identity_suspensions_v1
    TO :"identity_suspension_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_suspensions_v1.suspension_policy(),
    yoao_identity_suspensions_v1.execute_suspension_command(jsonb)
    TO :"identity_suspension_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.identity_suspension_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_identity_suspensions_v1.execute_suspension_command(jsonb)',
        'yoao_identity_suspensions_v1.suspension_policy()'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND function.prorettype NOT IN (
          'trigger'::pg_catalog.regtype, 'event_trigger'::pg_catalog.regtype
      )
      AND NOT EXISTS (
          SELECT 1
          FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Identity suspension LOGIN has unexpected functions: %', actual_functions;
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
           role_oid, 'yoao_identity_suspensions_v1', 'USAGE'
       ) OR pg_catalog.has_schema_privilege(
           role_oid, 'yoao_identity_suspensions_v1', 'CREATE'
       ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
       OR pg_catalog.has_database_privilege(
           role_oid, current_database(), 'CREATE,TEMP'
       ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
                  'pg_catalog', 'information_schema',
                  'yoao_identity_suspensions_v1', 'public'
              )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(role_oid, relation.oid, 'SELECT')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'INSERT')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'UPDATE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'DELETE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'TRUNCATE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'REFERENCES')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'TRIGGER')
               OR pg_catalog.has_any_column_privilege(
                   role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
               ))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS sequence ON sequence.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND sequence.relkind = 'S'
          AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
    ) THEN
        RAISE EXCEPTION 'Identity suspension LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

DO $$
BEGIN
    IF current_setting('yoao.grant_section_applied', true) IS DISTINCT FROM '1' THEN
        RAISE EXCEPTION 'No capability section selected; pass -v grant_<capability>=1';
    END IF;
END $$;
COMMIT;
