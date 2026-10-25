-- Apply after owner grants to an existing API role using remote Identity.
-- Embedded Identity and isolated test-seed operators retain their own profiles.
-- psql -v expected_database=... -v migration_role=... -v identity_owner=...
--      -v consumer_role=... -f identity-consumer-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.identity_consumer_role', :'consumer_role', true);

DO $guard$
DECLARE
    v_consumer name := current_setting('aap.identity_consumer_role');
    v_identity name := current_setting('aap.identity_owner');
    v_allowed oid[] := ARRAY[
        'aap_identity_v1.read_actor_facts(uuid)'::regprocedure::oid,
        'aap_identity_v1.lock_accounts_share(uuid[],boolean)'::regprocedure::oid,
        'aap_identity_v1.lock_accounts_update(uuid[],boolean)'::regprocedure::oid,
        'aap_identity_v1.lock_accounts_key_share(uuid[],boolean)'::regprocedure::oid,
        'aap_identity_v1.check_accepted_api_key(uuid,uuid,text[])'::regprocedure::oid
    ];
    v_relation record;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role')
       OR v_identity=current_user
       OR v_consumer IN (current_user, v_identity)
       OR pg_catalog.pg_has_role(v_consumer, current_user, 'MEMBER')
       OR pg_catalog.pg_has_role(v_consumer, v_identity, 'MEMBER') THEN
        RAISE EXCEPTION 'Invalid Identity consumer deployment ownership';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=v_identity
          AND NOT (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Identity owner must be a non-admin NOLOGIN role';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname=v_consumer AND rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members
        WHERE member=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=v_consumer)
    ) THEN
        RAISE EXCEPTION 'Identity consumer must be a non-admin LOGIN without memberships';
    END IF;
    IF (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_roles owner ON owner.oid=p.proowner
        WHERE p.oid=ANY(v_allowed) AND owner.rolname=v_identity) <> 5 THEN
        RAISE EXCEPTION 'Identity consumer capabilities have unexpected ownership';
    END IF;
    FOR v_relation IN
        SELECT c.oid,c.relname,c.relkind,c.relowner,c.reloptions,
               ARRAY(SELECT a.attname::text FROM pg_catalog.pg_attribute a
                     WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
                     ORDER BY a.attnum) AS columns
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='aap_identity_v1'
          AND c.relname IN ('account_profiles','invitation_audit_targets')
    LOOP
        IF v_relation.relkind <> 'v'
           OR v_relation.relowner<>(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=v_identity)
           OR NOT COALESCE(v_relation.reloptions @> ARRAY['security_barrier=true'],false)
           OR v_relation.columns IS DISTINCT FROM (CASE v_relation.relname
                WHEN 'account_profiles' THEN ARRAY['id','name','email','role','is_active','status','avatar_ref']
                ELSE ARRAY['id','project_id'] END) THEN
            RAISE EXCEPTION 'Identity read relation does not match its fixed contract';
        END IF;
    END LOOP;
    IF pg_catalog.to_regclass('aap_identity_v1.account_profiles') IS NULL
       OR pg_catalog.to_regclass('aap_identity_v1.invitation_audit_targets') IS NULL THEN
        RAISE EXCEPTION 'Identity read relations are missing';
    END IF;
END;
$guard$;

-- Remove both table and column grants; NOINHERIT alone cannot close direct ACLs.
DO $revoke$
DECLARE
    v_consumer name := current_setting('aap.identity_consumer_role');
    v_schema record;
    v_relation record;
    v_columns text;
BEGIN
    FOR v_relation IN
        SELECT c.oid,n.nspname,c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r','p','v','m','f') AND (
            n.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$'
            OR (n.nspname='public' AND (
                c.relname IN ('users','api_keys','groups','organizations',
                    'organization_members','user_invitations',
                    'password_reset_tokens','email_verification_tokens')
                OR pg_catalog.left(c.relname,9)='identity_'
            ))
        )
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON TABLE %I.%I FROM %I',
            v_relation.nspname,v_relation.relname,v_consumer);
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),',' ORDER BY attnum)
        INTO v_columns FROM pg_catalog.pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I',
                v_columns,v_relation.nspname,v_relation.relname,v_consumer);
        END IF;
    END LOOP;
    FOR v_schema IN
        SELECT nspname FROM pg_catalog.pg_namespace
        WHERE nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            v_schema.nspname,v_consumer);
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I',
            v_schema.nspname,v_consumer);
    END LOOP;
END;
$revoke$;

GRANT USAGE ON SCHEMA aap_identity_v1 TO :"consumer_role";
GRANT SELECT ON aap_identity_v1.account_profiles,
    aap_identity_v1.invitation_audit_targets TO :"consumer_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_v1.read_actor_facts(uuid),
    aap_identity_v1.lock_accounts_share(uuid[],boolean),
    aap_identity_v1.lock_accounts_update(uuid[],boolean),
    aap_identity_v1.lock_accounts_key_share(uuid[],boolean),
    aap_identity_v1.check_accepted_api_key(uuid,uuid,text[])
    TO :"consumer_role";

DO $verify$
DECLARE
    v_consumer name := current_setting('aap.identity_consumer_role');
    v_allowed oid[] := ARRAY[
        'aap_identity_v1.read_actor_facts(uuid)'::regprocedure::oid,
        'aap_identity_v1.lock_accounts_share(uuid[],boolean)'::regprocedure::oid,
        'aap_identity_v1.lock_accounts_update(uuid[],boolean)'::regprocedure::oid,
        'aap_identity_v1.lock_accounts_key_share(uuid[],boolean)'::regprocedure::oid,
        'aap_identity_v1.check_accepted_api_key(uuid,uuid,text[])'::regprocedure::oid
    ];
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$'
          AND pg_catalog.has_function_privilege(v_consumer,p.oid,'EXECUTE')
          AND NOT p.oid=ANY(v_allowed)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r','p','v','m','f') AND (
            n.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$'
            OR (n.nspname='public' AND (
                c.relname IN ('users','api_keys','groups','organizations',
                    'organization_members','user_invitations',
                    'password_reset_tokens','email_verification_tokens')
                OR pg_catalog.left(c.relname,9)='identity_'
            ))
        ) AND (
            pg_catalog.has_table_privilege(v_consumer,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
            OR pg_catalog.has_any_column_privilege(v_consumer,c.oid,'INSERT,UPDATE,REFERENCES')
            OR (c.oid NOT IN ('aap_identity_v1.account_profiles'::regclass,
                             'aap_identity_v1.invitation_audit_targets'::regclass)
                AND (pg_catalog.has_table_privilege(v_consumer,c.oid,'SELECT')
                     OR pg_catalog.has_any_column_privilege(v_consumer,c.oid,'SELECT')))
        )
    ) THEN
        RAISE EXCEPTION 'Identity consumer retains private data or command privileges';
    END IF;
END;
$verify$;
COMMIT;
