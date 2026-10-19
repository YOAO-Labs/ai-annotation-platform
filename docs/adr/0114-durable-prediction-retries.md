# 0114 — Durable failed-prediction retries and running tracker roots

- Status: Accepted; isolated local qualification in progress
- Date: 2026-10-20
- Deciders: Maintainers
- Extends: ADR-0112 and ADR-0113

## Context

An accepted retry previously depended on a boolean claim and one broker delivery.
Repeated deliveries created new Jobs, lost inference output and could charge
failure counters for persistence contention. Ordinary batch result checkpoints
did not cover this separate worker. Running tracker Masks also existed only in
worker memory between producer grace and final result staging.

## Decision

Labeling owns an immutable retry attempt with original failure, actor, Task,
backend, parameters, retry-count baseline and pre-model write scope. Jobs owns
its durable delivery outbox and stable Job UUID. Native admission combines them
with current Workspace manager authority in one caller transaction. Publication
occurs after commit; an uncertain reply retains the outbox and republishes the
same UUID. Missing retained input can leave a visible pending Job, without a
started notification, model call or failure charge.

The six-operation `aap-prediction-retries/v1` protocol uses the existing result
package and standard PostgreSQL transport. It adds no application, ORM, queue or
object-store dependency to the installed executor. Its read and two delivery
capabilities join the fourteen ordinary executor capabilities. The function-only
LOGIN receives no business tables, role memberships, DDL or TEMP privileges.
`PREANNOTATION_RESULTS_DATABASE_URL` or its secret file selects that connection
for retry reads, checkpoints and immediate/periodic publication. Final writes
remain on the Labeling provider's caller-owned transaction.

Exactly one model result must match the original Task. Raw output is saved
before post-model source checks and Mask production. Complete raw output,
prepared payloads, stable Prediction identities and Mask roots reuse the
existing chunked result store. Reentry skips completed inference; three quick
persistence attempts may defer indefinitely without charging a model failure.
This does not promise exactly-once computation before raw capture completes.

Successful finalization proves the actual Prediction and Meta, deletes the exact
original failure and its Meta, and commits the receipt, member progress, attempt
outcome and Job completion atomically. Model or Mask-data failure increments the
original counter once. Source, authority, dismissal or Job invalidation abandons
only the current attempt and releases its claim and pending roots without a
model-failure charge. Both paths require the current execution lease. Historical
outcomes survive ordinary Job cleanup. Unknown commit acknowledgements resolve
through those outcomes and receipts.

Celery uses late acknowledgement and rejection after child loss. Broker failure
signals cannot terminalize a bound retry; explicit cancellation still can.
Legacy deliveries use their broker UUID, or a deterministic UUID for an older
non-UUID broker identity. Migration releases unbound legacy boolean claims so
lost early-ack messages cannot permanently disable manual recovery; it does not
reset counters or start inference. Reclamation refuses active legacy retry Jobs
and requires an explicit migration acknowledgement after stopping/draining old
consumers and queues. It is a cold upgrade, not a mixed-writer rolling deployment.
A late delivery handled by the new worker must pass admission and cannot replace
a current attempt. Historical Jobs without native attempts retain ordinary
terminal cleanup behavior.

Running tracker Masks register a temporary root for the exact running Job and
Task before committing producer facts. Terminal staging transfers protection in
the same transaction; other terminal states release the temporary roots. Job
lock contention retries the held RLE. After rollback, relinking checks the
original byte epoch; an intervening collection requires publishing the held RLE
again under the new epoch. The GC LOGIN cannot register these producer roots.

## Consequences

Retry admission and result recovery are language-neutral data capabilities.
Snapshots and outbox records add database storage and traffic, and their lifetime
follows definitive business outcomes rather than a wall-clock timeout.

This change does not complete all Labeling, Inference or Jobs operations, provide
full tracker computation resumption, or qualify production capacity. The primary
development runtime, collector cutover guard and isolated Go experiment remain
under their existing deployment boundaries.
