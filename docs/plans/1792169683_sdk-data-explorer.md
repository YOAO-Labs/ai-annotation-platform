# Data Explorer and saved-view SDK coverage

> Status: implemented and locally validated on 2026-10-17 under the continuing SDK request.
> Committed base: `7ed68a5a667c43058917bdec5a9ee809cde052b1`.
> Working baseline: the previous uncommitted 19-operation notification/feedback/pipeline batch, validated with 577 SDK tests.

## Outcome contract

Add the next 19 public API operations as two synchronous SDK resources and CLI groups, including typed requests/responses, offline descriptions, full inventory classifications, documentation and package verification. Preserve all earlier uncommitted work. No local commit, push, release number, deployment, database migration or recurring automation is requested by this continuation.

Before this batch, the snapshot contains 568 operations: 215 covered, 161 excluded and 192 uncovered. All 19 selected routes are currently uncovered. After actual call sites land, the expected inventory is **568 = 234 covered + 161 excluded + 173 uncovered**. Coverage means an implemented route with focused contract checks, not unrestricted runtime availability or complete application coverage.

## Data Manager: 11 operations

Expose `client.data_manager` and `aap data-manager`:

- `schema(project_id, entity_scope="tasks")`: GET project Data Manager schema.
- `summary(project_id, body)`: POST task-centric filtered summary.
- `matches(project_id, task_id, body)`: POST the task's matched annotation/prediction/tracker summaries.
- `query_objects(project_id, body)`, `object_detail(project_id, annotation_id)`, `object_location(project_id, annotation_id)`.
- `query_tracks(project_id, body)`, `track_detail(project_id, track_ref)`.
- `assignment_preview(project_id, body)`, `assignment_apply(project_id, body)`.
- `export_tasks(project_id, body, *, idempotency_key=None)`.

Use the actual nested schema/summary/page/location/track/detail/action receipts, rather than an untyped HTTP escape hatch. Response fields tolerate additions. Request models forbid accidental fields and preserve JSON UUIDs, omitted fields and explicit nulls.

Fields/operators/options come from the current project's schema and entity scope. Keep filter JSON and dynamic attribute keys intact; do not flatten boolean groups, compile SQL, execute filters locally, or hardcode project attributes. API limits are depth 32, 4096 nodes and 200 items in an `in` filter. Strict CLI JSON input must reject non-finite numbers before HTTP; field/operator/value qualification remains server-owned.

Task, object and track grain are distinct. Task queries use offset; entity queries use opaque cursor pages (limit 1–200, cursor up to 2048 characters). Reset cursors when filters or sort change. The current entity handler uses the first sort item and validates columns without pruning row fields; summary dimensions are accepted but do not select a smaller aggregate response. Describe these actual limits rather than implying new projection or multi-sort behavior.

All results derive from project-visible tasks. Object IDs must identify visible active, non-cancelled objects; `track_ref` is an opaque server locator, distinct from track_id. Encode it as one URL path parameter, preserving its semantic value. Track detail returns all visible members and has no member pagination; do not invent a limit.

Assignment preview is read-only. Selection contains 1–200 task UUIDs and is sorted/deduplicated by the API contract. At least one owner field must be explicitly present: omitted means unchanged, null clears the task override and falls back to its batch owner. Apply submits the same selection/targets and returned 64-character preview_version; the server locks/recomputes eligibility and can reject stale evidence with 409. Per-item skipped/failed results are not total success. CLI apply requires confirmation and never silently refreshes the preview or retries a mutation.

Selected-task export sends the exact canonical selection, never a project-wide fallback. It requires export capability and rechecks visibility. Task-scoped export forbids video scope and currently excludes VOC/coco-multicamera/nuscenes/kitti/pointmask targets. Reuse `ExportRequestBody` lidar contracts. Optional caller Idempotency-Key is forwarded through the existing per-request header support; no generated replacement or automatic POST retry. The HTTP 202 acknowledgement may represent a reused Job with a terminal status. Observation and streamed artifact download reuse existing jobs/exports methods; CLI submission requires confirmation and does not wait implicitly.

## Task views and task query: eight operations

Expose `client.task_views` and `aap task-views`:

- `list`, `create`, `get`, `update`, `delete`, `copy` under project task-view routes.
- `query(project_id, body)` for POST project tasks/query.
- `query_view(project_id, view_id, *, limit=50, offset=0)` for a saved task view's GET tasks.

Every resource method carries project_id; the server jointly validates project/view visibility and ownership. Private views are owner-visible; project views are member-readable and manager-writable. Copy defaults to the source visibility, so an ordinary member copying a shared view must explicitly request private visibility. Name conflicts remain 409. Do not promise an otherwise unreachable super-admin exception for another user's private view.

The list is an unpaginated `{items}` envelope with built-ins and saved entries. Built-ins have a key, null id and no owner; UUID routes apply only to saved views. Query built-in configuration by passing its filter/sort/columns to the normal task query. Invalid dynamic fields preserve the view with invalid_fields and unknown counts. Null counts do not mean zero.

Queries use limit/offset, without cursor, schema revision or ETag. Explicit columns reduce expensive task projections; empty columns retain the current broader compatibility behavior. POST query is a read and needs no mutation confirmation. Update nulls mean unchanged; use empty objects/arrays to clear configuration. entity_scope is immutable on updates. Delete removes only the saved configuration, returns empty 204 and requires CLI confirmation. Copy requires an explicit JSON body, even `{}`.

## Ownership and integration

The task-view worker uses one isolated Orca worktree from local HEAD `7ed68a5a6`. Its eight-operation slice needs no earlier uncommitted feature modules. It owns only new task-view resource/model/CLI modules and focused tests, and returns a real local commit. A small independent TaskViewEntityScope Literal keeps that slice self-contained; both scope aliases use the same three API values.

The parent owns the 11 Data Manager operations, aggregate/public/root CLI wiring, manifest/catalog, shared JSON input validation, integration checks and documentation. Reuse the existing typed Task/UserBrief/ExportRequestBody, HTTP transport, JSON/error/confirmation helpers and jobs/download ownership. Do not modify API services or create another query/worker/transport framework. Workers may reuse the parent environment read-only with an explicit child PYTHONPATH.

CLI names mirror SDK methods with hyphens; complex bodies use files or stdin. Keep compatibility with the declared older Typer/Pydantic floor: CLI choices use existing string validation, request model configuration allows legitimate model-prefixed fields without import warnings. No dependency-bound changes are planned.

## Acceptance

- Verify exact method/path/query/body/status, project child identity, UUID aliases, omission/null/empty semantics, bounds and unknown response fields.
- Verify schema-driven filters survive intact, three grains/envelopes remain distinct, opaque cursors/track refs are forwarded safely, built-ins/null counts are preserved, and there is no hidden pagination or polling.
- Verify preview/apply identity, canonical selection and per-item failures; refusal and malformed inputs send no mutation. A stale preview does not cause another apply.
- Verify export selection, optional idempotency/auth header merging, no implicit widening, no POST retry, replay statuses and reuse of existing Job observation/download.
- Verify root CLI registration, JSON stdout/stderr, stdin, confirmation, non-finite input rejection and installed core/CLI/TUI boundaries. Only repeat earlier terminal rendering acceptance if changed behavior warrants it.
- Derive classifications from actual calls, regenerate/check the catalog, run focused checks then the full SDK suite, Ruff, formatting, docs build, `git diff --check`, wheel/sdist and fresh installed smoke checks.

Update SDK references, a dedicated Data Explorer workflow page, agent guide, package/root README and Unreleased changelogs. Plans/handoffs are English, product documentation Chinese. Static mocks and loopback checks do not prove live application permissions, database consistency, GPU/executor behavior or remote CI.

## Outcome

All 19 routes are implemented and classified from actual call sites. The current inventory is **568 = 234 covered + 161 excluded + 173 uncovered**. No exclusions were promoted in this batch. The catalog contains 238 SDK method descriptions and exports 47 new Data Explorer models/type aliases. The preceding uncommitted notification/feedback/pipeline batch is preserved, so the originating worktree contains two separately documented, uncommitted 19-operation batches beyond committed `7ed68a5a6`.

The task-view slice was integrated from worker commit `6fc134b86a4faa0d5a599b9fe191a61f43a60896`. The parent implemented the 11 Data Manager operations, generated their initial typed declarations from the current API schemas, reviewed inheritance/field semantics, added identity/grain descriptions, wired aggregate/public/CLI surfaces and updated coverage/catalog/product documentation. Request and response schemas retain their actual envelopes and dynamic JSON contracts; no query engine or additional service/dependency was introduced.

The shared CLI JSON reader now rejects non-finite constants and floats overflowing to infinity before HTTP. This prevents otherwise valid dynamic Any-valued dictionaries from escaping local validation and failing during transport serialization. Pydantic qualification and API filter/schema rules remain the request owners; inputs are not logged or silently replaced.

Local acceptance:

- Full SDK suite: **622 passed** on Python 3.11.14. The focused Data Manager/task-view checks passed 42 cases, including canonical/null assignment fields, stale-version single-call behavior, strict input refusal, selected export/no widening, opaque track path encoding, three-grain envelopes and actual registered-root CLI behavior.
- Catalog regeneration/check, Ruff 0.15.22 check/format, Prettier, and `git diff --check` passed.
- Docs build passed, frontmatter checked 176 files, navigation covered 179 pages, and 26 Python documentation snippets parsed. Existing language-highlighter/bundle-size advisories remain unrelated.
- Wheel and sdist built with the current generated catalog and English guide. Fresh core, CLI and TUI installations passed outside the checkout. Core requires neither Typer nor Textual; all 238 operation descriptions expanded offline in each environment.
- Current dependency smoke used Pydantic 2.14.1, Typer 0.27.3 and Textual 8.2.8. An additional installed CLI with Pydantic 2.5.3, Typer 0.12.5 and Click 8.1.8 passed the same checks without import warnings. These are installed smoke checks, not full-suite claims for every dependency combination.
- Installed console checks used ephemeral loopback HTTP fixtures to verify schema access, built-in null IDs/counts, nonempty typed task rows with unknown response fields, read-only POST without confirmation, canonical selection/null owner, zero-request refusal, single 409 apply, and authenticated Idempotency-Key selected export with an existing terminal Job status. No implicit wait, download or scope widening occurred.

Artifacts remain under `packages/python-sdk/dist/`:

- Wheel SHA-256: `1a7e2697a7acbc4d93ecb565405b740e1432e2d495e5a6a3edd7a34ce1ffd192`.
- Sdist SHA-256: `ec0483a67b5b8dc77dd8d565a14c4eec72313d680dd8798a6028988ab2207c48`.

Current product docs are `docs-site/dev/sdk/data-explorer.md`, SDK/CLI reference, quickstart, agent guide, package/root README and Unreleased changelogs. SDK release `0.20.0` and compatibility target `0.26.1` are unchanged; no originating-branch commit, push or release was performed for this continuation.

The clean task-view worker worktree was removed through Orca after integration/validation and its source branch retained. Temporary wheel-install environments, loopback servers and validation scratch files were cleaned up. The originating SDK environment and distribution artifacts remain; both pending batches are unstaged.

No live application API/database, browser, executor/GPU or remote CI acceptance was performed. No API/worker code changed, so no runtime restart or migration was needed. Backend ownership, visibility, row locks, stale preview decisions and dynamic schema qualification were traced in source but are not proven by mock/loopback fixtures. Later candidates remain video/tracker jobs and project templates, each requiring a new contract pass.
