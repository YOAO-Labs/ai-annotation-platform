# Audit Node conformance

The Node peers read the checked-in append, query, event-read, and daily-projection
JSON Schemas. They call the real `aap_audit_v1.append_events(jsonb)` and
`query_events(jsonb)` owner functions, and exercise the read-only
`aap_audit_v1.audit_events` and `audit_daily_facts` relations using explicitly
supplied disposable database URLs. PostgreSQL `bigint` IDs and aggregate counts
remain decimal strings in Node; do not convert them to JavaScript `Number`.
PostgreSQL `inet` remains native in the event relation and uses the PostgreSQL
driver's text representation in Node.
The relation's physical row key is `(id, created_at)`, so do not deduplicate
its rows by `id` alone.

Run `pnpm install --frozen-lockfile --ignore-workspace --ignore-scripts` and
`pnpm check` from this package directory for the local contract self-checks.
The live checks run by the owned module-data verifier against its
disposable PostgreSQL database and separate append, query, and read-only client
roles. The read-only LOGIN can select `audit_events` and `audit_daily_facts`; it
cannot read `public.audit_logs` or `mv_audit_bi_daily`, call the append/query
functions, or execute the refresh capability.
