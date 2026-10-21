# Labeling

This package owns the private contract and query manager for Project class-usage
reads. The base package needs only Pydantic. Optional PostgreSQL and HTTP runtime
bindings use a neutral function-only credential verifier; they do not depend on
the platform API, Workspace, Identity, an ORM, queues, object storage or ML.

The trusted API peer checks current Workspace management authority before calling
the Labeling projection. This runtime does not accept public credentials or an
`is_manager` flag. Independent providers use read-only READ COMMITTED transactions;
the default API adapter borrows its caller Session without owning commit.

`get_project_class_usage` first reads class counts, then reads every active
annotation's stored attributes JSON text. Class counts are grouped by the
annotation's project ID and include active rows regardless of cancellation,
annotation source, tool unit, or Task project. Attribute counts include only
top-level keys from JSON objects; scalar JSON values are still fully decoded.

The strict private request/reply schema is
`aap-labeling-class-query/v1`. Its schema file is distributed beside the Python
package.

The reply echoes request and Project identity. Count maps reject duplicate keys,
extra fields, booleans and nonpositive integers; map order and JSON whitespace are
not part of the contract. Late attribute decoding failure remains an internal500,
including the frozen legacy large-integer case, without returning partial counts.

Run `uv run --project apps/labeling --extra runtime python -m aap_labeling` with
`LABELING_CLASS_QUERY_DATABASE_URL(_FILE)` and
`LABELING_CLASS_QUERY_CONTROL_TOKEN(_FILE)`. It listens on port8178 by default.
The private route is `POST /v1/labeling/class-queries/execute`; `/ready` verifies
the policy and the exact three-function credential. The standalone Compose file
uses two read-only secret files and binds only localhost.

Use `uv run --project apps/labeling --extra runtime --extra test pytest
apps/labeling/tests` for the package suite. Native permissions, coordinated API
replay, installed peers, images and restore remain separate acceptance gates.
See [ADR-0142](../../docs/adr/0142-labeling-class-usage-query-owner.md) for their
current status and limits.
