# T04 — Frozen training data insights and version comparison

> Status: research-draft
> Priority: next; independent of parameter UI
> Depends on: existing immutable training datasets
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and existing owners

Managers can explain which data changed, why a version excludes samples, and whether a score comparison uses the same validation scope. Analysis must describe frozen data, not mutable current annotations.

Reuse [dataset models](../../../apps/api/app/db/models/training_dataset.py), [snapshot/materialization service](../../../apps/api/app/services/training_datasets.py), [bridge and split history](../../../apps/api/app/services/training_dataset_bridge.py), [dataset API](../../../apps/api/app/api/v1/training_datasets.py), and [project data tab](../../../apps/web/src/pages/Projects/ProjectLearningPage.tsx). Content/scene grouping, explicit negatives, inherited splits, and semantic automatic-policy baselines already exist.

## Scope and interfaces

- Summarize image/instance counts by class and split, explicit negatives, image dimensions, and normalized box-size distribution. Distinguish image frequency from instance frequency. Display descriptive imbalance, not an arbitrary universal quality score.
- Compare two authorized frozen versions: added/removed contents, changed labels, class identity changes, and validation benchmark changes. Distinguish task-ID churn from semantic changes; preserve explicit categories for label corrections on the same content.
- Reuse existing semantic signatures and grouping rules. Do not modify split allocation, automatic-policy growth counts, or dataset eligibility while adding analysis.
- Add bounded project-scoped summary, comparison, and sample-list reads. Use manifest/snapshot metadata and indexed/paginated computation; do not load all source images in a global polling request.
- Provide authorized immutable sample media and normalized-label reads for later previews/evaluation. Missing historical media is an explicit unavailable state; never substitute a mutable source image under the frozen identity. Cache only by frozen content/schema digest and recheck access at read/download.
- Present a version summary, class-distribution table/chart, change categories, and filtered sample drill-down. Separate blocking preparation issues from advisory data observations. Existing ready versions stay downloadable even if advisory analysis fails.
- Legacy versions lacking optional summary fields can be analyzed from their retained frozen inputs. Any derived report uses a versioned sidecar or cache; do not rewrite dataset digests or package bytes.

```text
Frozen snapshot + manifest -> versioned analysis/cache -> authorized summary/diff
Frozen media + labels -------------------------------> bounded sample drill-down
```

## Acceptance and tradeoffs

Candidate changes affect dataset schema/API/service, analysis helpers, project data components, and typed web contracts. Expect more than eight files with tests/docs; no new service or third-party credentials. Additive report storage is allowed, but existing snapshot/manifest semantics remain authoritative.

Verify duplicates across different task IDs, label-only corrections, removed/reintroduced ancestry samples, explicit negatives, class reorder/rename, changed validation content, missing media, stale caches, pagination, and cross-project access. Use a larger metadata fixture to check bounded response sizes without duplicating production media. Browser checks include class/sample filters and side-by-side version navigation.

The fragile assumption is that frozen samples remain readable; missing objects must yield partial/unavailable analysis with exact counts, not invented data. The minimal option exposes existing manifest counts and validation identity with a basic semantic diff. Rollback removes analysis entry points/caches while preserving datasets, original packages, split history, and automatic-policy baselines. Apply the shared verification, cleanup, documentation, and promotion protocol.
