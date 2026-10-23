# Audit append Node conformance

The Node client reads the checked-in JSON Schema and can call the real
`aap_audit_v1.append_events(jsonb)` owner function with an explicitly supplied
disposable database URL. PostgreSQL `bigint` IDs remain canonical positive
signed-64-bit strings in `pg` rows and the JSON receipt; consumers must not
convert them to JavaScript `Number`.

Run `pnpm install --frozen-lockfile` and `pnpm check` for the local contract
self-check. The live check is run by the owned module-data verifier against its
disposable PostgreSQL database and function-only client role.
