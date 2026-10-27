# T03 — Project training recipes and experiment cloning

> Status: completed — delivered and verified on 2026-10-15
> Authorization: user requested sequential review and implementation, one commit per node.
> Depends on: [T01](1792035920_training-parameter-catalog.md), [T02](1792035920_training-configuration-workspace.md)
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)
> Release milestone: not assigned.

## Fresh audit and outcome

T01 and T02 are committed. Preserve the unrelated `.orca/` asset and the retained, owned development environment. The database head before this node is `0178`. Read-only audits of API/policy ownership and draft/UI reuse confirmed the following decisions. This is a large additive change spanning more than eight files and four existing components; it remains one independently reviewable node.

Managers reuse complete configurations, select immutable revisions, inspect effective-value differences, and retain provenance without changing existing run digests or silently updating automatic policies.

## Contracts and boundaries

- Project-owned recipe identities have an archived flag and a monotonically increasing head. Revisions are append-only snapshots with author, name, description, schema identity, canonical user values, fully resolved effective values/origins/inactive fields, original base ID/SHA/filename, framework version and image identity. No dataset, executable script, runtime assignment, resume state, lease or upload URL is reusable configuration.
- Manager/scope-protected list, detail, revision history, create/revise, archive, resolve and clone-candidate APIs are project-scoped. Lists/history are paginated. Revision writes capture an idempotent request and expected head; conflicts retain editor content and require an explicit new basis. Archive blocks new adoption, never historical reads, queued runs, or already adopted policy execution.
- Run provenance is an independent nullable JSON column. Policy provenance stays outside its execution template. Legacy requests omit only the new absent field, retaining the original `preflight_digest=None` serialization. Existing request replay precedes any source/archive/catalog validation.
- A revision replays `user_values` and compares the entire resolved snapshot and original base/image/schema identity. It must never reinterpret omitted old defaults or submit platform/inactive effective fields as editable overrides. Incompatible sources remain readable but cannot be directly applied. Legacy runs only recorded six fields and fixed options; full clone/save requires a complete configuration, with an explicit migration explanation for legacy sources.
- Applying a recipe or clone captures owner/session/draft revision/catalog/schema/source identity, resolves the current configuration for a full effective-value diff, and revalidates before one atomic draft update. Acceptance rotates the request ID and clears preflight; cancellation preserves the draft. Existing explicit name/data choices remain; a new draft starts without data. Clone URL intent is consumed with replace on acceptance/cancellation, handles same-route source changes, and never overwrites an unconfirmed create request.
- Persist a bounded source descriptor with original base/image/schema and values, extending the strict draft and pending-body validators while retaining old drafts. Manual edits show a modified source; identity changes require explicit detachment or migration. Source deletion never erases saved revision/run provenance or introduces a foreign-key restriction on deleting the original run.
- Policies pin a specific revision and full configuration. New revisions only announce an available update. Adoption uses the existing current authority, policy CAS, cancellation of unstarted work and revision increment. Recipe editing does not save a policy. Archiving a pinned recipe permits ordinary threshold/cooldown edits and existing execution. The existing policy form's six-field serialization bug must be fixed: ordinary edits retain full configuration and provenance, and canonical edits change only their own overrides.
- No new service, environment variable, dependency, account, global sharing or warm-start capability is introduced. Native training behavior is unchanged; existing qualified executors consume the same frozen configuration.

```text
Project recipe -> immutable revision + original base/schema -> effective diff
Verified run -> saved revision ------------------------------^       |
Existing run -> clone candidate + immutable provenance -------^       |
                                                          atomic draft
                                                               |
                                               fresh preflight -> run snapshot
Immutable recipe revision -> explicit policy save -> pinned automatic run
```

## Exact change surface

- Add `apps/api/app/db/models/training_recipe.py`, migration `apps/api/alembic/versions/0179_training_recipes.py`, and the nullable run provenance field in `db/models/training_run.py`; register models in `db/models/__init__.py`.
- Add `schemas/training_recipe.py`, `services/training_recipes.py`, `api/v1/training_recipes.py`; register the router and extend `schemas/training_run.py`, `schemas/learning_policy.py`, `api/v1/training_runs.py`, `api/v1/learning_policies.py`, and `services/learning_policies.py`.
- Add `apps/web/src/api/trainingRecipes.ts`, `pages/Training/TrainingRecipeLibrary.tsx`, and `TrainingReuseDialog.tsx`; integrate `TrainingCreatePage.tsx`, `trainingDraftStorage.ts`, `TrainingRunMonitor.tsx`, and `pages/Projects/ProjectLearningPolicyPanel.tsx`.
- Add backend recipe regression tests and frontend recipe/diff tests; extend the affected draft, create-page and policy tests. Preserve the existing run/parameter/policy suites.
- Update OpenAPI snapshot, regenerate ignored web client, classify web-only SDK endpoints in `packages/python-sdk/api-coverage.toml`, and verify the SDK OpenAPI contract.
- Update `README.md`, `CHANGELOG.md`, `docs-site/user-guide/online-learning/index.md`, `docs-site/api/guides/training-datasets.md`, `docs-site/dev/concepts/training-datasets.md`, generated API route index and backlog/dependency links.

## Verification and runtime

Use the current API virtual environment and verified disposable test mode. Commands:

```sh
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_training_recipes.py tests/test_training_parameters.py tests/test_learning_policies.py tests/test_training_runs.py -q'
pnpm --filter @anno/web exec vitest run src/pages/Training/TrainingCreatePage.test.tsx src/pages/Training/trainingDraftStorage.test.ts src/pages/Training/TrainingRecipeLibrary.test.tsx src/pages/Projects/ProjectLearningPolicyPanel.test.tsx src/pages/Projects/ProjectTrainingPanel.test.tsx
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

Add focused tests for revision CAS/idempotency, archived/history references, cross-project and attempt relationships, deleted sources, strict source identity, effective-value differences, independent request IDs, pending-source restoration, delayed owner/URL changes and v2 policy preservation. Confirm the SDK contract with its checkout virtual environment.

Before migrating retained development data, verify its manifest/database owner and absence of active runs/triggers. Restart only this worktree's managed API/Web/ordinary and maintenance workers with the retained private training overrides; apply migration through the launcher. The standalone GPU executor's native inputs are unchanged. Verify live catalog/API health and worker registration. Browser acceptance uses a new named session, desktop and 390px mobile, both themes, keyboard, apply/cancel, refresh, clone intent, update announcements and explicit policy adoption. Inspect recent requests and console errors. Remove only task-created recipes/policies/drafts/jobs, browser state, logs, caches, screenshots and disposable test resources.

## Acceptance and rollback

Acceptance requires API and frontend regression checks, live browser evidence, current documentation, clean diff checks and one normal-hook commit. Queued/run snapshots and policy expansions must remain unchanged after recipe edits/archival. Provenance is not part of the executor configuration digest. Rollback disables new recipe UI/API writes while retaining immutable revisions and expanded snapshots; never delete referenced revisions or downgrade retained data as a rollback shortcut.

## Delivery evidence

- Migration `0179` and eight scoped recipe/clone operations are delivered. Immutable revisions use expected-head checks and idempotent writes. Source deletion leaves saved snapshots readable and compatible; archiving blocks new adoption while retained policy revisions remain usable.
- The configuration workspace and monitor expose recipe saves, revision history, complete effective-value differences and explicit source adoption. Draft/source/pending validation retains ownership and provenance. Same-route clone changes resume after a stale preview releases its lock; editing while the initial source request is pending requires explicit retry and confirmation.
- Policy editing preserves the entire advanced configuration, keeps adopted revisions pinned, announces updates and requires a separate policy save. Threshold edits and archived sources do not downgrade the full configuration to the legacy six-field shape.
- All 43 API recipe/parameter/policy/run regressions and all 38 focused frontend cases passed. Web type checking, web lint/CSS tokens, Python lint, OpenAPI consistency, formatting and diff checks passed. Web lint retains five existing warnings. Documentation built in 33.82 seconds with existing highlighting/bundle notices. The SDK coverage manifest classifies the eight new operations as web-only, with its OpenAPI contract verified.
- Live browser checks covered desktop and 390px layouts, light/dark themes, keyboard input, saves/revisions, conflict retention, apply/cancel, source modification, refresh, clone links, explicit policy adoption/update and archived-source threshold edits. Recent API calls and the console were inspected. A temporary native Ultralytics experiment created from the adopted configuration succeeded with matching preflight/frozen configuration digests and retained source provenance; this verifies execution, not model quality.
- Deleting the temporary original experiment left its saved recipe compatible and explicitly reported the missing source. Applying that saved revision still succeeded. The temporary experiment/artifacts, two recipes, policy, AsyncJob and notification, local draft, browser/vault, screenshots and disposable test resources were removed. The original two acceptance runs, data version and running owned development services remain available.

No version bump, push or remote CI result belongs to this node.

## Outcome

- Delivered scope: Delivered immutable project recipe revisions, experiment cloning, configuration differences and explicit automatic-policy adoption.
- Completion evidence: the existing `Delivery evidence` section above records implementation and verification. Archival on 2026-10-22 reuses that record; application tests and live acceptance were not rerun for this documentation move.
- Enduring documentation: [configuration.md](../../../docs-site/user-guide/online-learning/configuration.md), [training-datasets.md](../../../docs-site/dev/concepts/training-datasets.md), [training-datasets.md](../../../docs-site/api/guides/training-datasets.md); user impact is recorded in `CHANGELOG.md`.
- Release milestone: no new milestone or version is assigned by this archival.
- Remaining boundaries: preserve the exclusions and qualification limits recorded above; new task, inference or deployment scope requires its own plan and acceptance.
