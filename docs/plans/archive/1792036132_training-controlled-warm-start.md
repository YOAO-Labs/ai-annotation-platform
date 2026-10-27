# T10 — Controlled initialization from a trained project model

> Status: completed
> Audited: 2026-10-15 against local HEAD 4b2d5009e
> Authorization: user requested sequential audit and implementation, one commit per node.
> Release milestone: not determined
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Fresh audit and qualification

T01/T02 provide frozen native parameter definitions and a full configuration workspace;
T03 provides project recipes, configuration cloning and explicit policy adoption.
T09 provides DB-only provenance navigation and candidate decisions. Managed model
versions retain exact successful run/attempt artifacts, ordered native classes,
bindings and evaluation provenance. They are not currently fresh-training inputs.
The worktree is clean. Shared environment/dependency links remain untouched.

The pinned Ultralytics implementation supports weights-only transfer when `resume`
is false and restores training state only on its explicit resume path. References:
[pinned model implementation](https://github.com/ultralytics/ultralytics/blob/v8.4.177/ultralytics/engine/model.py),
[pinned trainer implementation](https://github.com/ultralytics/ultralytics/blob/v8.4.177/ultralytics/engine/trainer.py),
and [official training modes](https://docs.ultralytics.com/modes/train/).
The installed owned executor reports Ultralytics 8.4.177, Torch 2.14.1+cu132 and
LibreYOLO 1.6.0. A read probe verified the retained YOLO11n detection artifact
(SHA `f68acc8c7deefac913099a29cf7f53d2a90082b930629918e17d57ff2183f615`,
5,449,754 bytes): native classes are `car, person`; all 499 state tensors transfer
unchanged into a fresh DetectionModel. The inference artifact has epoch -1 and no
optimizer; a new SGD has no state and a new scheduler starts at zero. Probe copies
were removed. This is source-loading evidence, not a claim of completed training.

Two audit findings shape the design: current `base_model` also selects the parameter
schema and controlled local architecture; replacing it would corrupt that contract.
Current recipe/clone/policy fixed-field projections would silently omit a new source.
Also, older executors accept the same parameter identity while ignoring new fields;
an independent initialization capability and verified native receipt are required.

## Chosen scope

Managers may initialize a new Ultralytics YOLO11 image-detection experiment from a
retained same-project candidate with the same controlled base variant and exact
ordered native classes. Publication, deployment readiness and candidate archive
state do not decide training eligibility. Changing data/parameters creates a new
experiment; source weights do not imply continuation of the source optimizer or epoch.

Keep three independent identities: configuration `origin`, weights `initialization`,
and this experiment's `resume_from_id`. Clone and recipe adoption preserve the original
initialization pin. They never select the copied experiment's output implicitly.
Automatic policies can explicitly pin the same recipe initialization and cannot follow
later model publications or recipe heads. Existing defaults and request/configuration
hashes remain unchanged when initialization is omitted or explicitly catalog-based.

This is a large change across more than eight files and API, DB, shared contract,
executor, frontend and documentation. It requires no new dependency, service,
environment variable, account, token or cloud credential.

```text
Project candidate -> source resolver + object verification -> immutable source pin
                        |                                  |
               recipe revision / policy            new run + source FK
                        |                                  |
              explicit configuration reuse          capability-gated claim
                                                           |
                                              verified short-lived download
                                                           |
                                             native weights-only transfer
                                                           |
                                      fresh-state receipt -> new checkpoints
                                                           |
                                       later resume uses this run's checkpoint

Model lineage <- source FK <- run / recipe revision (configuration origin remains separate)
```

The minimal alternative keeps administrator base weights only. It cannot deliver
project-model initialization. Reusing resume would preserve the wrong immutable
inputs and training state, so it is rejected. The fragile assumption is that the
controlled inference artifact is suitable for full same-architecture transfer. Native
validation checks every source state tensor, exact class order and fresh trainer state;
an unknown/incompatible artifact fails before a training batch instead of falling back.

## Frozen contracts

### Source selection and persistence

- Add optional typed `initialization` to new-run/preflight, configuration resolution
  and policy save requests: `{kind: catalog}` or `{kind: trained_model,
model_version_id, source_digest}`. Catalog/omitted preserves existing request and
  frozen configuration bytes; a trained source requires full v2 configuration.
  No path, URL, arbitrary checkpoint, external model, class/head remap or optimizer
  option is accepted through this field.
- Add manager-only GET `/projects/{project_id}/training-runs/initialization-sources/{model_id}`
  with target framework/base model and optional dataset ID. It returns an explicit
  eligibility result, bounded reasons and a server-generated immutable pin. Missing
  or foreign models return 404. Metadata/head checks explain availability; full
  content hashing is performed by preflight, enqueue and initial claim. The UI does
  not describe metadata-only checks as complete object verification.
- Freeze model/run/attempt/project identity, exact controlled artifact key/SHA/size,
  source config/data/validation digests, framework/version, native model family and
  variant, native class order and bindings/digest, source runtime identity and
  evaluation artifact provenance. Include a shared qualification policy digest.
  Store a canonical `source_digest`; requests choose this digest, not a mutable latest
  model or release. Enforce the existing 2 GiB checkpoint-sized bound before streaming.
- Resolve against the same project's authoritative run/attempt, successful source
  attempt, exact committed weight artifact and immutable registration provenance.
  The target class list must equal the source native list in order. Reject unknown
  task/family/version/runtime evidence, missing/corrupt/oversized objects and changed
  pins. Archive remains eligible; deployment/publication are independent.
- Migration 0185 adds nullable named `initialization_model_id` RESTRICT FKs to
  TrainingRun, TrainingRecipeRevision and LearningPolicy, with reference indexes.
  Accepted rows match their JSON pin; historical rows stay NULL without backfill or
  digest rewriting. Existing model-to-source run/attempt protection remains before
  object deletion. No model hard-delete API is added.
- Preserve old recipe request hashes by removing only newly added absent/default
  initialization fields before hashing; do not globally omit existing null fields.
  Request replay returns the original accepted run/revision before new validation.

### Enqueue, claim and native execution

- Preflight and enqueue independently resolve source identity and verify the full
  object against size/SHA and attempt metadata. Accepted source references cannot
  be unpinned by candidate archive or policy disable.
- New executor capability identifies the shared initialization policy. Old executors
  cannot claim trained-source runs even when their parameter schema matches. Catalog
  runs retain their current compatibility behavior and frozen identities.
- Claim rechecks actor authority, the accepted frozen source and full object integrity
  before assigning an initial attempt. Missing/changed sources fail that run cleanly
  before GPU training. Claim already holds resource/run locks and must not take
  project advisory, policy or recipe exclusive locks. Source model reads use shared
  row locks where needed; no reverse policy-lock ordering is introduced.
- Assignment supplies a separate short-lived initialization download. The worker
  downloads into the attempt-owned directory, checks maximum/exact size and SHA,
  retains the controlled base architecture, and passes a distinct initialization path.
  Resume supplies this run's own checkpoint and takes precedence; it never reloads
  the parent candidate as a fresh start. The pin remains historical provenance.
- Native loading checks the actual DetectionModel, task, YOLO11 variant, ordered
  names and all transferred state tensors. Fresh training must start at epoch zero
  with empty optimizer state, a new scheduler/scaler/EMA/best state. It cannot inherit
  the source run's output directory, data or train arguments.
- Extend the bounded native-config evidence with initialization policy/source digest,
  actual weight SHA, transfer fingerprints/count and fresh-state facts. Backend success
  validation checks that receipt against the frozen pin and exact attempt mode. A
  resumed attempt records checkpoint-resume mode and the original initialization pin,
  while existing checkpoint validation restores its own state. Failed/legacy catalog
  attempts retain their existing behavior.
- Recipe snapshots, resolution, cloning, availability and origin comparison preserve
  the full pin. Policies persist it, revalidate it when accepted and before child
  creation, and compare the child pin to the accepted policy snapshot. Source loss
  produces an actionable failure/suspension, never a catalog fallback.
- T09 adds typed initialization input edges for runs/revisions and indexed reverse
  initialized-run/revision lists for models. Navigation remains DB-only, bounded and
  project-owned; it does not stream weights or check executors.

### Workspace behavior

- Add initialization choice alongside the existing controlled architecture, with
  paginated archive-inclusive candidates and independent selected-ID lookup. Display
  source data/evaluation context and specific qualification/compatibility reasons.
  Preserve an unavailable selection until the manager explicitly changes it.
- Initialization is separate from draft configuration source. Changing it invalidates
  preflight/preview view ownership, changes the creation UUID and clears an unsubmitted
  pending body. A lost-response accepted request remains locked and retries its exact
  saved body/UUID after refresh; source refetch cannot rewrite that request.
- Bind source/reuse/file/preflight work to authentication revision, draft revision and
  view intent, including session/source A→B→A. Add strict local-storage validation
  without invalidating old catalog drafts. The final review shows the actual frozen
  source SHA/size/IDs and explains fresh training versus checkpoint resume.
- Recipe/clone adoption shows initialization differences and retains the pin. Policy
  adoption/reload/save preserves it; clearing configuration origin does not clear
  weights. Unsupported choices require explicit changes rather than hidden resets.
- Keep portable YAML v1 and bare-parameter imports compatible. Full exports containing
  a trained source use v2 with model ID/source digest and no URL/secret; importing v2
  shows source changes and revalidates current same-project authority. V1/bare imports
  continue to replace parameters while retaining the current explicit initialization.
- Augmentation preview remains a data-transform preview using the controlled model
  architecture, not a weight-loading qualification. Explain this scope when a trained
  source is selected; initialization changes invalidate the previous preview through
  the existing draft/view binding.

## Exact file scope and dependency order

1. Shared qualification: `apps/_shared/training_contract/training_contract/initialization.py`
   and `apps/_shared/training_contract/tests/test_initialization.py`.
2. Persistence: `apps/api/alembic/versions/0185_training_initialization.py`,
   `apps/api/app/db/models/training_run.py`, `training_recipe.py`, `learning_policy.py`.
3. API contracts/resolution: `apps/api/app/schemas/training_initialization.py`,
   `training_run.py`, `training_recipe.py`, `learning_policy.py`;
   `apps/api/app/services/training_initialization.py`, `training_runs.py`,
   `training_recipes.py`, `learning_policies.py`, `model_lineage.py`;
   `apps/api/app/api/v1/training_runs.py`, `training_recipes.py`, `learning_policies.py`.
4. Executor: `apps/training-executor/training_executor/worker.py`, `train.py`,
   `initialization.py`; `apps/training-executor/tests/test_executor.py`,
   `test_adapters.py`, `test_initialization.py`.
5. Frontend: `apps/web/src/api/trainingRuns.ts`, `trainingRecipes.ts`;
   `apps/web/src/pages/Training/TrainingInitializationSource.tsx`,
   `TrainingCreatePage.tsx`, `useTrainingDraft.ts`, `trainingDraftStorage.ts`,
   `trainingConfiguration.ts`, `TrainingRecipeLibrary.tsx`, `TrainingReuseDialog.tsx`,
   `TrainingRunReview.tsx`, `ModelLineagePage.tsx`;
   `apps/web/src/pages/Projects/ProjectLearningPolicyPanel.tsx`.
6. Regression owners: `apps/api/tests/test_training_initialization.py`,
   `test_training_runs.py`, `test_training_recipes.py`, `test_learning_policies.py`,
   `test_model_lineage.py`; web `TrainingInitializationSource.test.tsx`,
   `TrainingCreatePage.test.tsx`, `trainingDraftStorage.test.ts`,
   `trainingConfiguration.test.ts`, `TrainingRecipeLibrary.test.tsx`,
   `ProjectLearningPolicyPanel.test.tsx` in their corresponding page directories.
7. Contracts/docs: `apps/api/openapi.snapshot.json`,
   `packages/python-sdk/api-coverage.toml`, generated API route reference,
   `README.md`, `CHANGELOG.md`, `docs-site/user-guide/online-learning/index.md`,
   `docs-site/api/guides/training-datasets.md`,
   `docs-site/dev/concepts/training-datasets.md`,
   `docs/adr/archive/0081-controlled-training-initialization.md`, ADR index, this plan,
   backlog README and roadmap links. Generated web API types remain local/ignored.

## Verification and acceptance

- Use the verified disposable worktree test mode, never the retained development DB:
  `pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_training_initialization.py tests/test_training_runs.py tests/test_training_recipes.py tests/test_learning_policies.py tests/test_model_lineage.py -q'`.
  Apply/downgrade/upgrade 0185 on that disposable database. Cover legacy digest/replay,
  exact classes/variant/runtime/provenance, source head/hash/size errors, foreign and
  withdrawn authority, delete-before-storage guards, archived source eligibility,
  independent origin/initialization/resume, recipe/policy pinning, claim capability,
  fresh/resume receipt mismatch and concurrent source acceptance/deletion.
- Shared qualification and executor tests exercise exact source size/SHA before
  native launch, old capability rejection, cancellation/recovery, full tensor transfer
  and original checkpoint resume. Native tests use pinned installed dependencies;
  never present mocked adapter tests as native qualification.
- Web targeted Vitest covers source selection, pagination/off-page selected source,
  unavailable sources, revision/ABA guards, preview/preflight invalidation, YAML v1/v2,
  pending refresh/replay and recipe/policy preservation. Run web typecheck/lint/CSS
  tokens, Ruff/format, OpenAPI export/codegen/check and SDK contract coverage checks.
- Refresh only the owned training executor image/process and affected API/Celery
  services after inspecting current mounts. Do not kill the preserved CPU inference
  service or unrelated GPU consumers. Apply the additive migration to the verified
  owned development environment. Runtime catalog and advertised image must match.
- Real native acceptance uses task-owned candidate/run records and the retained frozen
  data: bounded catalog-start control, a project-source new run and an interrupted
  project-source run resumed from its own committed checkpoint. Verify native receipt,
  source tensor transfer, new epoch/optimizer/scheduler/scaler/best state, resumed epoch
  and preserved immutable digest. No model-quality-improvement claim is required.
- Real browser checks source choice and incompatibility messages, exact frozen review,
  successful new experiment and T09 edge, recipe/policy reload, refresh and keyboard,
  1440px/390px light/dark. Inspect recent feature requests and console errors.
- Build documentation; check changed links, ADR/freshness, final diff and
  `git diff --check`. Remove task-owned runs/attempt artifacts, candidates, recipes,
  policies, jobs, browser/auth/screenshot/probe/build/test outputs in FK-safe order.
  Retain original native runs/data/weights, append-only audit events and lock inodes.

## Rollback

Stop accepting new trained-source choices and hide their controls; retain pins,
source rows and execution support for already accepted work. Catalog initialization
and explicit existing checkpoint resume remain available. Do not downgrade populated
source references or delete parent weights. One commit contains the accepted node;
no push, release, version bump or remote CI result is implied.

## Outcome

- Delivered in this node's single commit; release milestone remains undetermined.
- Added immutable same-project initialization pins, bounded source qualification,
  three nullable retained-model references, legacy digest preservation, independent
  origin/initialization/resume identities, recipe/policy preservation and DB-only
  forward/reverse lineage links. Initial object loss fails explicitly before training.
- Added executor capability gating, separate verified downloads, native full-state
  transfer checks and backend-verified fresh/resume receipts. The owned image and
  API/Celery processes were refreshed; the preserved CPU inference service remained
  available. Migration 0185 was applied to the owned development environment.
- Added paginated candidate selection, unavailable-source explanations, frozen
  review, source-preserving clone/recipe/policy flows and strict YAML v2 identity.
  Draft, pending replay and asynchronous operations retain session/view ownership,
  including ABA changes. Legacy YAML only replaces parameters.
- Local verification: 75 distinct API regressions, shared qualification tests,
  pinned executor tests, 82 relevant web tests and 4 SDK contract tests passed.
  Native initialization's six focused executor cases passed in the rebuilt image.
  Disposable migration downgrade to 0184 and upgrade to 0185 passed. Ruff/format,
  web typecheck/ESLint/CSS tokens, OpenAPI/codegen, documentation build, ADR,
  frontmatter/freshness and final diff checks passed. Existing unrelated lint and
  documentation syntax/chunk warnings remain unchanged.
- Real GPU acceptance completed a catalog control and three project-source runs,
  including the experiment submitted through the browser. A fourth project-source
  run stopped after its own committed epoch-5 checkpoint, then resumed at epoch 5
  and completed epoch 100 with the same configuration digest and original pin.
  Fresh native evidence shows 499 equal source/loaded state tensors, epoch zero,
  empty optimizer/scaler state, scheduler epoch -1, EMA zero and no historical best.
  Resume evidence records `checkpoint_resume`; no quality-improvement claim is made.
- Browser acceptance verified source choice and incompatible-framework explanations,
  exact frozen identities, successful creation, clone/recipe/policy preservation and
  refresh, keyboard lineage navigation and the independent initialization edge.
  Reviewed 1440px/390px light/dark screenshots; the mobile page has no horizontal
  overflow. Feature requests returned 200 and no browser errors were observed.
  Race, corruption, authority loss and concurrent delete cases are automated checks,
  not claims of browser race reproduction.
- Removed five owned runs/six attempts and their object prefixes, one candidate,
  one recipe, the disabled policy, five jobs/notifications, disposable test resources,
  private browser/auth/screenshots and local check/build outputs. Retained the
  original two native runs, one dataset, twelve tasks and original source weights.
  Release selection remains generation 0/null; append-only audit records remain.
- Updated README, changelog, user/API/architecture documentation and
  `docs/adr/archive/0081-controlled-training-initialization.md`. The qualification
  endpoint is explicitly web-only in SDK coverage. Remaining series work starts
  with a fresh T11 audit; no push, release or remote CI result is claimed.
