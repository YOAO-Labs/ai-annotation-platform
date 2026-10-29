---
name: aap-dataset-storage
description: Change or diagnose dataset sources, resumable scans and sync, immutable content identity, saved storage targets, original reads, or retention and migration in this project.
---

# Dataset and storage ownership

Start from the affected operation's current owner, not a file path or bucket name. Read [dataset sources and sync](../../../docs-site/dev/concepts/dataset-sources-and-sync.md) for observation/acceptance, [original access and retention](../../../docs-site/dev/concepts/original-access-and-retention.md) for physical I/O, or [storage migration](../../../docs-site/dev/concepts/storage-migration-and-ops.md) for relocation. Historical branch contracts are not evidence that a feature exists in this checkout.

Keep the requested storage acceptance slice explicit. Correct broken reads, permission/content guarantees, and regressions caused by this change; adjacent Tracker, review, session, training-scale, or migration work is not automatically authorized. Reuse completed acceptance evidence unless changed inputs or a concrete failure invalidate it.

## Content and location

- Keep directory observations, accepted source revisions, verified content, business references, and physical copies distinct. A successful scan, `stat`, HEAD, ETag, or legacy hash hint does not establish SHA-256 content identity. Mapping backfill is not byte verification.
- Resolve Task originals through `original_sources`: a missing direct Item can still have a `primary_lidar` link. Use the canonical owner and saved location; do not infer an independent Task original or try current purpose buckets until one works.
- A saved `storage_ref`/target records allocation-time ownership; a purpose-binding change affects new allocations, not existing objects. Keep historical creation receipts separate from current locators. Unknown legacy ownership requires the existing explicit adoption/verification workflow, not a GET-time repair.
- Preserve exact object keys and version facts. Do not normalize S3 keys as filesystem paths, decode them twice, or replace a missing version with a guessed current object. Shared SHA does not establish shared ownership or permission to delete.

## Transactions and physical work

- Reuse `managed_original_creation`, `managed_original_reader`, `storage_access`, and the operation's domain owner. Keep admission, physical byte verification, publication, and retirement responsibilities together; do not add a second reader or uploader to bypass a gate.
- Follow each owner's actual lock order and I/O boundary. General reads release business locks before external I/O and recheck current authority/identity afterward; some reference annotation writes intentionally keep their original transaction during a bounded read. A blanket rule to always release locks breaks that contract.
- Cancellation or a timeout is not proof that a native thread, request body, client, or subprocess stopped. Drain physical work before closing its stage or releasing its permit; retain unsettled responsibility when closure cannot be proved.
- Preserve conditional creation and exact attempt ownership. An ambiguous PUT/commit response must not trigger deletion of a possibly published object. Do not use a later HEAD, 404, 412, or TTL to invent a successful creation receipt.
- Metadata/usage/diagnostic GETs must retain their declared read-only and bounded-query behavior. Do not probe providers, mutate credentials, or schedule repairs merely to render a list.

## Resumable domain work

Keep domain ledgers independent of transient AsyncJobs. Claims, progress, cancellation, failure signals, resume, and publication must compare the current job/task ID, generation, token/lease, and current authorization. An old worker or signal must not mutate a replacement attempt. Use the existing source-access relationship checks rather than authorizing from a payload's `source_id` or submitter alone.

## Verification

Choose the affected existing tests under `apps/api/tests/test_dataset_*`, `test_storage_*`, or the relevant original-reader family. Exercise the actual operation against a disposable environment, including the changed uncertainty: interrupted/resumed attempts, source or permission drift during I/O, purpose switching with old objects, ambiguous publication, or exact-version cleanup. Do not run a broad storage migration or destructive cleanup as a generic smoke test. Use [aap-runtime](../aap-runtime/SKILL.md) for environment ownership and worker refresh; use [aap-online-learning](../aap-online-learning/SKILL.md) when freezing data into training versions.
