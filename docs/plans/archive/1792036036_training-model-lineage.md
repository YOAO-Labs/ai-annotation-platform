# T09 — Model lineage and candidate decisions

> Status: completed
> Audited: 2026-10-15 against local HEAD d96457b21
> Authorization: user requested sequential audit and implementation, one commit per node.
> Release milestone: not determined
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Audit and outcome

Existing dataset/run/attempt/model/deployment/release identities and T03 recipe/clone provenance are authoritative. T08 provides frozen sample evaluations. No trained-weight initialization entity exists before T10. Deployment checks are mutable latest checks; release snapshots are historical evidence. Existing model registration/deployment/publication remain manual and retain release generation CAS. The checkout was clean; Python and generated types are local; environment and Node dependency symlinks remain shared and untouched.

Implement a project-manager-only, DB-only one-hop provenance navigator and append-only candidate decisions. Read projections must never call executor health, reconcile runs, presign storage, expose authorization/runtime/config bodies, or traverse all ancestors. This is a large change touching more than eight files and API, DB, frontend, SDK contracts and documentation.

```text
Dataset parent -> run -> exact attempt -> model -> latest deployment check -> release
                  |          |            |                                |
            config clone  checkpoint    decisions                      previous/rollback
                  |       resume          |
               recipe                 sample evaluation -> frozen data
```

## Frozen contracts

- GET `/projects/{project_id}/model-lineage/{kind}/{node_id}` returns a bounded node, direct input links/gaps and typed available related sections. Kinds: dataset, run, attempt, model, deployment, release, recipe, recipe_revision, evaluation. Optional `relation`, `cursor` and `limit` (1–20) expand one section only; keyset cursors bind project/root/relation and use deterministic sort keys. No recursive SQL or graph dependency.
- Root missing/foreign returns 404. Independent FKs are checked for semantic ownership: attempt/run, model/run/attempt, deployment/model, recipe revision/recipe, evaluation/model/deployment/data, resume same run and earlier ordinal, release same-project earlier generations. Malformed/missing/self/cross-project snapshot references produce gaps without foreign metadata. Do not substitute a current/latest attempt for an explicit historical identity. Cycles in visited navigation are identified in the UI.
- Model input digests/SHA and release snapshot identity fields are selected explicitly. Clone means configuration reuse; resume means checkpoint reuse; base-model names do not imply a T10 edge. Rich T05 comparisons retain their compatibility reasons and exact attempt IDs; T08 evaluations link to their existing reports.
- Related lists use limit+1 keyset pagination, with indexes matching actual reference/sort columns. Prediction usage is opt-in and bounded to 1000 attributable retained rows plus a truncation flag. Match controlled frozen metadata and both prediction composite key columns, project, SHA/classes/binding identity. It excludes legacy/unattributable/deleted records; zero never means never used.
- Migration 0184 adds ModelDecision with model/project RESTRICT, creator SET NULL plus frozen creator label, positive per-model sequence, model/request UUID idempotency, body digest, disposition active/research/declined/archived and trimmed 1–2000 character note. API only appends; new decisions supersede the display disposition without rewriting earlier notes. No evaluation FK, no new deletion race or purge owner.
- Decision POST shares project advisory, owner authority and model row locks, replays same request/body, rejects changed-body UUID reuse, preserves concurrent notes and never updates project selection or release generation. GET is bounded by descending sequence. Existing model list adds archive exclude/include/only and decision/current metadata; default excludes archived except current release. Direct historical detail remains available. Project deletion guards candidate/decision references before children/storage mutation; existing dataset/run guards remain before S3 deletion.
- Frontend route `/projects/:id/learning/lineage/:kind/:nodeId` uses existing PageContainer/Button/Badge and semantic Tailwind tokens. Model tab URL preserves selected model, archive filter/page; lineage URL preserves root, related cursor and validated same-project return path. Exact run/attempt/data links, refresh/browser back and manager/session changes remain safe. Queries/writes use ownership and view checks including ABA, with stable decision request UUID for retries. No new sidebar or animation.

## Exact file scope and dependency order

1. Persistence: `apps/api/alembic/versions/0184_model_lineage.py`, `apps/api/app/db/models/model_decision.py`, `apps/api/app/db/models/__init__.py`.
2. Read/write contracts: `apps/api/app/schemas/model_lineage.py`, `apps/api/app/services/model_lineage.py`, `apps/api/app/api/v1/model_lineage.py`, `apps/api/app/api/v1/router.py`, `apps/api/app/api/v1/trained_models.py`, `apps/api/app/api/v1/training_runs.py`, `apps/api/app/schemas/trained_model.py`, `apps/api/app/services/project_delete.py`.
3. Frontend: `apps/web/src/api/modelLineage.ts`, `apps/web/src/api/trainedModels.ts`, `apps/web/src/pages/Training/ModelLineagePage.tsx`, `apps/web/src/pages/Training/SampleEvaluationPage.tsx`, `apps/web/src/pages/Projects/ProjectModelsPanel.tsx`, `apps/web/src/pages/Projects/ProjectLearningPage.tsx`, `apps/web/src/pages/Projects/ProjectTrainingPanel.tsx`, `apps/web/src/App.tsx`.
4. Meaningful regressions: `apps/api/tests/test_model_lineage.py`, `apps/web/src/pages/Training/ModelLineagePage.test.tsx`, existing `SampleEvaluationPage.test.tsx`, `ProjectModelsPanel.test.tsx`, `ProjectTrainingPanel.test.tsx` and `ProjectLearningPage.test.tsx` where needed.
5. Contracts/docs: `apps/api/openapi.snapshot.json`, `packages/python-sdk/api-coverage.toml`, generated API reference, `README.md`, `CHANGELOG.md`, `docs-site/user-guide/online-learning/index.md`, `docs-site/api/guides/training-datasets.md`, `docs-site/dev/concepts/training-datasets.md`, `docs/adr/0080-model-lineage-and-decisions.md` and ADR index; this plan, backlog README and roadmap links. Generated web types are local ignored artifacts.

## Verification and acceptance gates

- Run DB tests through `pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_model_lineage.py tests/test_trained_models.py tests/test_training_datasets.py tests/test_training_runs.py -q'`. Launcher checks disposable `_test` target. Test DB migrations and downgrade/upgrade where safe, FK guards before storage deletion, missing/foreign/mismatched references, mixed clone/resume, deep lazy ancestry, deterministic pagination, request replay/conflict, archived current visibility, decision/publication concurrency and unchanged serving state.
- Web targeted Vitest covers exact historical attempt, off-page candidate, return path and archive state, late reads/writes/auth ABA. Run typecheck, lint, CSS token lint, Python lint/format and diff checks. Export/check OpenAPI, codegen, SDK manifest/contract compatibility checks.
- Apply migration only through verified worktree runtime; refresh only owned processes if needed. Browser checks real retained native training origins plus task-owned candidates/notes: exact model/run/attempt/data/back, decision persistence and same release generation, archive filters, refresh, keyboard, 1440px and 390px light/dark, API/console evidence. Automated regressions cover archived-current visibility, clone/resume/legacy gaps, bounded deep ancestry, pagination, and concurrency. Native training/inference is unchanged; do not rerun GPU training for this node.
- Build docs; check ADR/plan freshness and changed links. Remove owned test infrastructure, screenshots, caches and temporary candidates/notes while retaining user's existing native runs/data and append-only audit records.

## Rollback

Hide new navigator and decision controls, stop decision writes, retain decision/provenance rows and existing releases. Migration is additive; do not downgrade away notes or source references on a populated environment. Existing model endpoints remain compatible with optional/default metadata and the list filter. No new dependency, service, configuration or secret is required. One commit contains this node after acceptance; no push or release is authorized.

## Outcome

- Delivered in this node's single commit, with no release/version assignment.
- Added bounded project-owned provenance projections, explicit missing/inconsistent
  source gaps, one-section keyset pagination, and attributable retained prediction
  counts. Read navigation performs no storage/executor/reconciliation operation.
- Added append-only, idempotent candidate decisions and archive filters. Candidate
  detail and exact historical run/attempt/data links survive refresh and return
  navigation. Decision, model and training views fence stale location/session work.
- Local verification: 59 distinct API tests, 45 relevant web tests, and 4 SDK
  contract tests passed. Final migration applied on a fresh disposable database;
  downgrade to 0183 and upgrade to 0184 passed. Ruff/format, web typecheck, ESLint,
  CSS tokens, OpenAPI/codegen/coverage checks, docs build, ADR and freshness checks
  passed. CSS lint retains three existing comparison-component overlay warnings.
- Browser acceptance used retained native Ultralytics training sources and two
  task-owned real candidates. Exact historical attempt and frozen dataset detail
  round trips, keyboard decision submission, persisted notes, archive-only/default
  lists, refresh, 1440px/390px and light/dark layouts passed. Feature requests
  returned 200 and no browser errors were observed. Clone/resume/gaps, pagination,
  archived-current visibility, concurrency and ABA races are automated evidence;
  they are not claims of live browser race reproduction.
- Two candidates and three temporary decisions were removed; original weights,
  two native runs, one dataset and twelve tasks were retained. Release selection
  stayed at generation 0 with no current release. Disposable test infrastructure,
  owned auth/browser session, screenshots, caches and build outputs were removed;
  append-only audit records remain. No native training/inference rerun was needed.
- Updated user/API/architecture documentation, README, changelog and SDK endpoint
  coverage. The three navigator/decision endpoints are explicitly web-only; no SDK
  release was made. Remaining series work proceeds with a fresh T10 audit.
