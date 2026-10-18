# ADR-0100: Durable connector import execution

- Status: Accepted
- Date: 2026-10-18

## Context

Connection imports were accepted into Jobs before a Celery publish. A broker failure could strand pending work; a worker crash could strand running work. Per-file results and counters lived in process memory. The importer wrote files under mutable dataset/name keys and deleted a just-written key after content deduplication. Concurrent imports could therefore affect each other's bytes. Immediate cancellation also released the source-deletion guard before an in-flight worker necessarily stopped.

## Decision

Run the actual S3/SFTP importer as an independent process in the Assets source artifact. Python reuses the established connector libraries; the public JSON and PostgreSQL contracts remain usable by another language. The process imports no API, ORM, Celery or Redis implementation and uses its own execute-only database identity and destination object credentials. API authentication, Dataset write scope and the accepted settings snapshot remain at the public adapter.

Accept an immutable Assets operation and its Jobs row atomically, using the caller's UUID as the job ID. Keep current active manager and connection visibility checks on acceptance and replay. Accepted execution retains its actor authority, while explicit cancellation, current Job status, Dataset existence, connector version and an unexpired lease fence every publication. A connector configuration or credential change stops the operation rather than mixing versions. Host allowlists are checked again near external I/O; an explicit empty database override denies access.

Persist the complete bounded source manifest before copying. Keep stable entry IDs, a committed cursor and counters. Stream each object into a distinct publication path under `dataset-sources/`; read S3 conditionally and check SFTP source identity and confinement. Item registration, Dataset count, Labeling append receipt, import progress and follow-up delivery intent share one SQL transaction. Retrying an uncertain commit cannot register or count an entry twice. Persistence contention retains the entry for retry rather than becoming a failed file.

Keep the existing media executor. A bounded Assets outbox makes the handoff to it durable; the API maintenance relay acknowledges only after broker acceptance. Terminal notifications are persisted through their owner capability and relayed through the existing notification publisher. This is at-least-once delivery, with possible duplicate broker messages; it does not certify media execution completion.

## Consequences

The importer can start after the API accepts work, reclaim an expired lease and resume committed progress without Celery. Cancellation preserves completed files and prevents a copied late result from creating rows after cancellation or source deletion. Pending and running imports continue to block source deletion. Migration refuses unproven active legacy imports instead of adopting them automatically.

Original bytes, including duplicate or uncertain unreferenced copies, are retained. Reclamation needs a separate durable evidence and late-I/O design. Raw object operations cannot be rolled back by PostgreSQL. A blocked backend call may finish after cancellation, but its publication UUID and SQL fences prevent it from becoming a current Item.

This slice does not move ZIP upload, bucket scanning, connector management, derived media execution or the remaining modules. Shared PostgreSQL and the aggregate API's transitional privileges remain. The independently runnable importer is qualified only in owned local resources; this decision does not deploy it to production or change the isolated Go experiment.
