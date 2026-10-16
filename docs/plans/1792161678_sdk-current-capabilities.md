# SDK capability alignment with the current application

> Status: implemented and locally verified on 2026-10-16. Release, push, and publication remain outside this task.
> Baseline: `47197fd3b1defaeb1bd8da728afd6118f7bd40a1` (2026-10-16).

## Goal and verified baseline

Update the existing synchronous Python SDK and its CLI to expose the current public online-learning workflows. Keep the TUI a small monitoring surface. Preserve existing public methods, error handling, authentication, and optional installation extras.

The worktree branch is `feat/sdk-update`, initially clean. Read-only `git ls-remote origin refs/heads/main` returned the same commit as local HEAD and local `main`. This plan targets that immutable commit; it does not assume future commits are compatible.

| Evidence                                            | Observed state                                                           |
| --------------------------------------------------- | ------------------------------------------------------------------------ |
| `apps/api/openapi.snapshot.json`                    | Application API baseline `0.26.1`; 568 HTTP operations                   |
| `packages/python-sdk/src/ai_annotation/_version.py` | SDK `0.20.0`; AAP target `0.24.0`                                        |
| `packages/python-sdk/api-coverage.toml`             | 326 watched operations: 89 covered, 237 excluded, no planned entries     |
| `packages/python-sdk/src/ai_annotation/client.py`   | 101 distinct HTTP request operations; all exist in the current snapshot  |
| Existing call sites outside watched tags            | 12: dashboard (5), API keys (5), current user (1), prediction import (1) |
| Public learning/training operations                 | 95 across ten tags; all currently excluded, mainly as `web-only`         |
| Trusted executor protocol                           | Ten operations; intentionally outside the public SDK                     |

The other 242 snapshot operations are outside the current watched tags. They are not automatically SDK defects. Workbench internals, account administration, storage connectors, and unrelated product surfaces remain outside this capability update.

Relevant decisions: [existing SDK alignment](archive/2026-08-14-v0.16.0-v0.20.0-[SDK]-sdk-cli-tui-capability-catch-up.md), [controlled models](../adr/archive/0076-controlled-trained-model-identity.md), [model lineage](../adr/0080-model-lineage-and-decisions.md), [canonical tasks](../adr/0086-canonical-task-data-contracts.md), and [task dataset streams](../adr/archive/0087-project-task-dataset-streams.md). The older plan's release assignments and acceptance counts are historical, not this plan's target or validation evidence.

## Chosen scope

Use the existing handwritten resource namespaces, `HttpTransport`, Pydantic models, Typer commands, and Textual views. Add the 95 public operations below, meaningful request/response tests, CLI entry points, and documentation together. This necessarily touches more than eight files; the scope spans the client, models/exports, command modules, contract tests, workflow tests, TUI, and official documentation.

The minimal option is compatibility fixes plus training-dataset creation and training-run submission. It leaves evaluation, model return, policies, studies, and active learning unavailable to automation, so it is a useful stopping point after phase B but does not satisfy the full requested alignment.

Do not generate a second client from OpenAPI, add backend/framework dependencies, split SDK/CLI/TUI into separate packages, rewrite existing namespaces, or reproduce the web configuration workspace in the terminal. No application version bump, migration, executor deployment, GPU installation, package publication, or release number assignment belongs to this work.

The existing ownership flow stays intact:

```text
Python caller / CLI / TUI
           |
    Client namespaces -> HttpTransport -> AAP API -> Existing workers/backends
                              |
                       Presigned storage
                        (without auth)
```

## Public interface decisions

All project resources take explicit `project_id` first, then resource IDs, with `IdLike = str | UUID`. New create/write methods take a typed request object; the CLI reads the same object from a JSON file. Methods with query parameters expose the current named query arguments rather than opaque query dictionaries. Paths, request schemas, and query bounds come from the pinned OpenAPI snapshot and corresponding handlers.

| Namespace                   | New methods                                                                                                                                                                                              | HTTP operations | Phase |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------: | ----- |
| `client.training`           | `catalog`, `projects`, `runs`                                                                                                                                                                            |               3 | B     |
| `client.task_datasets`      | `list`, `unrecognized_versions`                                                                                                                                                                          |               2 | B     |
| `client.training_datasets`  | `preflight`, `create`, `list`, `get`, `retry`, `download`, `delete`, `insights`, `classes`, `compare`, `samples`, `sample`, `download_media`                                                             |              13 | B     |
| `client.training_runs`      | `catalog`, `parameter_schema`, `initialization_source`, `preflight`, `create`, `list`, `get`, `events`, `evaluation`, `telemetry`, `logs`, `download_artifact`, `recovery`, `cancel`, `resume`, `delete` |              16 | B     |
| `client.training_recipes`   | `list`, `create`, `get`, `revisions`, `create_revision`, `resolve`, `clone_candidate`, `archive`                                                                                                         |               8 | B     |
| `client.training_previews`  | `capabilities`, `create`, `get`, `download_image`, `cancel`, `delete`                                                                                                                                    |               6 | B     |
| `client.trained_models`     | `create`, `list`, `get`, `download`, `deploy`, `lineage`, `decisions`, `decide`                                                                                                                          |               8 | C     |
| `client.model_releases`     | `list`, `create`                                                                                                                                                                                         |               2 | C     |
| `client.learning_policies`  | `get`, `save`, `check`, `disable`, `delete`, `triggers`                                                                                                                                                  |               6 | C     |
| `client.sample_evaluations` | `qualification`, `create`, `list`, `get`, `compare`, `report`, `samples`, `source`, `download_image`, `cancel`, `retry`, `delete`                                                                        |              12 | C     |
| `client.training_studies`   | `preview`, `create`, `list`, `get`, `cancel`                                                                                                                                                             |               5 | D     |
| `client.active_learning`    | `pool`, `preview`, `create`, `list`, `get`, `report`, `candidates`, `download_image`, `decisions`, `decide`, `feedback`, `create_feedback`, `cancel`, `retry`                                            |              14 | D     |

`download*` methods return the written `Path`; deletes return `None`. JSON methods return typed models or typed page/detail envelopes matching the actual response. A `202` returns the submitted resource immediately. Do not hide polling, resource creation, deployment, policy enabling, or publication behind another call.

Keep existing resource implementations in `client.py` / `models.py`, with additions to top-level exports. During implementation, the 95 operations and their request/response contracts are assigned to separate training, model-management, and learning-automation modules. This keeps each domain independently reviewable and avoids concurrent modifications to the existing client; `Client` remains the single public aggregate and transport owner. No runtime-generated wrappers or second transport are introduced.

### Requests, frozen identities, and validation

- Use canonical public `task` values `detect` / `segment` for qualified training workflows. Keep `task`, `format`, `media_type`, and `source_media_type` distinct. Treat historical config, provenance, manifests, and digests as opaque evidence; never normalize or rehash their contents in the SDK. Do not claim video or additional-task training support from a generic task enum.
- Keep `dataset_id` on training requests explicitly documented as a frozen training-version UUID, distinct from uploaded storage dataset IDs and `task_dataset_id`. Preserve nullable task-stream membership and non-contiguous `version_ordinal`; never substitute an ordinal for a UUID.
- Request objects forbid unknown fields and preserve important bounds, finite numbers, and cross-field rules. Dynamic configuration `values` remains JSON data validated against the server's selected parameter schema; do not hardcode the parameter catalog in the SDK.
- Serialize optional request fields with `exclude_unset=True`, JSON mode, and aliases. Do not send default legacy `parameters` alongside an explicit `configuration`. Preserve explicit nulls where accepted, and let the API own legacy serialization and digest construction.
- Preflight/preview is explicit. Creation carries the returned digest and the caller's stable `request_id`. Do not invent a new ID when a caller repeats a request or silently redo a failed preflight. Same ID with changed payload must preserve the server's conflict response.
- Recipe revisions and archive keep `expected_head_revision`; policies keep `expected_revision`; release creation keeps `expected_generation`. Policy disable and recipe archive send their required revision in the query, not a JSON body. A study uses `base.request_id`, not a second outer request ID. Model/active-learning decisions are append-only and request-idempotent; do not invent a decision-sequence compare-and-swap requirement.
- Requests validate local shape; the API remains authoritative for permissions, parent/child relationships, data freshness, initialization eligibility, budgets, task qualification, and publication. The SDK must retain structured server `detail` and error codes.

### Responses and pagination

Reuse `_AAPModel` and `extra="allow"` for responses. Required identity fields remain required; only genuinely optional fields get defaults. Preserve server extension fields in model dumps.

Do not force every response into the current `Page[T]`: it requires `items`, `total`, `limit`, and `offset`, while several training pages provide only `items` and `total`; active-learning decision/feedback pages provide `items` without a total. Use small explicit envelopes for these real shapes. Task-stream output includes pending-membership and unrecognized-version counts and nullable stream IDs. Model detail is `{model, deployments}`; the model contains current-release metadata rather than an embedded release object. Run detail is `{run, attempts, registration_eligibility}`; retain the registration qualification gate as a typed field.

Telemetry and logs use sequence cursors, not offsets. Logs accept `after` or `before`, never both. Model decisions use `before` with a maximum page size of 20. Frozen sample comparison retains `source_version_id`, particularly when removed samples belong to the baseline. Never fetch every page to produce a single list call.

Training states are domain states, including `succeeded`, `cancelling`, and `lost`; they must not be interpreted using the async-job `completed` terminal set. Cancellation requests may return `cancelling` while execution stops. Return current attempt IDs, recovery reasons, qualification reasons, budget accounting, unavailable metrics, and frozen evaluation identities without manufacturing success or comparability. Run-list reads can trigger the API's lost-executor reconciliation; do not promise those GETs have no server-side state effects.

### Transport and downloads

Reuse `HttpTransport.request` and `stream_download`. Keep retries restricted to idempotent GET requests. No automatic retries for submission, deployment, decisions, policy checks, resume, or publication.

Downloads have two distinct current forms: authorized API calls returning short-lived absolute storage URLs, and project-scoped binary image/media routes. Use the bare client only for the former. For fixed binary routes, pass the full relative `/api/v1/...` path through the authenticated streaming path; do not buffer large artifacts in `.content`. Query-bearing media downloads need a small extension to `stream_download` accepting query parameters, rather than manual query-string assembly or a second transport.

Before extending downloads, make the shared streaming write atomic with a temporary sibling file and replacement only after successful completion; clean the temporary on errors. This prevents an interrupted download from corrupting an existing destination and benefits existing export downloads too. Accept optional expected SHA-256 and size metadata in the shared helper and validate them while streaming, before replacement. Training-package/model/artifact downloads pass the descriptor's available metadata; redacted log artifacts use their own returned descriptor, not the original log hash. Test relative authentication, presigned header isolation, query parameters, chunked content, hash/size mismatch, HTTP errors, interrupted streams, and destination preservation. Do not add automatic URL-refresh retries.

Extend the contract extractor narrowly to recognize fixed relative literal/f-string calls to `self._http.stream_download` as GET operations. Runtime presigned URL variables are not fixed API operations. Keep unsupported dynamic API-path expressions rejected and cover the new extractor branch with focused tests.

### CLI

Add one `aap learning` Typer group, with `training`, `task-datasets`, `datasets`, `runs`, `recipes`, `previews`, `models`, `releases`, `policies`, `evaluations`, `studies`, and `active-learning` subgroups matching the namespace table. Method underscores become command hyphens. Existing `aap datasets`, `jobs`, and ML operations retain their meaning and arguments.

Project operations require `--project`; resource IDs remain positional. Complex writes take `--body <path>` containing the typed JSON request, including explicit request IDs/digests/revisions/budgets. Downloads take `--out <path>`. Expose named query options from each selected operation, including pagination. No new environment variables or interactive configuration wizard is needed.

Reuse `get_client`, `cli_errors`, `print_json`, and `confirm_destructive`. Put one reusable JSON-file reader in `_output.py`, handling file errors, malformed/non-object JSON, and Pydantic errors as concise CLI failures without HTTP mutations. Keep successful `--json` output undecorated; failures remain one-line stderr and nonzero exit, preserving the existing contract rather than claiming JSON error objects.

Require confirmation for delete, cancel, retry, resume, archive, deploy, release changes, policy save/check/disable, study creation, active-learning creation, and human decisions/feedback submission. JSON mode requires explicit `--yes`; refusal must issue no mutation request. Expose preview/preflight separately before spending training/inference budgets. Do not infer human acceptance or silently enable an automation policy.

### TUI

Add a lazy Training tab with the global queue and project filtering, pagination, refresh, and run detail. Detail shows attempts, status, metrics, qualification/recovery reasons, and bounded telemetry/log pages. Use only the new SDK methods; keep existing jobs independent. Refresh only the visible training view and coalesce slow refreshes, retaining the last successful data and per-view errors.

For unauthorized subjects, show the server's permission result only when the tab is opened. TUI startup remains projects/jobs/current-user only. Keep training creation/configuration, deployment, publication, policies, studies, and human candidate decisions in CLI/Web; no terminal form suite or preview image viewer is planned.

## Independently mergeable phases

### A — Existing compatibility and complete call-site accounting

1. Preserve all 101 current operations and existing positional/keyword behavior. Add optional list filters through the SDK and their existing CLI commands: Projects (`type_key`, `data_type`, `member_id`, `project_role`, `created_from`, `created_to`), Datasets (`has_scenes`), Tasks (`assignee_id`, `batch_id`, `unbatched`, `reject_reason_type`, `class_name`), Annotations (`video_segment_id`), Jobs (`search`), and Feedbacks (`root_only`, `include_counts`, both default false). Add optional `total` / `status_counts` to `FeedbackPage`. Current server support is in the corresponding GET handlers/OpenAPI; old callers retain existing defaults. Test omitted and explicit false/empty filter values, UUID/date serialization, filtered feedback counts, empty pages, and errors; update the obsolete SDK statement that feedback pages have no total. These are additive gaps, not broken existing routes. Preserve the explicitly documented API-key expiry-null exclusion; changing `expires_in_days=None` from omission to expiry removal would break old callers.
2. Add watched tags `dashboard`, `api-keys`, `auth`, and `predictions`. Classify their 37 operations: the 12 existing call sites become covered; the remaining 25 retain explicit exclusion reasons. Require every fixed SDK API operation to belong to the manifest, not only the intersection with watched tags. Add the narrowly defined `executor-only` exclusion reason to the guard and use it for the ten internal training operations instead of `web-only` / `no-external-demand`.
3. Add download call-site accounting and safe streaming behavior described above. Keep model/schema assertions focused on selected request/response shapes, not a whole-schema fingerprint framework.
4. After the existing SDK/CLI/TUI suite passes against this commit, update the independent AAP compatibility target and manifest snapshot to `0.26.1`. Preserve SDK release version `0.20.0`; a new release number requires a maintainer's instruction. Preserve the documented rule that later application version bumps alone do not fail the SDK guard; do not add permanent equality between the target and OpenAPI `info.version`.

Acceptance: manifest total 363, covered 101, excluded 262 before new capability methods. Existing workflows and extras still work. No new training method is labeled covered yet.

### B — Frozen data, training submission, and observation

Add the 48 operations for training discovery, task dataset streams, training versions/insights, runs, recipes, and augmentation previews, with corresponding CLI subgroups and typed models/exports. The full request flow is catalog/schema → data preflight/create → frozen version → resolved configuration/run preflight → submission → explicit status/attempt reads → artifact download. Initialization-source checks are included; no automatic fallback to catalog weights is allowed.

Acceptance: create and observe a frozen version/run using mocked boundary responses; resolve recipe/clone inputs; read events/logs/recovery; stream media/artifacts; validate failures and exact request bodies. A create-method test must enforce the fresh preflight digest even though the schema shared with preflight allows it to be absent. Covered count reaches 149 at the pinned baseline. The SDK remains usable if later model/automation phases never ship.

### C — Evaluation, controlled model return, and policies

Add 28 operations for sample evaluations, model versions/deployments/lineage/decisions, releases, and learning policies, plus CLI and documentation. Model registration, deployment, and project release remain separate operations. Expose release rollback through the existing create schema's `rollback_from_id`, with expected generation; do not invent another rollback endpoint.

Acceptance: evaluation creation/report/sample-source flow; model qualification/registration/deployment; release conflict and rollback requests; policy missing-value/read/save/revision conflict/disable/trigger history. An archived/candidate decision must not silently switch the current release. Preserve deletion reference guards and their 409 responses rather than promising cascading removal. Covered count reaches 177.

### D — Bounded studies and human-reviewed active learning

Add 19 operations for study preview/create/list/detail/cancel and active-learning pool/preview/rounds/candidates/report/decisions/feedback/retry/cancel, plus CLI and documentation. Budgets and explicit candidate IDs are required inputs. Keep model selection, human decisions, random-control evidence, and subsequent evaluations explicit.

Acceptance: exact preview replay, changed-digest rejection, duplicate IDs/ordinals, strict integer budgets, finite metrics, maximum bounds, insufficient review budget, stale or foreign attempts, cancelled/lost states, immutable decisions, factual feedback, and no automatic promotion or policy enabling. Final covered count reaches 196; excluded count is 167 across 363 watched operations. All 95 selected public learning operations are covered; all ten trusted executor operations remain excluded.

### E — Bounded TUI monitor

Add the Training tab and run detail after phase B; it can merge independently of C/D. Verify lazy requests, pagination, slow refresh coalescing, per-view errors, stale data retention, authorization failure, attempt changes, and terminal resizing. This phase adds no new HTTP operations.

Acceptance: Textual Pilot checks at 80×24 and 120×32, followed by an actual terminal check of navigation, Chinese text, offline/recovery behavior, and readable bounded logs. No training write is required to open or refresh the TUI.

Each phase updates its own tests, SDK reference/CLI/cookbook pages, package changelog, and relevant root Unreleased entry. Unimplemented endpoints retain their current exclusion until their actual call sites and tests land; this plan records intended scope without assigning future release numbers to manifest `planned` entries.

## Files and documentation

| Area                      | Change locations                                                                                                                                                                                                                                                                                                                            |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Public SDK                | `packages/python-sdk/src/ai_annotation/{client.py,models.py,__init__.py,_version.py}`; `training.py`, `model_management.py`, `learning_automation.py` and their model modules                                                                                                                                                               |
| Transport and CLI         | `_http.py`, `cli/{main.py,_output.py}`; new `cli/learning/__init__.py` and domain command modules under `cli/learning/`                                                                                                                                                                                                                     |
| TUI                       | `packages/python-sdk/src/ai_annotation/tui/app.py`                                                                                                                                                                                                                                                                                          |
| Coverage                  | `packages/python-sdk/api-coverage.toml`, `tests/test_openapi_contract.py`                                                                                                                                                                                                                                                                   |
| Meaningful regressions    | Existing affected test modules; new `test_training_datasets.py`, `test_training_runs.py`, `test_training_configuration.py`, `test_trained_models.py`, `test_learning_policies.py`, `test_sample_evaluations.py`, `test_training_studies.py`, `test_active_learning.py`, `test_cli_learning.py`, `test_downloads.py`, `test_tui_training.py` |
| Official SDK docs         | `packages/python-sdk/README.md`, `docs-site/dev/sdk/{quickstart,python-client,cli,cookbook,tui}.md`                                                                                                                                                                                                                                         |
| Product entry and history | `README.md`, `docs-site/user-guide/projects/online-learning.md` (automation link/boundary), root and SDK `CHANGELOG.md`                                                                                                                                                                                                                     |
| Existing API/architecture | Current handlers/schemas and API guides remain authoritative; regenerate API artifacts only if a separately justified backend contract change is necessary                                                                                                                                                                                  |

Use `cli/learning/__init__.py` for the command group and one module per domain; do not create the ambiguous sibling `cli/learning.py`. Keep command-specific formatting small and shared infrastructure in the existing `_output.py`. Package dependencies and lockfile need no change unless a real packaging defect is found.

## Verification and implementation prerequisites

Observed tools: `uv`, `pnpm`, Python, and `agent-browser` are available. The SDK has no `.venv` in this worktree. The API has a worktree-local `.venv`; `node_modules` is local. `.env` is a symlink to the primary checkout and must not be edited or assumed to describe an isolated runtime. No dependency installation, test run, build, service start, or GPU operation was performed for this plan.

Run commands through `rtk proxy`. During implementation, initialize/use the SDK's own environment with the existing lockfile; do not install SDK dependencies into the API environment.

```sh
rtk proxy uv run --project packages/python-sdk --extra test pytest packages/python-sdk/tests/test_openapi_contract.py -q
rtk proxy uv run --project packages/python-sdk --extra test pytest packages/python-sdk/tests -q
rtk proxy uvx --from ruff==0.15.22 ruff check packages/python-sdk
rtk proxy uvx --from ruff==0.15.22 ruff format --check packages/python-sdk
rtk proxy uv build --project packages/python-sdk
rtk proxy pnpm docs:build
rtk proxy git diff --check
```

The existing CI job `Python SDK tests` in `.github/workflows/ci.yml` already runs `uv run --extra test pytest -q` and `uv build` from the package directory. Keep this owner; no new CI workflow is needed. Local success does not establish remote CI success.

Test matrix:

| Boundary                        | Required evidence                                                                                                                                                                          |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Every selected HTTP operation   | Correct method/path/IDs/query/body and representative actual-shaped response; new fields survive parsing                                                                                   |
| Authorization and relationships | 401/403/404; wrong project/version/attempt/candidate; API-key scope failure; structured server error retained                                                                              |
| Admission and conflicts         | 409 stale preview/revision/generation/runtime/qualification, 422 invalid shape/budgets, 503 unavailable executor/backend; no write retry                                                   |
| Serialization                   | Explicit-vs-unset/default/null handling, configuration without legacy parameters, unchanged supplied digests, canonical task output                                                        |
| Pages and observations          | Empty and nonempty pages, no total, cursor bounds, before/after conflict, baseline-source sample identity, unknown extension fields                                                        |
| Lifecycles                      | 202 accepted resource, failed/cancelled/cancelling/lost states, current attempt replacement, recovery ineligibility, unavailable metrics                                                   |
| CLI                             | Human and JSON output, parse/API failure exits, no token/body traceback leakage, cancelled confirmation sends zero mutations                                                               |
| Installation                    | Wheel in clean core-only, CLI, and TUI environments; top-level import works without optional dependencies; `aap` core-only shows installation guidance; metadata/runtime/CLI version agree |
| TUI                             | Pilot state and request-count assertions plus actual terminal validation; no startup training fetch or overlapping refresh                                                                 |
| Downloads                       | Authenticated relative route versus bare presigned URL, complete streamed bytes, query forwarding, hash/size mismatch, interruption preserves destination                                  |

Live acceptance is a separate implementation prerequisite: use a verified disposable worktree runtime and owned test project/dataset. Project routes require project management authority; global training-hub calls additionally require platform role `project_admin` or `super_admin`. Credentials need the actual required `annotations:read`, `datasets:read`, and `datasets:write` scopes; project ownership alone does not satisfy the global role gate. Additional endpoint scopes remain enforced by the API. A qualified training executor and controlled inference backend are required only for live training/deployment/evaluation, not SDK unit tests. Do not request executor secrets for the public SDK.

For live acceptance, exercise one small data → run → artifact chain, one qualified model/evaluation chain, and the bounded study/active-learning paths using explicit budgets. Verify real request/response evidence and restore any test policy/release state before removing owned fixtures. Read the runtime skill before provisioning and check isolation; never use the shared development database as a disposable test database. If GPU/backend access is unavailable, report live acceptance as unverified and do not claim model quality or full production compatibility from mocked tests.

## Effort, risks, and rollback

This is a medium-to-large SDK update: 95 additional public operations plus CLI, contracts, documentation, and a small TUI view. Budget roughly two to three engineer-weeks, including contract fixtures and terminal/live acceptance; this is an estimate, not a release commitment. Phase B delivers the smallest complete training automation chain first.

The most fragile premise is that the newly merged public training routes are stable enough for a supported SDK contract. If a route is withdrawn or changes before implementation, re-pin the baseline and adjust that domain's plan, tests, and documentation explicitly. Do not implement guessed routes or claim support by changing the target string alone.

Key risks are response shapes underspecified as generic objects in OpenAPI, frozen-identity serialization, async-domain state confusion, project/key authorization, and downloads that accidentally omit authentication or overwrite existing files. Handlers, typed boundary tests, and the shared streaming fix address these directly. Higher request volume must still use server pagination and bounded observation reads; no client-side fetch-all or N+1 background polling is introduced.

SDK code can be rolled back without a database migration. Remote writes already accepted by the server are not undone by reverting an SDK commit. Cleanup/rollback of owned smoke policies and releases uses the corresponding server revision/generation APIs; preserve human decisions and audit history. No package publication or public release is part of the proposed implementation authorization.

At completion, review the full diff, record actual check results and remaining live limitations, update official docs, and append `## Outcome` with landed commits and documentation paths. Commit/push/release only when separately requested by the user.

## Pinned operation inventory

The following inventory is derived from the pinned OpenAPI snapshot. It is an implementation checklist, not a claim that the methods already exist.

### Phase B operations

| Tag               | Operation                                                                                                  | Request schema                 | Query parameters                                                                             |
| ----------------- | ---------------------------------------------------------------------------------------------------------- | ------------------------------ | -------------------------------------------------------------------------------------------- |
| training-hub      | `GET /api/v1/training/catalog`                                                                             | `—`                            | —                                                                                            |
| training-hub      | `GET /api/v1/training/projects`                                                                            | `—`                            | `search`, `limit`, `offset`                                                                  |
| training-hub      | `GET /api/v1/training/runs`                                                                                | `—`                            | `state`, `project_id`, `framework`, `limit`, `offset`                                        |
| training-datasets | `GET /api/v1/projects/{project_id}/task-datasets`                                                          | `—`                            | —                                                                                            |
| training-datasets | `GET /api/v1/projects/{project_id}/task-datasets/unrecognized-versions`                                    | `—`                            | `limit`, `offset`                                                                            |
| training-datasets | `GET /api/v1/projects/{project_id}/training-datasets`                                                      | `—`                            | `limit`, `offset`, `status`, `task`, `task_dataset_id`                                       |
| training-datasets | `POST /api/v1/projects/{project_id}/training-datasets`                                                     | `TrainingDatasetCreate`        | —                                                                                            |
| training-datasets | `DELETE /api/v1/projects/{project_id}/training-datasets/{version_id}`                                      | `—`                            | —                                                                                            |
| training-datasets | `GET /api/v1/projects/{project_id}/training-datasets/{version_id}`                                         | `—`                            | `include_manifest`                                                                           |
| training-datasets | `GET /api/v1/projects/{project_id}/training-datasets/{version_id}/download`                                | `—`                            | —                                                                                            |
| training-datasets | `GET /api/v1/projects/{project_id}/training-datasets/{version_id}/insights`                                | `—`                            | —                                                                                            |
| training-datasets | `GET /api/v1/projects/{project_id}/training-datasets/{version_id}/insights/classes`                        | `—`                            | `limit`, `offset`, `search`, `order_by`, `split`                                             |
| training-datasets | `GET /api/v1/projects/{project_id}/training-datasets/{version_id}/insights/compare`                        | `—`                            | `baseline_id` (required)                                                                     |
| training-datasets | `GET /api/v1/projects/{project_id}/training-datasets/{version_id}/insights/samples`                        | `—`                            | `limit`, `offset`, `split`, `class_index`, `content_sha256`, `kind`, `baseline_id`, `change` |
| training-datasets | `GET /api/v1/projects/{project_id}/training-datasets/{version_id}/insights/samples/{task_id}`              | `—`                            | `limit`, `offset`                                                                            |
| training-datasets | `GET /api/v1/projects/{project_id}/training-datasets/{version_id}/insights/samples/{task_id}/media`        | `—`                            | `preview`                                                                                    |
| training-datasets | `POST /api/v1/projects/{project_id}/training-datasets/{version_id}/retry`                                  | `—`                            | —                                                                                            |
| training-datasets | `POST /api/v1/projects/{project_id}/training-datasets:preflight`                                           | `TrainingDatasetSelection`     | —                                                                                            |
| training-runs     | `GET /api/v1/projects/{project_id}/training-runs`                                                          | `—`                            | `limit`, `offset`                                                                            |
| training-runs     | `POST /api/v1/projects/{project_id}/training-runs`                                                         | `TrainingRunCreate`            | —                                                                                            |
| training-runs     | `GET /api/v1/projects/{project_id}/training-runs/catalog`                                                  | `—`                            | —                                                                                            |
| training-runs     | `GET /api/v1/projects/{project_id}/training-runs/initialization-sources/{model_id}`                        | `—`                            | `framework` (required), `base_model` (required), `dataset_id`                                |
| training-runs     | `GET /api/v1/projects/{project_id}/training-runs/parameter-schema`                                         | `—`                            | `framework` (required), `base_model` (required)                                              |
| training-runs     | `DELETE /api/v1/projects/{project_id}/training-runs/{run_id}`                                              | `—`                            | —                                                                                            |
| training-runs     | `GET /api/v1/projects/{project_id}/training-runs/{run_id}`                                                 | `—`                            | —                                                                                            |
| training-runs     | `GET /api/v1/projects/{project_id}/training-runs/{run_id}/attempts/{attempt_id}/artifacts/{name}/download` | `—`                            | —                                                                                            |
| training-runs     | `GET /api/v1/projects/{project_id}/training-runs/{run_id}/attempts/{attempt_id}/evaluation`                | `—`                            | `offset`, `limit`                                                                            |
| training-runs     | `GET /api/v1/projects/{project_id}/training-runs/{run_id}/attempts/{attempt_id}/logs`                      | `—`                            | `after`, `before`, `limit`                                                                   |
| training-runs     | `GET /api/v1/projects/{project_id}/training-runs/{run_id}/attempts/{attempt_id}/telemetry`                 | `—`                            | `after`, `limit`                                                                             |
| training-runs     | `POST /api/v1/projects/{project_id}/training-runs/{run_id}/cancel`                                         | `—`                            | —                                                                                            |
| training-runs     | `GET /api/v1/projects/{project_id}/training-runs/{run_id}/events`                                          | `—`                            | `attempt_id`, `after`, `latest`                                                              |
| training-runs     | `GET /api/v1/projects/{project_id}/training-runs/{run_id}/recovery`                                        | `—`                            | —                                                                                            |
| training-runs     | `POST /api/v1/projects/{project_id}/training-runs/{run_id}/resume`                                         | `—`                            | —                                                                                            |
| training-runs     | `POST /api/v1/projects/{project_id}/training-runs:preflight`                                               | `TrainingRunCreate`            | —                                                                                            |
| training-recipes  | `GET /api/v1/projects/{project_id}/training-recipes`                                                       | `—`                            | `limit`, `offset`, `include_archived`                                                        |
| training-recipes  | `POST /api/v1/projects/{project_id}/training-recipes`                                                      | `TrainingRecipeWrite`          | —                                                                                            |
| training-recipes  | `GET /api/v1/projects/{project_id}/training-recipes/clone/{run_id}`                                        | `—`                            | `attempt_id`                                                                                 |
| training-recipes  | `GET /api/v1/projects/{project_id}/training-recipes/{recipe_id}`                                           | `—`                            | `revision_id`                                                                                |
| training-recipes  | `POST /api/v1/projects/{project_id}/training-recipes/{recipe_id}/archive`                                  | `—`                            | `expected_head_revision` (required)                                                          |
| training-recipes  | `GET /api/v1/projects/{project_id}/training-recipes/{recipe_id}/revisions`                                 | `—`                            | `limit`, `offset`                                                                            |
| training-recipes  | `POST /api/v1/projects/{project_id}/training-recipes/{recipe_id}/revisions`                                | `TrainingRecipeWrite`          | —                                                                                            |
| training-recipes  | `POST /api/v1/projects/{project_id}/training-recipes:resolve`                                              | `TrainingConfigurationResolve` | —                                                                                            |
| training-previews | `POST /api/v1/projects/{project_id}/training-previews`                                                     | `TrainingPreviewCreate`        | —                                                                                            |
| training-previews | `GET /api/v1/projects/{project_id}/training-previews/capabilities`                                         | `—`                            | —                                                                                            |
| training-previews | `DELETE /api/v1/projects/{project_id}/training-previews/{preview_id}`                                      | `—`                            | —                                                                                            |
| training-previews | `GET /api/v1/projects/{project_id}/training-previews/{preview_id}`                                         | `—`                            | —                                                                                            |
| training-previews | `POST /api/v1/projects/{project_id}/training-previews/{preview_id}/cancel`                                 | `—`                            | —                                                                                            |
| training-previews | `GET /api/v1/projects/{project_id}/training-previews/{preview_id}/images/{index}`                          | `—`                            | —                                                                                            |

### Phase C operations

| Tag                | Operation                                                                                                             | Request schema           | Query parameters                                                              |
| ------------------ | --------------------------------------------------------------------------------------------------------------------- | ------------------------ | ----------------------------------------------------------------------------- |
| trained-models     | `GET /api/v1/projects/{project_id}/model-lineage/{kind}/{node_id}`                                                    | `—`                      | `relation`, `cursor`, `limit`                                                 |
| trained-models     | `GET /api/v1/projects/{project_id}/model-releases`                                                                    | `—`                      | `limit`, `offset`                                                             |
| trained-models     | `POST /api/v1/projects/{project_id}/model-releases`                                                                   | `ModelReleaseCreate`     | —                                                                             |
| trained-models     | `GET /api/v1/projects/{project_id}/trained-models`                                                                    | `—`                      | `limit`, `offset`, `archive`                                                  |
| trained-models     | `POST /api/v1/projects/{project_id}/trained-models`                                                                   | `TrainedModelCreate`     | —                                                                             |
| trained-models     | `GET /api/v1/projects/{project_id}/trained-models/{version_id}`                                                       | `—`                      | —                                                                             |
| trained-models     | `GET /api/v1/projects/{project_id}/trained-models/{version_id}/decisions`                                             | `—`                      | `before`, `limit`                                                             |
| trained-models     | `POST /api/v1/projects/{project_id}/trained-models/{version_id}/decisions`                                            | `ModelDecisionCreate`    | —                                                                             |
| trained-models     | `POST /api/v1/projects/{project_id}/trained-models/{version_id}/deploy`                                               | `ModelDeploymentCreate`  | —                                                                             |
| trained-models     | `GET /api/v1/projects/{project_id}/trained-models/{version_id}/download`                                              | `—`                      | —                                                                             |
| learning-policies  | `DELETE /api/v1/projects/{project_id}/learning-policy`                                                                | `—`                      | —                                                                             |
| learning-policies  | `GET /api/v1/projects/{project_id}/learning-policy`                                                                   | `—`                      | —                                                                             |
| learning-policies  | `PUT /api/v1/projects/{project_id}/learning-policy`                                                                   | `LearningPolicySave`     | —                                                                             |
| learning-policies  | `POST /api/v1/projects/{project_id}/learning-policy/check`                                                            | `—`                      | —                                                                             |
| learning-policies  | `POST /api/v1/projects/{project_id}/learning-policy/disable`                                                          | `—`                      | `expected_revision` (required)                                                |
| learning-policies  | `GET /api/v1/projects/{project_id}/learning-policy/triggers`                                                          | `—`                      | `limit`, `offset`                                                             |
| sample-evaluations | `GET /api/v1/projects/{project_id}/sample-evaluations`                                                                | `—`                      | `limit`, `offset`                                                             |
| sample-evaluations | `POST /api/v1/projects/{project_id}/sample-evaluations`                                                               | `SampleEvaluationCreate` | —                                                                             |
| sample-evaluations | `GET /api/v1/projects/{project_id}/sample-evaluations/compare`                                                        | `—`                      | `left_id` (required), `right_id` (required)                                   |
| sample-evaluations | `GET /api/v1/projects/{project_id}/sample-evaluations/qualification`                                                  | `—`                      | `model_version_id` (required), `deployment_id` (required)                     |
| sample-evaluations | `DELETE /api/v1/projects/{project_id}/sample-evaluations/{evaluation_id}`                                             | `—`                      | —                                                                             |
| sample-evaluations | `GET /api/v1/projects/{project_id}/sample-evaluations/{evaluation_id}`                                                | `—`                      | —                                                                             |
| sample-evaluations | `GET /api/v1/projects/{project_id}/sample-evaluations/{evaluation_id}/attempts/{attempt_id}/report`                   | `—`                      | —                                                                             |
| sample-evaluations | `GET /api/v1/projects/{project_id}/sample-evaluations/{evaluation_id}/attempts/{attempt_id}/samples`                  | `—`                      | `class_index`, `error`, `confidence_min`, `confidence_max`, `offset`, `limit` |
| sample-evaluations | `GET /api/v1/projects/{project_id}/sample-evaluations/{evaluation_id}/attempts/{attempt_id}/samples/{ordinal}/image`  | `—`                      | —                                                                             |
| sample-evaluations | `GET /api/v1/projects/{project_id}/sample-evaluations/{evaluation_id}/attempts/{attempt_id}/samples/{ordinal}/source` | `—`                      | —                                                                             |
| sample-evaluations | `POST /api/v1/projects/{project_id}/sample-evaluations/{evaluation_id}/cancel`                                        | `—`                      | —                                                                             |
| sample-evaluations | `POST /api/v1/projects/{project_id}/sample-evaluations/{evaluation_id}/retry`                                         | `—`                      | —                                                                             |

### Phase D operations

| Tag              | Operation                                                                                          | Request schema        | Query parameters  |
| ---------------- | -------------------------------------------------------------------------------------------------- | --------------------- | ----------------- |
| training-studies | `GET /api/v1/projects/{project_id}/training-studies`                                               | `—`                   | `limit`, `offset` |
| training-studies | `POST /api/v1/projects/{project_id}/training-studies`                                              | `TrainingStudyCreate` | —                 |
| training-studies | `GET /api/v1/projects/{project_id}/training-studies/{study_id}`                                    | `—`                   | —                 |
| training-studies | `POST /api/v1/projects/{project_id}/training-studies/{study_id}/cancel`                            | `—`                   | —                 |
| training-studies | `POST /api/v1/projects/{project_id}/training-studies:preview`                                      | `TrainingStudyCreate` | —                 |
| active-learning  | `GET /api/v1/projects/{project_id}/active-learning-rounds`                                         | `—`                   | `limit`, `offset` |
| active-learning  | `POST /api/v1/projects/{project_id}/active-learning-rounds`                                        | `RoundCreate`         | —                 |
| active-learning  | `GET /api/v1/projects/{project_id}/active-learning-rounds/pool`                                    | `—`                   | `limit`, `offset` |
| active-learning  | `GET /api/v1/projects/{project_id}/active-learning-rounds/{round_id}`                              | `—`                   | —                 |
| active-learning  | `GET /api/v1/projects/{project_id}/active-learning-rounds/{round_id}/attempts/{attempt_id}/report` | `—`                   | —                 |
| active-learning  | `POST /api/v1/projects/{project_id}/active-learning-rounds/{round_id}/cancel`                      | `—`                   | —                 |
| active-learning  | `GET /api/v1/projects/{project_id}/active-learning-rounds/{round_id}/candidates`                   | `—`                   | —                 |
| active-learning  | `GET /api/v1/projects/{project_id}/active-learning-rounds/{round_id}/candidates/{ordinal}/image`   | `—`                   | —                 |
| active-learning  | `GET /api/v1/projects/{project_id}/active-learning-rounds/{round_id}/decisions`                    | `—`                   | `limit`, `offset` |
| active-learning  | `POST /api/v1/projects/{project_id}/active-learning-rounds/{round_id}/decisions`                   | `DecisionCreate`      | —                 |
| active-learning  | `GET /api/v1/projects/{project_id}/active-learning-rounds/{round_id}/feedback`                     | `—`                   | `limit`, `offset` |
| active-learning  | `POST /api/v1/projects/{project_id}/active-learning-rounds/{round_id}/feedback`                    | `FeedbackCreate`      | —                 |
| active-learning  | `POST /api/v1/projects/{project_id}/active-learning-rounds/{round_id}/retry`                       | `—`                   | —                 |
| active-learning  | `POST /api/v1/projects/{project_id}/active-learning-rounds:preview`                                | `Selection`           | —                 |

## Outcome

- Implemented phases A–E based on the pinned application commit, included in the local SDK capability-alignment commit requested by the user. No push, release tag, or publication was performed.
- Isolated worker commits integrated with `git cherry-pick --no-commit`: contracts `2ed595c846ed7351028f652effc328a71edb3d02`, training `8ff6316f6c54dc44dad489a3f41cb4f4106c8cf8`, model management `c49504737eadfeb7828bbb9c36eb92129b131eb9`, automation `5508bc5b853e81ea768911e9f08d4fbd5cb488d1`, and TUI `506f620a6a315e05ff6e95196d7e56e4e93a03e6`. Parent integration also fixes the literal download call site and aligns typed UUID/null fixtures with real JSON behavior.
- Added all 95 selected public HTTP operations and 95 explicit `aap learning` commands. Coverage accounts for 196 real operations across 363 watched operations; 167 exclusions remain, including ten executor-only operations. Existing filters/counts and atomic integrity-checked downloads are included.
- SDK release version remains `0.20.0`; the tested OpenAPI compatibility baseline is `0.26.1`. This does not claim compatibility with untested platform versions or live GPU workflow qualification.
- Final SDK/CLI/TUI suite: **436 passed** on Python 3.11.14. The TUI subset contains 46 cases, including real SDK response-model integration, nested queue envelopes, allowed filter categories, attempt changes, error retention, and 80×24 / 120×32 Pilot sizes. Lint, formatting, coverage/version guard, and diff whitespace checks pass.
- Built wheel and source archive under `packages/python-sdk/dist/`. Fresh, separate core-only, CLI, and TUI installations of the wheel pass imports, metadata, optional-dependency isolation, all 95 command registrations, and entry-point checks. The unlocked extras resolved newer compatible dependencies than the test lock; these checks are installation smoke, not a second full dependency-matrix test run.
- An actual 80×24 PTY using controlled mock HTTP data passed queue navigation, Chinese rendering, run detail, refresh, a permission failure, and recovery. This verifies the terminal surface, not a real training backend.
- Official documentation: `packages/python-sdk/README.md`, `docs-site/dev/sdk/{quickstart,python-client,cli,cookbook,tui}.md`, `README.md`, and the automation entry in `docs-site/user-guide/projects/online-learning.md`. Root and SDK Unreleased changelogs record user-visible changes. No new architecture ADR or service is needed.
- Documentation build, frontmatter, and navigation checks passed; the final documentation build completed successfully in 14.21 seconds. Existing syntax-highlighter and large-chunk warnings do not fail the build.
- Wheel SHA-256: `42181b3fc21f3586d7d4cc5e95809b68946ab26921dbef2fa43aaf49c37bc2e8`. Removed all five task-created worker worktrees and temporary package-smoke environments/scripts/logs. The worktree-local SDK test environment and reviewed distribution artifacts remain available.
- Live acceptance limitation: worktree doctor reports `initialized: false`; no owned disposable API/database/GPU training stack was started. Real training, controlled deployment, sample inference, and study/active-learning execution remain unverified. No remote CI result is claimed.
- Follow-up audit identified incomplete JSON-body and synchronous VOC handling in existing export helpers; these are addressed by the separate agent-usability implementation plan. Full API parity is not claimed by the operation count.
- Follow-through: push or assign/publish an SDK release only upon the user's request. Worker branches retain their source commit history.
