# Assets media

This package defines the language-neutral `aap-assets-media/v1` boundary for
durable video metadata/timetables, playback/posters, frames/chunks and their
reads, retries, backfill and cleanup. The contract imports only Pydantic and the
standard library. Native installation belongs to migration 0219; its schema is
identical to the package data file.

The control and execution processes use separate function-only SQL logins and
S3 identities. Execution authority is persisted in Assets, with source generation,
attempt leases, bounded timetable staging and immutable output prefixes. The
worker does not depend on the API, ORM, Celery or Redis. Raw source deletion,
Labeling state, image-pyramid ownership and general storage administration keep
their existing owners.

Implementation and local qualification are in progress. This package alone does
not establish a runnable media deployment or whole-module completion. The exact
state, consumer migration and remaining work are tracked in the
[backend module plan](../../docs/plans/1792160873_backend-module-decoupling.md)
and [ADR-0105](../../docs/adr/0105-independent-video-media-owner.md).
