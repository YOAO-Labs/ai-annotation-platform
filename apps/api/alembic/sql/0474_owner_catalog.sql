-- Names and definitions, not installation-specific OIDs or ACL grantee names.
-- The caller fixes search_path to pg_catalog, pg_temp before this inventory.
WITH namespaces AS MATERIALIZED (
    SELECT oid, nspname FROM pg_namespace WHERE nspname ~ '^aap_'
), routines AS MATERIALIZED (
    SELECT p.*, n.nspname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.oid IN (SELECT oid FROM namespaces)
       OR (n.nspname NOT IN ('pg_catalog', 'information_schema') AND
           (p.prosrc ~ :tokens OR array_to_string(p.proconfig, ',') ~ :tokens))
), objects AS MATERIALIZED (
    SELECT 'pg_namespace'::regclass AS classid, oid, nspname FROM namespaces
    UNION ALL SELECT 'pg_proc'::regclass, oid, nspname FROM routines
    UNION ALL SELECT 'pg_class'::regclass, c.oid, n.nspname FROM pg_class c
              JOIN namespaces n ON n.oid = c.relnamespace
    UNION ALL SELECT 'pg_type'::regclass, t.oid, n.nspname FROM pg_type t
              JOIN namespaces n ON n.oid = t.typnamespace
), entries AS (
    SELECT nspname, 'schema' AS kind, nspname::text AS identity, '' AS definition FROM namespaces
    UNION ALL
    SELECT nspname, 'routine', oid::regprocedure::text, pg_get_functiondef(oid) FROM routines
    UNION ALL
    SELECT n.nspname, 'relation', c.oid::regclass::text,
           json_build_array(c.relkind, c.relpersistence, c.reloptions, c.relrowsecurity,
               c.relforcerowsecurity, CASE WHEN c.relkind IN ('v','m') THEN pg_get_viewdef(c.oid)
                   WHEN c.relkind IN ('i','I') THEN pg_get_indexdef(c.oid) END)::text
    FROM pg_class c JOIN namespaces n ON n.oid = c.relnamespace
    UNION ALL
    SELECT n.nspname, 'column', c.oid::regclass::text || '.' || quote_ident(a.attname),
           json_build_array(a.attnum, format_type(a.atttypid,a.atttypmod), a.attnotnull,
               a.attidentity, a.attgenerated, pg_get_expr(d.adbin, d.adrelid))::text
    FROM pg_class c JOIN namespaces n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
    UNION ALL
    SELECT n.nspname, 'constraint', c.conrelid::regclass::text || '.' || quote_ident(c.conname),
           pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN namespaces n ON n.oid = c.connamespace
    UNION ALL
    SELECT o.nspname, 'dependent', CASE WHEN d.classid='pg_class'::regclass AND d.deptype='i'
               AND o.classid='pg_class'::regclass AND EXISTS (
                   SELECT FROM pg_class parent WHERE parent.oid=o.oid AND parent.reltoastrelid=d.objid
               ) THEN 'internal TOAST storage' ELSE (pg_identify_object(d.classid,d.objid,d.objsubid)).identity END,
           json_build_array(d.classid::regclass::text, d.deptype, o.classid::regclass::text,
               (pg_identify_object(o.classid,o.oid,d.refobjsubid)).identity,
               CASE WHEN d.classid='pg_trigger'::regclass THEN pg_get_triggerdef(d.objid)
                    WHEN d.classid='pg_rewrite'::regclass THEN pg_get_ruledef(d.objid) END)::text
    FROM objects o JOIN pg_depend d ON d.refclassid=o.classid AND d.refobjid=o.oid
    WHERE NOT EXISTS (SELECT FROM pg_trigger t WHERE d.classid='pg_trigger'::regclass
                      AND t.oid=d.objid AND t.tgisinternal)
      AND NOT EXISTS (
          SELECT FROM pg_trigger t
          JOIN pg_trigger parent ON parent.oid=t.tgparentid
          JOIN pg_class child ON child.oid=t.tgrelid
          JOIN pg_inherits i ON i.inhrelid=child.oid AND i.inhparent=parent.tgrelid
          WHERE d.classid='pg_trigger'::regclass AND d.objid=t.oid
            AND o.classid='pg_proc'::regclass AND o.oid=t.tgfoid
            AND parent.tgrelid='public.predictions'::regclass
            AND parent.tgname='trg_mask_gc_roots_predictions'
            AND parent.tgfoid='aap_labeling_v1._mask_gc_sync_root_trigger()'::regprocedure
            AND parent.tgenabled='O' AND t.tgenabled='O'
            AND child.relispartition AND child.relnamespace='public'::regnamespace
            AND child.relname ~ '^predictions_y[0-9]{4}m(0[1-9]|1[0-2])$'
            AND pg_get_triggerdef(t.oid)=replace(pg_get_triggerdef(parent.oid),
                ' ON public.predictions FOR EACH ROW ',
                format(' ON %s FOR EACH ROW ', child.oid::regclass))
      )
)
SELECT nspname, kind, identity, definition FROM entries
ORDER BY nspname COLLATE "C", kind COLLATE "C", identity COLLATE "C", definition COLLATE "C"
