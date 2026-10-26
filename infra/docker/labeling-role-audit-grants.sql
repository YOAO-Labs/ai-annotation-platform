-- Install fixed historical Labeling audit projections for one trusted LOGIN.
-- This explicit installer works before revision 0173 and does not create roles,
-- alter business data, install a migration, or advance Alembic.
-- psql -v expected_database=... -v migration_role=... -v labeling_owner=...
--      -v consumer_role=... -f labeling-role-audit-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.labeling_owner', :'labeling_owner', true);
SELECT set_config('aap.audit_consumer', :'consumer_role', true);

DO $preflight$
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
END
$preflight$;

-- References freeze the exact projection definitions without replacing view OIDs.
CREATE TEMP VIEW _aap_labeling_role_audit_task_legacy_ref
WITH (security_barrier = true) AS
SELECT task.id, task.project_id, task.status, task.assignee_id, task.reviewer_id,
       task.batch_id, task.review_round_id, task.reviewer_claimed_at,
       task.reviewed_at, task.first_reviewed_at, task.first_review_result,
       NULL::jsonb AS annotation_contributor_ids,
       NULL::jsonb AS review_contributor_ids,
       NULL::uuid AS review_submitter_id
FROM public.tasks AS task;

CREATE TEMP VIEW _aap_labeling_role_audit_batch_ref
WITH (security_barrier = true) AS
SELECT batch.id, batch.annotator_id, batch.reviewer_id
FROM public.task_batches AS batch;

CREATE TEMP VIEW _aap_labeling_role_audit_annotation_ref
WITH (security_barrier = true) AS
SELECT annotation.source, annotation.user_id, annotation.is_active,
       annotation.was_cancelled
FROM public.annotations AS annotation;

CREATE TEMP VIEW _aap_labeling_role_audit_lock_ref
WITH (security_barrier = true) AS
SELECT task_lock.expire_at
FROM public.task_locks AS task_lock;

CREATE TEMP VIEW _aap_labeling_role_audit_capabilities_ref
WITH (security_barrier = true) AS
SELECT EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.tasks'::pg_catalog.regclass
      AND attribute.attname::text = 'annotation_contributor_ids'
      AND attribute.attnum > 0 AND NOT attribute.attisdropped
) AS task_annotation_contributor_ids,
EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.tasks'::pg_catalog.regclass
      AND attribute.attname::text = 'review_contributor_ids'
      AND attribute.attnum > 0 AND NOT attribute.attisdropped
) AS task_review_contributor_ids,
EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.tasks'::pg_catalog.regclass
      AND attribute.attname::text = 'review_submitter_id'
      AND attribute.attnum > 0 AND NOT attribute.attisdropped
) AS task_review_submitter_id;

DO $install$
DECLARE
    v_migration name := current_setting('aap.migration_role');
    v_labeling name := current_setting('aap.labeling_owner');
    v_consumer name := current_setting('aap.audit_consumer');
    v_migration_oid oid;
    v_labeling_oid oid;
    v_consumer_oid oid;
    v_tasks_oid oid;
    v_batches_oid oid;
    v_annotations_oid oid;
    v_locks_oid oid;
    v_schema_oid oid;
    v_view_oid oid;
    v_reference_oids oid[];
    v_legacy_task_reference oid := pg_catalog.to_regclass(
        'pg_temp._aap_labeling_role_audit_task_legacy_ref'
    );
    v_view_names text[] := ARRAY[
        'task_facts', 'task_batch_facts', 'annotation_facts',
        'task_lock_facts', 'schema_capabilities'
    ];
    v_source_names text[] := ARRAY[
        'tasks', 'task_batches', 'annotations', 'task_locks'
    ];
    v_view_name text;
    v_source_name text;
    v_all_source_columns text;
    v_view_columns text;
    v_columns text[];
    v_column_types text[];
    v_expected_columns text[];
    v_expected_types text[];
    v_view_definition text;
    v_task_annotation_expression text;
    v_task_review_expression text;
    v_task_submitter_expression text;
    v_has_annotation_ids boolean;
    v_has_review_ids boolean;
    v_has_review_submitter boolean;
    v_granted_create boolean := false;
    v_index integer;
BEGIN
    IF v_migration !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_labeling !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_consumer !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_labeling = v_migration
       OR v_consumer IN (
            v_migration, v_labeling, 'identity_api_consumer',
            'workspace_api_consumer', 'labeling_core_read_client'
       ) THEN
        RAISE EXCEPTION 'Invalid or overlapping Labeling audit roles';
    END IF;
    IF pg_catalog.array_position(ARRAY[v_legacy_task_reference], NULL::oid) IS NOT NULL THEN
        RAISE EXCEPTION 'Failed to create Labeling audit legacy reference view';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(20261023, 303);

    SELECT role.oid INTO v_migration_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_migration;
    SELECT role.oid INTO v_labeling_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_labeling;
    SELECT role.oid INTO v_consumer_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_consumer;
    IF v_migration_oid IS NULL OR v_labeling_oid IS NULL OR v_consumer_oid IS NULL THEN
        RAISE EXCEPTION 'Migration, Labeling owner, and audit consumer roles must exist';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.oid = v_labeling_oid
          AND NOT role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.oid = v_consumer_oid
          AND role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Labeling owner must be NOLOGIN and audit consumer a restricted LOGIN';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = v_consumer_oid OR membership.roleid = v_consumer_oid
           OR membership.member = v_labeling_oid
           OR (membership.roleid = v_labeling_oid
               AND membership.member <> v_migration_oid)
    ) THEN
        RAISE EXCEPTION 'Audit consumer or Labeling owner has an unsafe role membership';
    END IF;
    IF NOT pg_catalog.pg_has_role(v_migration_oid, v_labeling_oid, 'MEMBER')
       AND NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_roles AS role
           WHERE role.oid = v_migration_oid AND role.rolsuper
       ) THEN
        RAISE EXCEPTION 'Migration role cannot transfer views to the Labeling owner';
    END IF;

    -- These stable source columns exist at 0171; preparation columns are optional.
    IF EXISTS (
        SELECT 1
        FROM (VALUES
            ('tasks', 'id', 'uuid'),
            ('tasks', 'project_id', 'uuid'),
            ('tasks', 'status', 'character varying(30)'),
            ('tasks', 'assignee_id', 'uuid'),
            ('tasks', 'reviewer_id', 'uuid'),
            ('tasks', 'batch_id', 'uuid'),
            ('tasks', 'review_round_id', 'uuid'),
            ('tasks', 'reviewer_claimed_at', 'timestamp with time zone'),
            ('tasks', 'reviewed_at', 'timestamp with time zone'),
            ('tasks', 'first_reviewed_at', 'timestamp with time zone'),
            ('tasks', 'first_review_result', 'character varying(16)'),
            ('task_batches', 'id', 'uuid'),
            ('task_batches', 'annotator_id', 'uuid'),
            ('task_batches', 'reviewer_id', 'uuid'),
            ('annotations', 'source', 'character varying(20)'),
            ('annotations', 'user_id', 'uuid'),
            ('annotations', 'is_active', 'boolean'),
            ('annotations', 'was_cancelled', 'boolean'),
            ('task_locks', 'expire_at', 'timestamp with time zone')
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
        RAISE EXCEPTION 'Historical Labeling source schema is incompatible';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM (VALUES
            ('annotation_contributor_ids', 'jsonb'),
            ('review_contributor_ids', 'jsonb'),
            ('review_submitter_id', 'uuid')
        ) AS expected(column_name, type_name)
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.nspname = 'public'
        JOIN pg_catalog.pg_class AS relation
          ON relation.relnamespace = namespace.oid AND relation.relname = 'tasks'
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = relation.oid
         AND attribute.attname = expected.column_name
         AND attribute.attnum > 0 AND NOT attribute.attisdropped
        WHERE pg_catalog.format_type(attribute.atttypid, attribute.atttypmod)
              IS DISTINCT FROM expected.type_name
    ) THEN
        RAISE EXCEPTION 'Optional Labeling preparation columns have incompatible types';
    END IF;

    SELECT relation.oid INTO v_tasks_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'tasks';
    SELECT relation.oid INTO v_batches_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'task_batches';
    SELECT relation.oid INTO v_annotations_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'annotations';
    SELECT relation.oid INTO v_locks_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'task_locks';
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_index AS primary_key
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = primary_key.indrelid AND attribute.attname = 'id'
        WHERE primary_key.indrelid = v_tasks_oid AND primary_key.indisprimary
          AND primary_key.indnatts = 1 AND primary_key.indkey[0] = attribute.attnum
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_index AS primary_key
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = primary_key.indrelid AND attribute.attname = 'id'
        WHERE primary_key.indrelid = v_batches_oid AND primary_key.indisprimary
          AND primary_key.indnatts = 1 AND primary_key.indkey[0] = attribute.attnum
    ) THEN
        RAISE EXCEPTION 'Historical Task and TaskBatch IDs must remain primary keys';
    END IF;
    IF v_tasks_oid IS NULL OR v_batches_oid IS NULL
       OR v_annotations_oid IS NULL OR v_locks_oid IS NULL THEN
        RAISE EXCEPTION 'A required Labeling audit source is missing';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS relation
        WHERE relation.oid = ANY(ARRAY[
            v_tasks_oid, v_batches_oid, v_annotations_oid, v_locks_oid
        ]) AND relation.relowner = v_consumer_oid
    ) THEN
        RAISE EXCEPTION 'Audit consumer must not own a private Labeling source';
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_tasks_oid
          AND attribute.attname = 'annotation_contributor_ids'
          AND attribute.attnum > 0 AND NOT attribute.attisdropped
    ) INTO v_has_annotation_ids;
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_tasks_oid
          AND attribute.attname = 'review_contributor_ids'
          AND attribute.attnum > 0 AND NOT attribute.attisdropped
    ) INTO v_has_review_ids;
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_tasks_oid
          AND attribute.attname = 'review_submitter_id'
          AND attribute.attnum > 0 AND NOT attribute.attisdropped
    ) INTO v_has_review_submitter;
    v_task_annotation_expression := CASE WHEN v_has_annotation_ids
        THEN 'task.annotation_contributor_ids' ELSE 'NULL::jsonb' END;
    v_task_review_expression := CASE WHEN v_has_review_ids
        THEN 'task.review_contributor_ids' ELSE 'NULL::jsonb' END;
    v_task_submitter_expression := CASE WHEN v_has_review_submitter
        THEN 'task.review_submitter_id' ELSE 'NULL::uuid' END;
    EXECUTE pg_catalog.format(
        'CREATE TEMP VIEW _aap_labeling_role_audit_task_ref '
        'WITH (security_barrier = true) AS '
        'SELECT task.id, task.project_id, task.status, task.assignee_id, '
        'task.reviewer_id, task.batch_id, task.review_round_id, '
        'task.reviewer_claimed_at, task.reviewed_at, task.first_reviewed_at, '
        'task.first_review_result, %s AS annotation_contributor_ids, '
        '%s AS review_contributor_ids, %s AS review_submitter_id '
        'FROM public.tasks AS task',
        v_task_annotation_expression, v_task_review_expression,
        v_task_submitter_expression
    );
    v_reference_oids := ARRAY[
        pg_catalog.to_regclass('pg_temp._aap_labeling_role_audit_task_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_labeling_role_audit_batch_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_labeling_role_audit_annotation_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_labeling_role_audit_lock_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_labeling_role_audit_capabilities_ref')::oid
    ];
    IF pg_catalog.array_position(v_reference_oids, NULL::oid) IS NOT NULL THEN
        RAISE EXCEPTION 'Failed to create Labeling audit reference views';
    END IF;

    IF NOT pg_catalog.has_schema_privilege(v_labeling_oid, 'public', 'USAGE') THEN
        EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA public TO %I', v_labeling);
    END IF;
    EXECUTE pg_catalog.format(
        'GRANT SELECT (id, project_id, status, assignee_id, reviewer_id, batch_id, '
        'review_round_id, reviewer_claimed_at, reviewed_at, first_reviewed_at, '
        'first_review_result) ON TABLE public.tasks TO %I', v_labeling
    );
    IF v_has_annotation_ids THEN
        EXECUTE pg_catalog.format(
            'GRANT SELECT (annotation_contributor_ids) ON TABLE public.tasks TO %I',
            v_labeling
        );
    END IF;
    IF v_has_review_ids THEN
        EXECUTE pg_catalog.format(
            'GRANT SELECT (review_contributor_ids) ON TABLE public.tasks TO %I',
            v_labeling
        );
    END IF;
    IF v_has_review_submitter THEN
        EXECUTE pg_catalog.format(
            'GRANT SELECT (review_submitter_id) ON TABLE public.tasks TO %I',
            v_labeling
        );
    END IF;
    EXECUTE pg_catalog.format(
        'GRANT SELECT (id, annotator_id, reviewer_id) '
        'ON TABLE public.task_batches TO %I', v_labeling
    );
    EXECUTE pg_catalog.format(
        'GRANT SELECT (source, user_id, is_active, was_cancelled) '
        'ON TABLE public.annotations TO %I', v_labeling
    );
    EXECUTE pg_catalog.format(
        'GRANT SELECT (expire_at) ON TABLE public.task_locks TO %I', v_labeling
    );
    IF pg_catalog.has_table_privilege(v_labeling_oid, v_tasks_oid, 'SELECT')
       OR pg_catalog.has_table_privilege(v_labeling_oid, v_batches_oid, 'SELECT')
       OR pg_catalog.has_table_privilege(v_labeling_oid, v_annotations_oid, 'SELECT')
       OR pg_catalog.has_table_privilege(v_labeling_oid, v_locks_oid, 'SELECT') THEN
        RAISE EXCEPTION 'Labeling audit owner has unexpected table-level source SELECT';
    END IF;

    SELECT namespace.oid INTO v_schema_oid
    FROM pg_catalog.pg_namespace AS namespace
    WHERE namespace.nspname = 'aap_labeling_audit_v1';
    IF v_schema_oid IS NULL THEN
        EXECUTE pg_catalog.format(
            'CREATE SCHEMA aap_labeling_audit_v1 AUTHORIZATION %I', v_labeling
        );
        SELECT namespace.oid INTO v_schema_oid
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'aap_labeling_audit_v1';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.oid = v_schema_oid AND namespace.nspowner = v_labeling_oid
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS relation
        WHERE relation.relnamespace = v_schema_oid
          AND relation.relname <> ALL (v_view_names)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS routine
        WHERE routine.pronamespace = v_schema_oid
    ) THEN
        RAISE EXCEPTION 'aap_labeling_audit_v1 has a foreign owner or unexpected object';
    END IF;

    FOR v_index IN 1..5 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('aap_labeling_audit_v1.%I', v_view_name)
        );
        IF v_view_oid IS NULL THEN
            IF NOT pg_catalog.has_schema_privilege(v_migration_oid, v_schema_oid, 'CREATE')
               AND NOT v_granted_create THEN
                EXECUTE pg_catalog.format(
                    'GRANT CREATE ON SCHEMA aap_labeling_audit_v1 TO %I', v_migration
                );
                v_granted_create := true;
            END IF;
            v_view_definition := pg_catalog.pg_get_viewdef(v_reference_oids[v_index], true);
            EXECUTE pg_catalog.format(
                'CREATE VIEW aap_labeling_audit_v1.%I '
                'WITH (security_barrier = true) AS %s',
                v_view_name, v_view_definition
            );
            EXECUTE pg_catalog.format(
                'ALTER VIEW aap_labeling_audit_v1.%I OWNER TO %I',
                v_view_name, v_labeling
            );
            v_view_oid := pg_catalog.to_regclass(
                pg_catalog.format('aap_labeling_audit_v1.%I', v_view_name)
            );
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_class AS relation
            WHERE relation.oid = v_view_oid AND relation.relkind = 'v'
              AND relation.relowner = v_labeling_oid
              AND COALESCE(relation.reloptions, ARRAY[]::text[])
                  @> ARRAY['security_barrier=true']
        ) THEN
            RAISE EXCEPTION 'aap_labeling_audit_v1.% has an unexpected owner, kind, or security option',
                v_view_name;
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
                v_expected_columns := ARRAY[
                    'id', 'project_id', 'status', 'assignee_id', 'reviewer_id',
                    'batch_id', 'review_round_id', 'reviewer_claimed_at', 'reviewed_at',
                    'first_reviewed_at', 'first_review_result',
                    'annotation_contributor_ids', 'review_contributor_ids',
                    'review_submitter_id'
                ];
                v_expected_types := ARRAY[
                    'uuid', 'uuid', 'character varying(30)', 'uuid', 'uuid', 'uuid',
                    'uuid', 'timestamp with time zone', 'timestamp with time zone',
                    'timestamp with time zone', 'character varying(16)', 'jsonb',
                    'jsonb', 'uuid'
                ];
            WHEN 2 THEN
                v_expected_columns := ARRAY['id', 'annotator_id', 'reviewer_id'];
                v_expected_types := ARRAY['uuid', 'uuid', 'uuid'];
            WHEN 3 THEN
                v_expected_columns := ARRAY['source', 'user_id', 'is_active', 'was_cancelled'];
                v_expected_types := ARRAY[
                    'character varying(20)', 'uuid', 'boolean', 'boolean'
                ];
            WHEN 4 THEN
                v_expected_columns := ARRAY['expire_at'];
                v_expected_types := ARRAY['timestamp with time zone'];
            ELSE
                v_expected_columns := ARRAY[
                    'task_annotation_contributor_ids', 'task_review_contributor_ids',
                    'task_review_submitter_id'
                ];
                v_expected_types := ARRAY['boolean', 'boolean', 'boolean'];
        END CASE;
        v_view_definition := pg_catalog.pg_get_viewdef(v_reference_oids[v_index], true);
        IF v_columns IS DISTINCT FROM v_expected_columns
           OR v_column_types IS DISTINCT FROM v_expected_types THEN
            RAISE EXCEPTION 'aap_labeling_audit_v1.% has an unexpected shape', v_view_name;
        END IF;
        IF pg_catalog.pg_get_viewdef(v_view_oid, true) IS DISTINCT FROM v_view_definition THEN
            IF v_index = 1
               AND (v_has_annotation_ids OR v_has_review_ids OR v_has_review_submitter)
               AND pg_catalog.pg_get_viewdef(v_view_oid, true) IS DISTINCT FROM
                   pg_catalog.pg_get_viewdef(v_legacy_task_reference, true) THEN
                RAISE EXCEPTION 'aap_labeling_audit_v1.% has an unexpected definition',
                    v_view_name;
            END IF;
            IF v_index <> 1
               OR NOT (v_has_annotation_ids OR v_has_review_ids OR v_has_review_submitter) THEN
                RAISE EXCEPTION 'aap_labeling_audit_v1.% has an unexpected definition',
                    v_view_name;
            END IF;
            IF NOT pg_catalog.has_schema_privilege(v_migration_oid, v_schema_oid, 'CREATE')
               AND NOT v_granted_create THEN
                EXECUTE pg_catalog.format(
                    'GRANT CREATE ON SCHEMA aap_labeling_audit_v1 TO %I', v_migration
                );
                v_granted_create := true;
            END IF;
            EXECUTE pg_catalog.format(
                'CREATE OR REPLACE VIEW aap_labeling_audit_v1.task_facts '
                'WITH (security_barrier = true) AS %s',
                v_view_definition
            );
        END IF;
    END LOOP;
    IF v_granted_create THEN
        EXECUTE pg_catalog.format(
            'REVOKE CREATE ON SCHEMA aap_labeling_audit_v1 FROM %I', v_migration
        );
    END IF;

    EXECUTE 'REVOKE ALL ON SCHEMA aap_labeling_audit_v1 FROM PUBLIC';
    EXECUTE pg_catalog.format(
        'REVOKE ALL ON SCHEMA aap_labeling_audit_v1 FROM %I', v_consumer
    );
    FOR v_index IN 1..4 LOOP
        v_source_name := v_source_names[v_index];
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                     ORDER BY attribute.attnum)
        INTO v_all_source_columns
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = CASE v_index
            WHEN 1 THEN v_tasks_oid WHEN 2 THEN v_batches_oid
            WHEN 3 THEN v_annotations_oid ELSE v_locks_oid END
          AND attribute.attnum > 0 AND NOT attribute.attisdropped;
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE public.%I FROM %I', v_source_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE public.%I FROM %I',
            v_all_source_columns, v_source_name, v_consumer
        );
    END LOOP;
    FOR v_index IN 1..5 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('aap_labeling_audit_v1.%I', v_view_name)
        );
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                     ORDER BY attribute.attnum)
        INTO v_view_columns
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_view_oid
          AND attribute.attnum > 0 AND NOT attribute.attisdropped;
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE aap_labeling_audit_v1.%I FROM PUBLIC', v_view_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE aap_labeling_audit_v1.%I FROM PUBLIC',
            v_view_columns, v_view_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE aap_labeling_audit_v1.%I FROM %I',
            v_view_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE aap_labeling_audit_v1.%I FROM %I',
            v_view_columns, v_view_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'GRANT SELECT ON TABLE aap_labeling_audit_v1.%I TO %I',
            v_view_name, v_consumer
        );
    END LOOP;
    EXECUTE pg_catalog.format(
        'GRANT USAGE ON SCHEMA aap_labeling_audit_v1 TO %I', v_consumer
    );

    IF pg_catalog.has_table_privilege(v_consumer_oid, v_tasks_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_tasks_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private public.tasks access';
    END IF;
    IF pg_catalog.has_table_privilege(v_consumer_oid, v_batches_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_batches_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private public.task_batches access';
    END IF;
    IF pg_catalog.has_table_privilege(v_consumer_oid, v_annotations_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_annotations_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private public.annotations access';
    END IF;
    IF pg_catalog.has_table_privilege(v_consumer_oid, v_locks_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_locks_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private public.task_locks access';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(v_consumer_oid, v_schema_oid, 'USAGE')
       OR pg_catalog.has_schema_privilege(v_consumer_oid, v_schema_oid, 'CREATE') THEN
        RAISE EXCEPTION 'Audit consumer has an unexpected Labeling audit schema privilege';
    END IF;
    FOR v_index IN 1..5 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('aap_labeling_audit_v1.%I', v_view_name)
        );
        IF NOT pg_catalog.has_table_privilege(v_consumer_oid, v_view_oid, 'SELECT')
           OR pg_catalog.has_table_privilege(
               v_consumer_oid, v_view_oid,
               'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
           ) OR pg_catalog.has_any_column_privilege(
               v_consumer_oid, v_view_oid, 'INSERT,UPDATE,REFERENCES'
           ) THEN
            RAISE EXCEPTION 'Audit consumer has an unexpected Labeling audit view privilege';
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_class AS relation
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(relation.relacl, pg_catalog.acldefault('r', relation.relowner))
            ) AS privilege
            WHERE relation.oid = v_view_oid AND privilege.grantee = 0
        ) THEN
            RAISE EXCEPTION 'PUBLIC retains access to a Labeling audit relation';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(namespace.nspacl, pg_catalog.acldefault('n', namespace.nspowner))
        ) AS privilege
        WHERE namespace.oid = v_schema_oid AND privilege.grantee = 0
    ) THEN
        RAISE EXCEPTION 'PUBLIC retains access to aap_labeling_audit_v1';
    END IF;
END
$install$;
-- aap-statement-break
DROP VIEW pg_temp._aap_labeling_role_audit_capabilities_ref;
DROP VIEW pg_temp._aap_labeling_role_audit_lock_ref;
DROP VIEW pg_temp._aap_labeling_role_audit_annotation_ref;
DROP VIEW pg_temp._aap_labeling_role_audit_batch_ref;
DROP VIEW pg_temp._aap_labeling_role_audit_task_legacy_ref;
DROP VIEW pg_temp._aap_labeling_role_audit_task_ref;
COMMIT;
