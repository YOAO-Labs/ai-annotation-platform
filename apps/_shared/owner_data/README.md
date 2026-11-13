# Owner data client utilities

This neutral Python leaf verifies function-only PostgreSQL credentials. It has no
business owner, transport, ORM or database-driver dependency. Owner clients supply
their schema and exact function signatures; the verifier rejects extra data,
schema, role, database and function privileges. Each caller retains its existing
connection, transaction and error handling.

The function inventory recognizes both module namespace prefixes, legacy `aap_`
and current `yoao_`. A PUBLIC SECURITY INVOKER capability in either namespace is
therefore part of the compared set and cannot escape the exact-inventory check.

The role name defaults to the schema-derived owner client using the legacy `aap_`
stripping rule; that derivation is unchanged. Callers on the `yoao_` namespace
supply their intended role explicitly. A caller may also supply an explicit role
when multiple function-only profiles share a schema, such as
`video_execution_client` in `aap_jobs_v1`. The exact function inventory and all
other privilege checks remain mandatory for that profile.
