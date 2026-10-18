# ADR-0099: Independent Assets source publication and deletion

- Status: Accepted
- Date: 2026-10-18

## Context

Dataset upload and source deletion had native owner contracts, but the API still owned presigning, object reads/copy, legacy ORM completion and the execution transaction. Moving only a DTO would leave both broad storage credentials and actual source publication in the gateway. Legacy placeholders have no provable upload actor, and a Labeling append receipt cannot independently prove Assets completion.

## Decision

Provide a separately built Assets source runtime for upload initialization/completion and Dataset/Item deletion. Python fits the existing asyncpg, Boto3 and Pillow I/O workload; the frozen JSON and PostgreSQL contracts do not require provider Python objects. Keep public authentication and Dataset write scopes at the gateway, with current active authority and parent/source checks repeated natively. Selected remote failure never falls back locally.

Move source values into a pure public package and the unchanged audit-context type into a shared Audit package. Publish the whole-operation `aap-assets-source/v1` schema as wheel data. Local and remote execution use one framework-free workflow and the same native capabilities. Validate complete outputs and byte budgets before service commit. Public responses retain their existing shapes; upload phases accept caller-saved UUID keys, and the API dispatches media only after a committed success.

Add migration 0213 for legacy locking/completion and immutable Assets receipts. A successful legacy operation atomically updates source metadata, calls Labeling append, updates counters and saves its own result. It never invents a historical uploader or modern intent. Current active accounts retain the existing legacy rule. Missing/duplicate objects do not delete existing rows or bytes. Replays recheck current authority, parent existence and source path/type; changed canonical inputs or replaced sources fail closed. Receipts have no live resource/account foreign keys and no automatic expiry.

Use a separate execute-only LOGIN with exactly nine public Assets functions, no relation/sequence/foreign-schema privileges or role membership. The runtime has its own token and narrow S3 identity: staging Get/Put, historical source Get and new publication-prefix Put, without object deletion, listing or bucket management. Internal object I/O and public signing origins are explicit and separate. The runtime imports no API, ORM, Celery or Redis code.

## Consequences

A modern pending attempt holds current identity, Dataset and intent locks across HEAD, bounded conditional image reading and conditional server-side copy. This preserves the established write fences. Object replacement or removal after HEAD rejects the attempt instead of accepting stale metadata; permission/network failures remain failures rather than missing dispositions. SQL failure rolls back metadata and tasks but cannot undo an uncertain external copy. Fresh publication generations isolate retries; orphan source copies are deliberately retained until a separate durable reclamation design exists.

The API still performs post-commit media dispatch. Import/scan/connector workflows, derived media, complete Blob contracts and other module runtimes remain work toward the full goal. This slice still requires shared PostgreSQL and does not establish full Assets deployment, production capacity or a complete language replacement. Go remains an isolated experiment and is not deployed by this decision.
