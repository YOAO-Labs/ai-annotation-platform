# T04 — Frozen training data insights and version comparison

> Status: completed / verified
> Authorization: user requested sequential review and implementation, one commit per node.
> Depends on: existing immutable training datasets
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)
> Release milestone: not assigned.

## Fresh audit and chosen outcome

Baseline is T03 commit `cd634ab4c`; database head is `0179`. Preserve `.orca/`, the two original manual-acceptance runs, their data version and the retained owned development services. Independent read-only audits confirmed the authoritative inputs and two required extensions.

The existing snapshot freezes annotations, class order and eligibility. The manifest identifies actual packaged members, dimensions, labels/media SHA, splits and cumulative ancestry. Automatic candidates deduplicate package members, so statistics must join manifest members to frozen annotations rather than count the entire snapshot. Original source paths, current tasks/annotations and the media materialization writer are never analysis inputs.

Preparation exclusions currently exist only in the temporary preflight report. New manual and automatic versions will store a separate, versioned frozen preparation report. Existing versions explicitly report that historical exclusions were not retained. Adding the report does not change snapshot digests, package bytes, eligibility or automatic growth semantics.

Managers receive bounded class/split distributions, immutable sample drill-down and directional version comparison. A derived database sidecar provides indexed filtering and pagination. The minimal alternative scans every frozen JSON document on each request; the sidecar avoids repeated large reads and permits sample pages without loading annotations or source images. This is a large change across more than eight files and four existing components, with no new service, dependency, environment variable or credential.

## Settled contracts

- Add nullable `preparation_report` to data versions and two derived tables: one analysis per version with schema/input identity and summary, and indexed sample rows with frozen labels/metadata. Both tables cascade on dataset deletion and never block ordinary deletion. Cache identity includes analysis schema, snapshot digest and manifest content digest. Cache creation serializes on the version row and rechecks existence/identity; stale schema/input caches are rebuilt atomically.
- Derive actual packaged image/instance counts by train/validation split, per-class image frequency versus instance frequency, unique/duplicate contents, explicitly confirmed negatives, logical dimensions and normalized box-area bins. These are descriptions, not a universal quality score. Missing retained metadata yields partial/unavailable analysis with exact known/unknown counts; advisory failure never changes ready status or package download.
- Regenerate detection labels from frozen annotations with the existing bridge and verify their hash against the manifest before exposing normalized labels. Semantic label identity uses class names and the native rounded coordinates, excluding annotation/task IDs. Reordering classes is reported separately from actual label corrections; names have no durable class UUID, so added/removed names do not prove a rename.
- Compare content SHA bidirectionally: additions/removals, label corrections, split changes, multiplicity changes, task-ID churn and reintroduced ancestry contents. Return explicit partial/unknown state where evidence is missing. Reuse existing semantic partitions without changing them. Expose both their deduplicated validation identity and a new evaluation multiset identity retaining duplicate counts plus ordered classes. Neither replaces the existing training manifest/run digest.
- Six manager/scope-protected GET operations under `/projects/{project_id}/training-datasets/{version_id}/insights`: overview, paginated classes, filtered/paginated samples, one sample's paginated normalized labels, authenticated immutable sample media and directional comparison with an authorized `baseline_id`. Read both versions inside the same project. Pages default to 25 samples (maximum 100), with stable ordering and filtered totals. Removed-sample rows explicitly carry the baseline version identity.
- Sample media derives its key from project/version/member identity. Verify frozen snapshot metadata, expected size and actual byte SHA before serving a bounded temporary-file stream. Missing/corrupt media never falls back to a mutable source or another member. Each read rechecks current authority and scopes; no presigned URL is cached. Media observations carry their check time and exact verified/unavailable/unchecked counts; a static metadata report never promises current media availability.
- Extend the existing data-version detail area with overview/classes, sample filters/preview and baseline comparison. Persist only selected version, baseline and view choices in the URL; independently paginate eligible baseline choices and fetch a selected version outside the current list page. Show clear preparation blockers versus advisory observations. Keep existing create/download/build/error behavior.
- Bind new queries/previews to account, session, project, version and filters. Abort obsolete requests and discard late blobs, revoke object URLs on replacement/close, preserve keyboard focus and semantic app theme tokens. Include desktop and 390px layouts, both themes, refresh/back and error/empty states.

```text
Frozen selection -> separate preparation report
Snapshot + package manifest -> versioned analysis -> indexed sample/class pages
                           -> semantic + multiset comparison -> filtered drill-down
Version member + retained bytes -> authenticated SHA-checked media + frozen labels
```

## Exact implementation surface

- `apps/api/app/db/models/training_dataset.py`, new `db/models/training_dataset_analysis.py`, model registration and migration `apps/api/alembic/versions/0180_training_dataset_analysis.py`.
- `app/services/training_datasets.py`, `app/services/learning_policies.py`, `app/api/v1/training_datasets.py`: retain preparation reports in both creation paths without changing frozen selection or digests.
- New `app/services/training_dataset_analysis.py`, `app/schemas/training_dataset_analysis.py`, `app/api/v1/training_dataset_analysis.py`, with router registration. Reuse bridge hashing/label/partition helpers and storage client.
- `apps/web/src/api/client.ts`, `trainingDatasets.ts`, new `pages/Projects/TrainingDatasetInsights.tsx` and `TrainingDatasetSamplePreview.tsx`, integration in `ProjectLearningPage.tsx`.
- New API/analysis and frontend insights/preview regressions; extend existing dataset/policy/client/page tests for report persistence and transport/URL integration.
- OpenAPI snapshot, regenerated ignored web client, SDK API coverage manifest and contract validation, generated API route index.
- README, CHANGELOG, the current user/API/architecture training-data guides, this promoted plan and backlog/dependency links.

## Verification and runtime

Use checkout virtual environments and a verified disposable test mode:

```sh
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_training_dataset_analysis.py tests/test_training_datasets.py tests/test_training_dataset_bridge.py tests/test_learning_policies.py -q'
pnpm --filter @anno/web exec vitest run src/pages/Projects/TrainingDatasetInsights.test.tsx src/pages/Projects/TrainingDatasetSamplePreview.test.tsx src/pages/Projects/ProjectLearningPage.test.tsx src/api/client.test.ts
pnpm --filter @anno/web typecheck
pnpm --filter @anno/web lint
pnpm --filter @anno/web lint:css-tokens
pnpm lint:python
pnpm openapi:export
pnpm codegen
pnpm openapi:check
pnpm docs:build
git diff --check
```

Cover task-ID churn/duplicates, label-only corrections, class order/name changes, validation multiplicity, split migration, removed/reintroduced ancestors, explicit negatives, automatic deduplication, unrecorded historical exclusions, missing/corrupt media despite a live mutable source, scope/project denial, stale caches, stable filtered pagination, large metadata fixtures and late account/version/filter responses. Verify preparation report persistence for manual and automatic candidates and unchanged existing digests/growth tests.

Apply the additive migration only in the confirmed owned development database, after checking no active training/build work. Refresh the owned ordinary and maintenance workers because model/policy code changes; retain private training/inference/Redis overrides and verify registered tasks and health. Native GPU runner inputs are unchanged; no GPU qualification workload is needed for analysis. Live browser acceptance uses bounded task-owned frozen data versions for class/sample/comparison navigation, then removes those versions, jobs, observations, browser/vault, screenshots, reports, caches and disposable test resources.

## Acceptance and rollback

Acceptance requires complete analysis/drill-down/comparison behavior, verified immutable media and frozen labels, passing focused regressions and checks, live browser evidence, current documentation and one normal-hook commit. Rollback removes analysis entry points and derived caches while preserving data versions, preparation reports, original snapshots/packages, split history and automatic-policy baselines. Missing historical evidence remains explicitly unavailable.

## Delivered evidence

- Added migration `0180`, frozen preparation records for manual/automatic candidates, versioned derived statistics and indexed sample rows, six authenticated insight operations, compact version reads, and the project data-version views with URL restoration.
- Focused dataset/bridge/policy/analysis regression suite: 54 passed. A later media/preview check passed 4 cases, including two new independent-session deletion races. The unique backend coverage is 56 cases. The race checks pause storage, verify the real delete transaction waits via `pg_blocking_pids`, then confirm the intended success or missing-media response and cascade cleanup.
- Frontend regression suite: 26 passed across insights (7), preview (5), project integration (9) and client transport (5). The insights cases passed again after adding explicit keyboard-focus restoration. Full web typecheck and lint passed; lint retained five unrelated existing warnings. Python lint/format, CSS tokens, OpenAPI export/check, regenerated web types, SDK contract checks (4), Prettier and `git diff --check` passed. Documentation build completed successfully.
- Live acceptance used the retained owned development API/Web and refreshed ordinary/maintenance workers. Both registered current learning tasks; an owned policy-check dispatch completed and its result was removed. The additive migration is applied in the owned development database. No GPU workload was required or launched for analysis.
- Browser acceptance covered 1280/1440 px desktop and 390×844 mobile layouts, both themes, class/split drill-down, selected-baseline refresh, directional removal counts, baseline-member media URLs, actual frozen images/boxes, empty exclusions and real missing-media behavior. Escape returns focus to the opening preview button. No horizontal page overflow or JavaScript errors were observed. WCAG A/AA scans after the small-text contrast fix found no violations, with one unresolved gradient-related check on the existing app header; this is not accessibility certification.
- Two bounded fixture versions (six/four members) were built through public APIs, compared and deleted through public APIs. Their storage prefixes, two jobs, two notifications and Celery result keys were removed. The original version's task-created derived cache, test modes, temporary reports/screenshots/files and browser/vault were removed. The original data version, two successful examples, three unread notifications, dark preference and owned manual services remain available.

Historical missing metadata is reported as unknown. Frozen SHA verification is per read; media observation timestamps describe past checks and do not promise continued object availability. Runtime training digests, automatic growth semantics and native runner inputs remain unchanged.

## Outcome

- Delivered scope: Delivered frozen preparation records, versioned insights, sample inspection and compatible version comparison without changing training identities.
- Completion evidence: the existing `Delivered evidence` section above records implementation and verification. Archival on 2026-10-22 reuses that record; application tests and live acceptance were not rerun for this documentation move.
- Enduring documentation: [data-versions.md](../../../docs-site/user-guide/online-learning/data-versions.md), [training-datasets.md](../../../docs-site/api/guides/training-datasets.md); user impact is recorded in `CHANGELOG.md`.
- Release milestone: no new milestone or version is assigned by this archival.
- Remaining boundaries: preserve the exclusions and qualification limits recorded above; new task, inference or deployment scope requires its own plan and acceptance.
