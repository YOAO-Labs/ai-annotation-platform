# Audit Node conformance

The Node peers read the checked-in append and query JSON Schemas and call the
real `aap_audit_v1.append_events(jsonb)` and `query_events(jsonb)` owner
functions with explicitly supplied disposable database URLs. PostgreSQL
`bigint` IDs remain decimal strings in both protocols; consumers must not
convert them to JavaScript `Number`.

Run `pnpm install --frozen-lockfile` and `pnpm check` for the local contract
self-checks. The live checks run by the owned module-data verifier against its
disposable PostgreSQL database and separate function-only client roles.
