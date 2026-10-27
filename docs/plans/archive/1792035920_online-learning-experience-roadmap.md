# Online learning experience roadmap

> Status: completed
> Created: 2026-10-15
> Scope: T01–T14 reviewed and implemented under sequential user authorization; one commit per node, no release assignment.

This series extends the initial online-learning loop through 14 completed independent outcomes. Each linked archived plan records its fresh review, implementation and acceptance evidence. No release version is assigned. Further task or inference qualifications require the [promotion gate](../backlog/README.md#promotion-gate), a fresh repository audit and their own authorization.

## Verified baseline at draft creation

At draft creation, the working tree contained the initial [A–E delivery](1792035920_online-learning-experience-roadmap.md#existing-delivery) and subsequent UI changes. These changes are not assumed to be a committed release. Preserve unrelated uncommitted work and the retained manual acceptance environment.

- `/training` exposes managed projects, the queue, and qualified training repositories. Repositories describe framework capabilities; multiple entries may share one physical executor.
- `/projects/:id/learning` separates training, data, models, and automation. New experiments start inside the project. Preserve the removal of the homepage repository chooser and project-card new-experiment action.
- Immutable image-detection data packages, two qualified training frameworks, six bounded parameters, preflight digests, attempt isolation, cancellation, lost-resource reconciliation, resume, and verified downloads exist.
- Monitoring includes stages, epoch curves, attempt selection, artifacts, and configuration. Latest-epoch metrics and final best-weight evaluation have different identities.
- Ultralytics model registration, deployment checks, a fixed-image trial, basic two-candidate comparison, manual publication/rollback, and prediction provenance exist. LibreYOLO training then provided native downloads without managed inference return; T13 subsequently qualified that return path.
- Automatic training policies and trigger history exist, remain opt-in, and never publish models automatically. Their frozen runtime identity is not a reusable user recipe library.

### Existing delivery

See [the original closed-loop draft](../backlog/1791971285_online-learning-closed-loop.md) and its completed [data](1791972267_online-learning-phase-a.md), [training](1791975133_online-learning-phase-b.md), [model return](1791979058_online-learning-phase-c.md), [automatic policy](1791982370_online-learning-phase-d.md), and [LibreYOLO](1791983290_online-learning-phase-e.md) records. The new drafts extend those contracts rather than recreate them.

## Delivery map

| ID                                                          | Independent outcome                                                                           | Priority  | Hard dependency                                                          | Effort       |
| ----------------------------------------------------------- | --------------------------------------------------------------------------------------------- | --------- | ------------------------------------------------------------------------ | ------------ |
| [T01](1792035920_training-parameter-catalog.md)          | Versioned native parameter inventory, strict validation, reproducible effective configuration | Completed | Existing training adapters                                               | Large        |
| [T02](1792035920_training-configuration-workspace.md)    | Full-page grouped configuration, search, draft recovery, configuration import/export          | Completed | T01                                                                      | Medium       |
| [T03](1792035920_training-recipes-and-cloning.md)        | Project recipe revisions and traceable experiment cloning                                     | Completed | T01, T02                                                                 | Medium       |
| [T04](1792035920_training-data-insights.md)              | Frozen dataset differences, class distributions, quality explanations                         | Completed | Existing immutable data versions                                         | Medium       |
| [T05](1792036036_training-experiment-comparison.md)      | Compare 2–4 experiments using explicit evaluation identities and verified final reports       | Completed | Existing runs/artifacts; T01 enriches parameter display                  | Medium       |
| [T06](1792036036_training-augmentation-preview.md)       | Reproducible native augmentation previews with transformed boxes                              | Completed | T01, T02, T04; native CPU preview qualification                          | Medium–large |
| [T07](1792036036_training-observability-and-recovery.md) | Real resource telemetry, bounded live logs, actionable failure explanations                   | Completed | Existing executor fencing and attempt ownership                          | Medium–large |
| [T08](1792036036_training-sample-evaluation.md)          | Verified per-sample evaluation and error review with Workbench return                         | Completed | T04, T05; qualified controlled inference and fixed-threshold rules       | Large        |
| [T09](1792036036_training-model-lineage.md)              | Navigate data, experiments, models, deployment checks, and release history                    | Completed | Existing provenance and model APIs                                       | Medium       |
| [T10](1792036132_training-controlled-warm-start.md)      | Initialize a new experiment from a compatible project model                                   | Completed | T01, T02, T09; qualified source artifact                                 | Large        |
| [T11](1792036132_training-bounded-parameter-sweeps.md)   | Explicit-budget grid searches through the existing resource queue                             | Completed | T01, T03, T05, T07                                                       | Large        |
| [T12](1792036132_training-active-learning.md)            | Auditable suggestions for the next human annotation batch                                     | Completed | T04, T08; qualified inference provenance                                 | Large        |
| [T13](1792036132_training-libreyolo-model-return.md)     | Qualified LibreYOLO model registration, inference, publication, and rollback                  | Completed | Existing managed model contracts; native inference compatibility         | Large        |
| [T14](1792036132_training-task-qualification.md)         | Qualified polygon instance training, full checkpoint resume and separate box/mask reports     | Completed | T01; independently qualified polygon bridge, native training and metrics | Large        |

Effort records the original scope comparison, not a delivery-date estimate. The promoted plans contain exact implementation surfaces and completed acceptance records. No node introduced a new third-party account or external API key. T14 qualifies polygon instance training/downloads; segmentation inference return and other task types remain independent future work.

## Dependency and navigation model

```text
Existing adapters -> T01 parameter catalog -> T02 configuration -> T03 recipes
Existing data versions -> T04 data insights
Existing runs/artifacts -> T05 experiment comparison
Existing executor fencing -> T07 telemetry/recovery
Existing model provenance -> T09 lineage
Existing managed model contracts -> T13 LibreYOLO inference

T01 + T02 + T04 -> T06 augmentation preview
T04 + T05 -> T08 sample evaluation -> T12 active learning
T01 + T02 + T09 -> T10 warm start
T01 + T03 + T05 + T07 -> T11 sweeps
T13 -> T12 only when LibreYOLO is the ranking source
T01 + task-specific qualification -> T14 task expansion
```

T05 needs a verified final-report reader, included in its own scope; it does not wait for T08's per-sample analysis. T12 starts with an already qualified inference framework and does not require T13 for Ultralytics. T09 works before T10. T14's data/download slice can remain usable even if managed inference is not enabled. Each delivered slice must remain usable when later drafts never ship.

Keep global browsing at `/training`. Use the existing project tabs for actions and context; prefer nested project views and detail tabs over additional permanent sidebar entries. Desktop configuration uses category navigation, a form, and a summary; mobile uses the same state in an accordion layout. URL state contains resource IDs and view choices, never credentials or mutable configuration bodies.

## Shared contracts

1. **Configuration:** inventory upstream fields by pinned framework, model family, task, and adapter revision. Mark fields editable, platform-managed, inapplicable, or unqualified. The same reviewed definition drives API validation, UI metadata, executor checks, recipes, and automatic policies. Freeze resolved inputs and schema identity; historical runs never inherit new defaults.
2. **Evaluation:** preserve final best-weight SHA, dataset/validation digest, ordered classes/bindings, evaluator implementation/version, evaluation options, metric definition, and attempt identity. Missing or incompatible provenance yields an explicit incompatibility reason, not a ranking. Framework-specific raw loss values do not form a cross-framework leaderboard.
3. **Resources:** use existing physical-resource ownership, fencing, process supervision, and verified artifact publication. Lost execution does not free the lease. Auxiliary native jobs need their own typed purpose and ownership contract; do not disguise them as training or give the API a Docker socket.
4. **Authority:** keep current project-manager and API-key scope checks. Recheck authority when work is accepted, claimed, and published. Read-only analysis still respects data/model ownership. No cross-project sharing is introduced by this series.
5. **Learning:** human review remains the gate for training truth. Model candidates, sweeps, active-learning suggestions, and automatic policies do not implicitly change the current release.
6. **Recovery:** changing data, parameters, initialization weights, or schema creates a new experiment. Resume preserves the original immutable contract. Recipe application, cloning, warm starts, and retries remain distinct operations.

## Shared implementation and verification protocol

Promote only the selected draft, keeping existing filenames and updating links. Recheck current migrations, native package pins, capabilities, generated API contracts, and the working tree. Preserve symlinked `.env` and shared Node dependencies; Python and generated API types remain checkout-owned. Determine actual worker mounts before refreshing owned services.

For implementation, run relevant existing or new regression tests only. Current anchors include `apps/api/tests/test_training_runs.py`, `test_training_datasets.py`, `test_training_hub.py`, `test_learning_policies.py`, `test_trained_models.py`, and `test_trained_model_routing.py`; executor tests live in `apps/training-executor/tests/`. Use the verified disposable database and current virtual environment. The exact environment-bound commands belong in the promoted plan, after checking the runtime manifest.

The current configuration/event regression command is `pnpm --filter @anno/web exec vitest run src/pages/Projects/ProjectTrainingPanel.test.tsx src/pages/Training/useTrainingEvents.test.tsx`; promoted plans replace this example with their exact affected test list. Frontend implementation also requires `pnpm --filter @anno/web typecheck` and `pnpm --filter @anno/web lint`. Styling requires `pnpm --filter @anno/web lint:css-tokens`. API changes require OpenAPI export/check, web code generation, and SDK coverage/contract updates. Run `pnpm lint:python` for changed Python. Build affected published documentation with `pnpm docs:build`.

Manual acceptance covers desktop and 390px mobile, light/dark themes, keyboard use, refresh/back navigation, delayed responses, permission changes, errors/empty states, and recent API/console evidence. Native behavior changes require bounded real framework verification; synthetic scores establish execution and provenance, not model quality. Do not run GPU workloads merely to write or promote drafts.

After every check, remove task-owned screenshots, temporary reports, caches, disposable resources, and test jobs. Preserve the user's retained manual environment and data. Review the final diff and run `git diff --check`.

Implementations update the current [user guide](../../../docs-site/user-guide/online-learning/index.md), [API guide](../../../docs-site/api/guides/training-datasets.md), [architecture explanation](../../../docs-site/dev/concepts/training-datasets.md), and CHANGELOG as appropriate. Schema/protocol or model-identity changes may require an ADR. Draft creation alone must not claim these features in rendered product documentation.

## Simplicity, rollback, and promotion ownership

The minimal option is a small reviewed expansion of framework-specific parameters plus existing experiment pages. The catalog/workspace approach earns its larger surface by preventing UI/API/runner drift and preserving configuration provenance. Start with T01/T02; T04, T05, T07, and T09 can proceed as independent later branches.

No new always-on service is proposed. Reuse the existing API, database, Celery coordination, object storage, executor, and inference admission. T13 must stop promotion if dependency compatibility would require a new service; that deployment tradeoff needs a revised concrete plan. Migrations are additive; rollback stops new writes/claims and preserves retained versions, reports, artifacts, recipes, and releases. Legacy parsers remain available for historical records.

Maintainers choose scheduling and release milestones. The implementation owner supplies an exact file/migration/API list, trigger evidence, runtime authorization, validation commands, and rollback procedure at promotion. Unmet triggers keep a draft in backlog without blocking independent work.

## Outcome

- Delivered scope: All T01–T14 outcomes were reviewed, implemented and accepted within their recorded scopes; their linked plans retain the individual evidence and qualification limits.
- Completion evidence: the existing `Delivery map` section above records implementation and verification. Archival on 2026-10-22 reuses that record; application tests and live acceptance were not rerun for this documentation move.
- Enduring documentation: [index.md](../../../docs-site/user-guide/online-learning/index.md), [training-datasets.md](../../../docs-site/dev/concepts/training-datasets.md), [training-datasets.md](../../../docs-site/api/guides/training-datasets.md); user impact is recorded in `CHANGELOG.md`.
- Release milestone: no new milestone or version is assigned by this archival.
- Remaining boundaries: preserve the exclusions and qualification limits recorded above; new task, inference or deployment scope requires its own plan and acceptance.
