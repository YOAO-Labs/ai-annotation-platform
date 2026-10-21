# Owner data client utilities

This neutral Python leaf verifies function-only PostgreSQL credentials. It has no
business owner, transport, ORM or database-driver dependency. Owner clients supply
their schema and exact function signatures; the verifier rejects extra data,
schema, role, database and function privileges. Each caller retains its existing
connection, transaction and error handling.
