# T13 — Qualified LibreYOLO managed model return

> Status: completed
> Authorization: the user requested sequential fresh review and implementation, one commit per node.
> Release milestone: Not yet determined
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Fresh review and promotion evidence

The worktree is clean at `8edee77d9`; T01–T12 are committed. Existing managed models already provide authenticated installation, immutable artifacts/classes, exact-instance routing, trial, manual releases, rollback and provenance. This node extends those owners; it adds no service, database migration, deployment variable, account or automatic release.

The promotion trigger passed against the retained successful LibreYOLO experiment: native `1.6.0` best weights SHA256 `65848d71bc66cbce5806b3aa870b5caab40ac9ea9a69e14a494b80cadcdafe1e`, ordered classes `car, person`, YOLO9-t detection. CPU loading checked all 1,338 main tensors exactly; 432 extra tensors are native auxiliary training heads. Non-square native inference returned 61 detections at 320×240 and 60 at 176×112 with valid geometry/classes/scores. These low-confidence results prove protocol behavior, not quality.

The same probe passed in an ephemeral virtual environment over the actual controlled backend's existing Torch `2.14.1+cu132`, torchvision `0.29.1+cu132`, Ultralytics `8.4.177`, Pillow, OpenCV and NumPy. Only LibreYOLO and its additional pinned dependencies were installed there, and the probe environment was removed. Before/after dependency checks had the identical pre-existing `spin` / `click` conflict; no new conflict was introduced. Preserve the existing common dependency pins. Source: [official native model documentation](https://www.libreyolo.com/docs/models/yolov9) and [pinned release source](https://github.com/LibreYOLO/libreyolo/tree/809009bc294daa6e75e0976ab7cfdd0057d17d79).

## Decisions and contracts

- Extend the existing backend with a dedicated native adapter. Qualified identity is LibreYOLO `1.6.0`, YOLO9 `t/s/m/c`, detection, schema `1.0`, native tensor state-dict format, and the pinned CPU runtime. Native YOLO9 has no E configuration; E, P2, E2E, quantized and other task/format variants remain unqualified. Validate each size with native construction/load/output fixtures before claiming it qualified.
- A stdlib shared qualification helper owns the immutable identity and configuration eligibility. GET training-run detail always computes a typed `registration_eligibility` (optional in the additive response schema for older consumers); registration recomputes the same predicate. The UI enables registration from the server response and still requires a successful attempt and full project class bindings. Missing eligibility fails closed. Historical training configurations remain unchanged.
- Download only the unique verified `weights` artifact. LibreYOLO registration validates the final report through the existing evaluation parser; native precision/recall aliases and missing per-class results remain unavailable. Preserve Ultralytics historical report/receipt behavior.
- Native loading first uses `torch.load(weights_only=True)` and raw strict metadata checks, because the native loader tolerates missing tensors and fills missing class names. Check original names without coercion, all main keys/shapes/loaded tensor values, finite tensors, and only known auxiliary-head extras. Use the native runner owner for device state; this qualification serves LibreYOLO on CPU.
- Wrap the single native Results object without indexing away detections. Validate original image dimensions, exact ordered names, complete aligned boxes/confidence/class arrays, finite pixel coordinates, integral in-range class indexes and valid confidence. Preserve the checkpoint's supported image size and letterbox placement, then reuse the existing percent-coordinate emitter once.
- LibreYOLO installation receipts add family, size, artifact format, artifact byte size and pinned runtime qualification. Existing Ultralytics receipts and the three-field prediction wire identity remain unchanged. Actual installation and reload independently verify the qualification. Pool keys add the verified receipt digest, so framework/version/family/runtime and class order participate without changing public request identity or pool ownership.
- Keep exact selected instance/version routing, auth, health, binding and worker revalidation. No fallback to another framework, builtin model or service-pool member. Existing pending/draft preannotation and workbench editing guards remain mandatory.
- T08/T12 shared sample evaluation stays Ultralytics-only. Reject LibreYOLO before profile/request receipt admission, and disable it with an explanation in both creation UIs. Native final training metrics do not become a shared evaluator by relabeling.

```text
Retained successful run + exact best weights + verified final report
  -> shared eligibility -> immutable model/classes/bindings
  -> authenticated native CPU install -> receipt -> isolated pool lease
  -> fixed-sample trial -> human publication -> exact instance prediction
  -> persisted model provenance -> ordinary review / human rollback
```

## Exact implementation scope

This is a large change across shared contracts, inference, API, React UI and documentation (more than eight files and three components). Root owns all writes; read-only agents audit native semantics and API compatibility.

- Shared: `apps/_shared/training_contract/training_contract/model_return.py`, `catalog.py`; `tests/test_model_return.py`.
- Inference: `apps/yolo-backend/{pyproject.toml,uv.lock,requirements.lock,Dockerfile,export_requirements.py,managed_models.py,libreyolo_adapter.py,main.py,schemas.py,README.md}`; `tests/{test_managed_models.py,test_libreyolo_adapter.py,test_sample_evaluations.py}`. Extend existing native test coverage only where needed.
- API: `apps/api/app/{services/trained_models.py,services/managed_inference.py,schemas/training_run.py,api/v1/training_runs.py}`; `apps/api/tests/{test_trained_models.py,test_training_runs.py,test_sample_evaluations.py,test_active_learning.py,test_training_evaluations.py}`. Existing prediction/routing owners change only if fresh acceptance exposes an implementation mismatch; revise this scope before editing them.
- Web: `apps/web/src/api/{trainingRuns.ts,trainedModels.ts}`; `pages/Projects/{ProjectModelsPanel.tsx,ProjectModelsPanel.test.tsx}`; `pages/Training/{SampleEvaluationCreate.tsx,SampleEvaluationCreate.test.tsx,ActiveLearningCreate.tsx,ActiveLearningCreate.test.tsx}`.
- Generated contracts: `apps/api/openapi.snapshot.json`, `apps/web/src/api/generated/{types.gen.ts,sdk.gen.ts}`, API/SDK generated inventories and docs via existing generators (inspect actual generated diff).
- Documentation: this plan, `docs/plans/backlog/{README.md,1792035920_online-learning-experience-roadmap.md}`, the T12/T08 references to this promoted plan, `docs/adr/archive/0084-qualified-libreyolo-model-return.md`, `docs/adr/README.md`, `docs-site/{user-guide/online-learning/index.md,api/guides/training-datasets.md,dev/concepts/training-datasets.md}`, `README.md`, `DEV.md`, `.env.example`, generated environment reference, `CHANGELOG.md`.

## Dependencies, compatibility and rollback

Shared helper precedes API eligibility/receipt and native adapter. Pin LibreYOLO `1.6.0` in the backend lock, retaining existing package versions; export hashed requirements using `export_requirements.py`. Add the shared contract package and adapter to the existing Docker build. No database schema change: qualification is retained in existing provenance/receipt JSON. Rebuild only the owned backend; refresh mounted API/workers and verify active signatures. Do not touch primary-checkout services or symlink targets.

Old Ultralytics install bodies/receipts and public prediction wire remain identical. Existing historical reports remain readable without a current inference runtime. A rebuilt backend changes the T08 runtime source identity, so new evaluations must obtain its new qualification; never rewrite historical evidence. Rollback stops new LibreYOLO registrations/installs and explicitly rolls active releases back while preserving all experiments, weights, models, deployments, releases and predictions. Do not disable a backend still serving retained bindings.

## Verification and acceptance gates

- Shared/backend unit tests cover eligible/foreign identities, every qualified native size, raw malformed/sparse/foreign names, missing/extra/shape-invalid main tensors, allowed auxiliary tensors, corrupt/foreign artifacts, exact byte/SHA checks, stale receipts, native multi-box/empty/filtered/max-det output, non-square coordinates, runtime mismatch and isolated model class orders. Existing managed pool/cancellation and Ultralytics tests must pass.
- API tests run only in the verified disposable worktree test database: source status/uniqueness/hash, eligibility/detail compatibility, retained metric gaps, registration/install/trial/release/rollback, permission revocation, foreign/stale receipts, exact instance/pool mismatch and T08/T12 rejection. Run `test_pipeline_validation.py` plus the existing controlled prediction route and worker tests. Destroy the test environment afterward.
- Web: targeted Vitest for the changed screens, typecheck, lint and `pnpm --filter @anno/web lint:css-tokens`. Verify missing/foreign eligibility, native metric gaps, stale async ownership and clear evaluation restrictions.
- Contract/docs: `pnpm openapi:export`, `pnpm codegen`, affected SDK contract checks, generated API docs/inventories, `pnpm docs:gen-env-vars`, docs build and repository documentation checks. Finish with `git diff --check` and normal commit hooks.
- Real acceptance uses only the isolated development runtime and explicitly journaled resources: register retained LibreYOLO and Ultralytics sources, load/trial, verify native reload and exact receipt, manually publish prior Ultralytics then LibreYOLO, preannotate only newly created owned tasks, inspect persisted framework/weight/deployment/release provenance, and manually roll back to the prior verified release. Restore the original release state and remove only acceptance-owned data/artifacts. Native immutable request ledgers use their normal retention.
- Browser acceptance covers the normal project Models flow, fixed sample, publication confirmation, rollback, error/retry and keyboard use, desktop light/mobile dark overflow and console/API errors. Clean browser state, screenshots, test outputs and temporary model files after evidence is recorded.

## Outcome

Implemented the qualified native adapter, immutable receipt/cache identity, server-derived registration eligibility, native metric gaps and explicit shared-evaluator restrictions. The existing authenticated instance, manual publication and rollback owners remain in use; no migration or new service was added.

Verification passed: 9 shared qualification tests; 224 backend tests plus 77 native/managed/evaluator checks; API groups of 104 and 62 checks plus the strengthened native-metrics check in the disposable database; 19 final targeted web tests; 4 SDK contract checks; Python/web lint, CSS tokens, TypeScript, OpenAPI/code generation and documentation build. Documentation checks passed for 173 pages, 84 ADRs and 508 routes from 81 modules. The disposable test environment was destroyed. Existing lint/deprecation warnings remain; remote CI was not run.

In the isolated development browser, both retained training sources registered and passed installation/trial. The native fixed sample correctly returned no detections at the trial threshold; its image loaded and unavailable native metrics remained explicit. A human-confirmed Ultralytics publication was followed by a human-confirmed LibreYOLO publication. Ordinary queued preannotation on two acceptance-owned images produced 61 and 59 valid boxes at threshold 0.001, with exact persisted framework, weight SHA, deployment, registry and release provenance. Human rollback restored the preceding Ultralytics release while those predictions retained their original identities. These deliberately weak model scores prove workflow behavior, not model quality.

Desktop light/mobile dark checks showed no horizontal overflow; keyboard navigation, a simulated read failure followed by recovery, and the disabled LibreYOLO shared-evaluation option were verified. After the simulated failure was removed, browser/console checks had no unexpected errors. Acceptance data, predictions, releases, managed model cache entries, uploads and temporary outputs were removed; the original project retained its 12 tasks, two experiments and unpublished state. The rebuilt qualified CPU backend remains healthy.

One T13 commit contains this node; no push or release is requested. T14 remains an independent fresh review.
