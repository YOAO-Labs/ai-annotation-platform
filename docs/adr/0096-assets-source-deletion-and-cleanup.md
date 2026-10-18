# 0096 — Assets Source Deletion and Independent Cleanup

- **Status:** Accepted
- **Date:** 2026-10-18
- **Supersedes:** The Celery cleanup execution adapter in ADR 0095

## Context

Dataset and DatasetItem deletion previously mixed ORM cascades, incomplete
reference checks, and best-effort S3 cleanup. Upload initialization also exposed
a placeholder Item before its source existed. Failure cleanup could remove a
pre-existing source under a deterministic name. These paths prevented safe
ownership transfer and independent execution.

## Decision

Assets owns versioned Dataset/Item deletion commands and immutable receipts.
Fresh global catalog-manager authorization, ordered source locks, Learning and
Labeling retention guards, Jobs checks/cancellation, attribution detach, exact
pyramid outbox rows, metadata deletion, Audit and receipts share the caller's
PostgreSQL transaction. A retained reference returns a conflict. Original bytes
are always retained. Receipt replay requires the original active actor and
canonical request; public REST still checks its current manager admission.

Dataset `FOR UPDATE` fences uncooperative FK writers. Scene/item/association,
upload-intent, pyramid Asset and generation locks precede destructive cascades.
JSON-scoped import acceptance takes the Dataset parent fence; quality-run
acceptance takes the native Scene reference fence. Item deletion blocks active
authorized creation operations; whole-Dataset deletion revokes exact current
association generations. No legacy job scope or upload actor is synthesized.

Upload reservation stores an intent without a ready Item. The client PUTs only
to import staging. Completion locks current intent ownership and the Dataset,
assigns a unique publication path, conditionally copies the captured ETag, and
calls one native command that accepts the Item, appends through Labeling and
freezes the result. Completed retries never copy again. Each pending retry gets
a different path so a cancelled attempt's late copy cannot overwrite it.
Legacy placeholders retain non-destructive completion compatibility.

The independent Python `apps/assets-cleanup` process replaces the Celery cleanup
adapter. It uses asyncpg and boto3, with no API, ORM, Redis or Celery dependency.
Its LOGIN can execute only bucket-filtered keyset pending reads and completion;
its S3 identity can list `image-pyramids/*` and delete within that prefix in one
bucket. It commits pending reads before object I/O and acknowledges only after
successful full-prefix deletion. Failed deletes or acknowledgements remain
retryable; keyset paging prevents an earlier failed entry starving later work.

Outbox `not_before` captures the latest building-generation lease. Both pending
reads and acknowledgements reject premature completion. Writer configuration
requires the lease to outlast its Celery hard time limit. Original deployed SQL
is unchanged; additive migrations replace producers/completion and downgrade
restores the original definitions.

Cutover installs migrations/grants and the scoped S3 identity before starting a
ready new consumer and refreshing the API/old workers and beat. Operational
rollback first stops producers/builders, waits out captured leases and drains
the outbox, then stops the new consumer before reverting schema/code. Empty
disposable migration rollback is not proof that active work can be discarded.

## Consequences and limits

Source bytes and SQL cannot commit atomically. Failed or uncertain publication
attempts can leave unpublished source copies; those bytes and pending intents
currently remain retained. Import staging retains its lifecycle expiry. A future
source reclamation capability needs durable publication evidence and a late-I/O
fence before receiving source-delete credentials.

These are selected stateful Assets operations and one independent executor.
The catalog's other CRUD/import/media operations and other business modules
still need complete protocols, ownership, credentials and runtime artifacts.
The SQL boundary still shares PostgreSQL and the caller's connection; it is not
a multi-database transaction. Local acceptance does not establish production
capacity, arbitrary object-store completion timing or language replacements for
the complete Assets module. The Go monthly-report experiment remains separate.
