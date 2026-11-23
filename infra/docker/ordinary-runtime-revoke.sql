DO $ordinary_runtime_revoke$
DECLARE
    runtime_role text := pg_catalog.current_setting('yoao.application_role');
    database_oid oid;
    runtime_oid oid;
    schemas oid[];
    relations oid[];
    target record;
    columns text;
    defaults text;
BEGIN
    SELECT d.oid INTO database_oid FROM pg_catalog.pg_database AS d
    WHERE d.datname = pg_catalog.current_database()
      AND pg_catalog.pg_has_role(current_user, d.datdba, 'USAGE');
    IF database_oid IS NULL
       OR pg_catalog.current_database() <> pg_catalog.current_setting('yoao.expected_database')
       OR current_user <> pg_catalog.current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner for ordinary runtime grants';
    END IF;

    SELECT role.oid INTO runtime_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname::text = runtime_role AND role.rolcanlogin
      AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
               OR role.rolreplication OR role.rolbypassrls);
    IF runtime_oid IS NULL OR runtime_role = current_user OR EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS member WHERE member.member = runtime_oid
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_database AS d WHERE d.oid = database_oid AND d.datdba = runtime_oid
    ) THEN
        RAISE EXCEPTION 'Ordinary runtime must be a distinct non-admin LOGIN without inherited roles or database ownership';
    END IF;

    schemas := ARRAY(
        SELECT n.oid FROM pg_catalog.pg_namespace AS n
        WHERE n.nspname = 'public' OR pg_catalog.left(n.nspname,5) = 'yoao_'
           OR pg_catalog.left(n.nspname,4) = 'aap_'
    );
    relations := ARRAY(
        WITH RECURSIVE scoped(oid) AS (
            SELECT c.oid FROM pg_catalog.pg_class AS c
            WHERE c.relnamespace = ANY(schemas) AND c.relkind IN ('r','p','m','f','v','S')
            UNION
            SELECT i.inhrelid FROM pg_catalog.pg_inherits AS i JOIN scoped ON scoped.oid = i.inhparent
        ) SELECT oid FROM scoped
    );
    IF EXISTS (SELECT FROM pg_catalog.pg_namespace WHERE oid = ANY(schemas) AND nspowner = runtime_oid)
       OR EXISTS (SELECT FROM pg_catalog.pg_class WHERE oid = ANY(relations) AND relowner = runtime_oid)
       OR EXISTS (SELECT FROM pg_catalog.pg_proc WHERE pronamespace = ANY(schemas) AND proowner = runtime_oid) THEN
        RAISE EXCEPTION 'Ordinary runtime owns an application schema, relation or routine';
    END IF;

    -- A different object creator's global defaults cannot be countermanded by ours.
    FOR target IN
        SELECT d.defaclrole, d.defaclobjtype, n.nspname
        FROM pg_catalog.pg_default_acl AS d
        LEFT JOIN pg_catalog.pg_namespace AS n ON n.oid = d.defaclnamespace
        WHERE (d.defaclnamespace = 0 OR d.defaclnamespace = ANY(schemas))
          AND d.defaclobjtype IN ('r','S','f','n')
          AND EXISTS (SELECT FROM pg_catalog.aclexplode(d.defaclacl) AS a
                      WHERE a.grantee IN (0,runtime_oid))
    LOOP
        IF target.defaclrole <> current_user::pg_catalog.regrole THEN
            RAISE EXCEPTION 'Unsafe ordinary default privileges for creator %; revoke them as that creator',
                target.defaclrole::pg_catalog.regrole;
        END IF;
        defaults := CASE target.defaclobjtype
            WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES'
            WHEN 'f' THEN 'FUNCTIONS' WHEN 'n' THEN 'SCHEMAS' END;
        EXECUTE pg_catalog.format('ALTER DEFAULT PRIVILEGES%s REVOKE ALL ON %s FROM %I, PUBLIC',
            CASE WHEN target.nspname IS NULL THEN ''
                ELSE pg_catalog.format(' IN SCHEMA %I', target.nspname) END,
            defaults, runtime_role);
    END LOOP;
    EXECUTE pg_catalog.format('ALTER DEFAULT PRIVILEGES REVOKE ALL ON FUNCTIONS FROM %I, PUBLIC', runtime_role);

    EXECUTE pg_catalog.format('REVOKE CREATE ON DATABASE %I FROM %I, PUBLIC',
        pg_catalog.current_database(), runtime_role);
    IF pg_catalog.has_database_privilege(runtime_oid, database_oid, 'CREATE') THEN
        RAISE EXCEPTION 'Ordinary runtime retains database CREATE';
    END IF;
    FOR target IN SELECT oid,nspname FROM pg_catalog.pg_namespace WHERE oid = ANY(schemas) LOOP
        IF target.nspname = 'public' THEN
            REVOKE CREATE ON SCHEMA public FROM PUBLIC;
            EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA public FROM %I', runtime_role);
        ELSE
            EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I, PUBLIC', target.nspname, runtime_role);
        END IF;
        IF pg_catalog.has_schema_privilege(runtime_oid, target.oid, 'CREATE') OR
            (target.nspname <> 'public' AND pg_catalog.has_schema_privilege(runtime_oid, target.oid, 'USAGE')) THEN
            RAISE EXCEPTION 'Ordinary runtime retains schema privileges on %', target.nspname;
        END IF;
    END LOOP;

    FOR target IN
        SELECT c.oid, c.relkind, n.nspname, c.relname FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace WHERE c.oid = ANY(relations)
    LOOP
        IF EXISTS (SELECT FROM pg_catalog.pg_class AS c CROSS JOIN LATERAL
                       pg_catalog.aclexplode(c.relacl) AS a
                   WHERE c.oid = target.oid AND a.grantee IN (0,runtime_oid)) THEN
            EXECUTE pg_catalog.format('REVOKE ALL ON %s %I.%I FROM %I, PUBLIC',
                CASE WHEN target.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
                target.nspname, target.relname, runtime_role);
        END IF;
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ',' ORDER BY attribute.attnum)
        INTO columns FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = target.oid AND NOT attribute.attisdropped
          AND EXISTS (SELECT FROM pg_catalog.aclexplode(attribute.attacl) AS a
                      WHERE a.grantee IN (0,runtime_oid));
        IF columns IS NOT NULL THEN
            EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I, PUBLIC',
                columns, target.nspname, target.relname, runtime_role);
        END IF;
        IF EXISTS (SELECT FROM pg_catalog.pg_class AS c CROSS JOIN LATERAL
                       pg_catalog.aclexplode(c.relacl) AS a
                   WHERE c.oid = target.oid AND a.grantee IN (0,runtime_oid)) OR EXISTS (
            SELECT FROM pg_catalog.pg_attribute AS attribute CROSS JOIN LATERAL
                pg_catalog.aclexplode(attribute.attacl) AS a
            WHERE attribute.attrelid = target.oid AND NOT attribute.attisdropped
              AND a.grantee IN (0,runtime_oid)
        ) THEN
            RAISE EXCEPTION 'Ordinary runtime retains private relation privileges on %.%', target.nspname, target.relname;
        END IF;
    END LOOP;

    FOR target IN
        SELECT p.oid, p.oid::pg_catalog.regprocedure AS signature
        FROM pg_catalog.pg_proc AS p WHERE p.pronamespace = ANY(schemas)
          AND NOT EXISTS (SELECT FROM pg_catalog.pg_depend AS d
                          WHERE d.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
                            AND d.objid = p.oid AND d.deptype = 'e')
          AND pg_catalog.has_function_privilege(runtime_oid, p.oid, 'EXECUTE')
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON ROUTINE %s FROM %I, PUBLIC', target.signature, runtime_role);
        IF pg_catalog.has_function_privilege(runtime_oid, target.oid, 'EXECUTE') THEN
            RAISE EXCEPTION 'Ordinary runtime retains routine privileges on %', target.signature;
        END IF;
    END LOOP;
END;
$ordinary_runtime_revoke$;
