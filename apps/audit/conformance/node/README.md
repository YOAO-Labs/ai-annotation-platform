# Audit Node conformance

The Node peers read the checked-in append, query, and read-relation JSON
Schemas. They call the real `aap_audit_v1.append_events(jsonb)` and
`query_events(jsonb)` owner functions, and exercise the read-only
`aap_audit_v1.audit_events` relation using explicitly supplied disposable
database URLs. PostgreSQL `bigint` IDs remain decimal strings in Node; do not
convert them to JavaScript `Number`. PostgreSQL `inet` remains native in the
relation and uses the PostgreSQL driver's text representation in Node.
The relation's physical row key is `(id, created_at)`, so do not deduplicate
its rows by `id` alone.

Run `pnpm install --frozen-lockfile` and `pnpm check` for the local contract
self-checks. The live checks run by the owned module-data verifier against its
disposable PostgreSQL database and separate append, query, and read-only client
roles. The read-only LOGIN can select only `audit_events`; it cannot read
`public.audit_logs` or call the append/query functions.
