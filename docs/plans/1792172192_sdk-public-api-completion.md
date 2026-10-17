# Public REST SDK completion

> Status: adopted public API scope implemented and locally verified.
> Scope decision: all stable public REST operations, including administration and operations, explicitly confirmed by the user on 2026-10-17.
> Committed base: `7ed68a5a667c43058917bdec5a9ee809cde052b1`; preserve the two pending 19-operation batches.

## Completion contract

The current API snapshot contains 568 operations. The adopted scope is **558 public REST operations**, with only ten `/api/v1/internal/training/*` executor protocol operations excluded. At adoption, 234 operations have implemented SDK call sites and 324 remain. The previous 151 exclusions for Workbench origin, browser use, missing external demand, operational risk or a separate connector project become uncovered; those reasons do not establish a private protocol. Do not count reclassification as implementation.

Completion requires a callable SDK operation for each adopted route, typed request and response contracts where the API defines them, usable CLI entry points, offline descriptions, relevant workflow documentation and meaningful behavior verification. Reconcile actual call sites against the complete snapshot after each batch. The continuing goal remains active while adopted routes or required correctness checks remain. No release assignment, originating-branch commit, push or deployment is implied by this continuation.

Treat task and dataset-item routes as distinct operation identities even when their underlying media services overlap. Preserve legacy public routes with explicit guidance toward preferred pagination or creation paths. API-key permissions, user role, resource visibility and parent-child identity remain server-owned; privileged actions require documented authority and existing CLI confirmation. GET cache generation is an observable side effect, and generic AsyncJob completion is not necessarily domain acceptance.

## Current implementation batch

Implement 71 media, review, template, storage, file and scene operations using isolated Orca worktrees based on the current local HEAD and parent-owned integration:

| Slice                                                                  | Operations | Ownership                    |
| ---------------------------------------------------------------------- | ---------: | ---------------------------- |
| Dataset-item video media and chapters                                  |         13 | Media worker                 |
| Task video media, including legacy manifest                            |          9 | Media worker                 |
| Task segment claim, heartbeat, release, submit, reopen and unassign    |          6 | Media worker                 |
| Tracker Jobs, task recovery and tracking/correction creation           |         13 | Tracker worker               |
| Mask keyframes/content, track quality and track composition/conversion |         12 | Review worker                |
| Project template list, CRUD and duplication                            |          6 | Template worker              |
| Storage diagnostics, video asset failures and retry                    |          4 | Parent                       |
| Task files and dataset scenes, with authorization prerequisites        |          8 | File/scene worker and parent |

The parent integrates aggregate/public/CLI registration, coverage/catalog, workflow documentation and installed-package verification. A separate isolated API worker fixes template/source project inheritance as a prerequisite for a truthful template application workflow. Workers return real local commits; integration preserves earlier uncommitted work. Model, helper and CLI names follow existing SDK ownership instead of adding another transport, decoder, query or orchestration framework.

Video descriptors remain JSON, including dynamic HTTP 202 pending frames/chunks and signed URLs. Downloads must preserve atomic replacement and credential isolation and reject incomplete successful-looking responses. Do not add Range or browser decoding without a separate concrete need. Reads can create cache records or enqueue work; frame bounds and retry/force effects must be discoverable. Writes are never retried automatically. Tracking creation has no idempotency key; recover through task active/reviewable lists after uncertain results. Lease heartbeats and polling are explicit caller actions.

Tracker decisions submit the preview revision, source versions and exact selector/digest. Stale 409 responses require caller reconciliation. Quality runs have their own digest and reviewer lifecycle. Mask writes submit If-Match versions. Composition and split operations can deactivate source annotations and require explicit CLI confirmation. Do not silently refresh evidence, widen a selection or apply candidates after compute completion.

Templates use server inheritance, not SDK field merging. Correct default modality and type-label inheritance before exposing omission as a supported workflow. Preserve explicit overrides, legacy tool-binding coalescing, template/source mutual exclusion, visibility, guide-copy semantics and usage accounting. Template lists have no pagination or revision token; do not invent either.

## Remaining cohorts

Continue through comments/history/search/guides; mask and point-cloud quality; users/groups/invitations and lifecycle previews; mask formats/bug reports/performance; personal settings and API keys; health and ML capabilities; storage connections/assets, audit, system settings and administrative operations; files/scenes/avatars; and the former Workbench/project/ML exclusions. These are public candidates, not indefinitely deferred work. Derive each batch from the manifest's uncovered set so overlapping tags and compatibility paths cannot distort counts.

Files and scenes require access-policy verification before SDK exposure: preliminary source tracing found login checks without obvious project/dataset visibility checks. Audit-log scope must be checked against its intended global versus project policy. Fix demonstrated API correctness problems in the owning service with focused regressions; do not block independent domains or expose unsafe behavior merely to increase coverage.

The file/scene audit established existing policy: file writes require project management, signed file reads use task visibility, and scenes follow the global dataset catalogue rather than inferred project membership. Scene API-key reads require datasets:read and metadata writes follow dataset manager roles. Apply these guards before storage/queue effects. Expose a separate signed upload-content helper so callers retain task identity across init, PUT and completion; it sends no platform credentials and does not implicitly complete or retry.

## Acceptance and evidence

For each slice, verify exact path, parent identity, query aliases, body omission/nulls, schema bounds, response envelope, dynamic status, mutation retry policy, optimistic conflicts and confirmation refusal before HTTP. Add meaningful tests for new validation and security-sensitive transitions. Keep core-only imports independent of CLI/TUI extras and retain the declared older Pydantic/Typer floor.

Regenerate and check the offline catalog, run focused checks followed by the SDK suite, Ruff/formatting, docs build, final diff review and `git diff --check`; rebuild wheel/sdist and test installed entry points after package changes. API changes additionally require affected backend checks and regenerated API artifacts. Database tests must run through the worktree launcher against verified disposable resources; never use shared default test or development databases. No worker change in this batch requires a primary Celery restart.

Record local tests, loopback fixtures, live API/database evidence and remote CI separately. Missing infrastructure is a precise validation limitation, not a reason to claim success or complete the whole public-API goal. Update this plan with integrated operation counts and acceptance evidence after the batch.

## Integrated outcome

All 71 operations in this batch are implemented. The inventory is **568 = 305 covered + 253 uncovered + 10 executor-private exclusions**, with 314 concrete SDK method/helper descriptions and 296 CLI command descriptions. The 151 old public exclusions were reclassified without claiming implementation. The continuing 558-operation public REST goal remains active.

Integrated worker commits are `e4698f0ca35a` (templates), `805b7597ef0a` (tracker), `b208491c4562` (video review/content), `92b9a1c1f1cd` (media/leases), `cfe74225a` (API inheritance), and `b8328e583c11` (files/scenes). The parent wired aggregate/public/CLI declarations, metadata, docs and artifacts; added explicit signed upload-content support; preserved earlier pending batches; and removed duplicated response hydration so server-derived chapter/touched-annotation fields remain authoritative. Media call sites are literal and concrete; coverage extraction was not weakened to infer dynamic route bases.

API source/template creation now resolves modality through one existing-service helper before legacy tool-binding coalescing and fills configuration afterward. Omitted identity fields can inherit only with a source/template; explicit null required identity fields are rejected. Four frame/chunk routes now document their actual HTTP 202 JSON response. Files/scenes apply the established access policy before storage, database or queue side effects. No migration, primary runtime restart, Celery source change, dependency-bound change or SDK release bump was needed.

Acceptance:

- Full SDK suite: **754 passed** on Python 3.11.14. Additional offline metadata assertions also passed after final helper discovery checks.
- Backend checks used owned disposable `aap_wt_a13d73521739f081_test` at migration head 0189, isolated Redis and nine owned buckets. Template/project API suites passed 32 cases. File/scene/auth/key suites initially passed 26 of 27; the failing backfill fixture returned None instead of a Celery result. Correcting that fixture and adding a foreign-project-admin/legitimate-owner boundary produced two passing focused rechecks, covering **28 unique cases** in that group. No production code was relaxed for the fixture failure.
- OpenAPI export/check and local frontend API code generation passed. Catalog generation/check, pinned Ruff check/format and whitespace checks passed.
- Docs build passed; frontmatter checked 178 files, navigation reached 181 pages, and 26 Python snippets under the SDK docs parsed. Existing highlighter and bundle-size advisories are unrelated.
- Wheel/sdist built. All 86 package code/resource files matched the wheel bytes. Fresh core, CLI and TUI installs expanded all 314 descriptions offline; core imported without Typer/Textual. Current dependency smoke used Pydantic 2.14.1/Typer 0.27.3; older CLI floor smoke used Pydantic 2.5.3/Typer 0.12.5/Click 8.1.8 with import warnings treated as errors.
- Installed current/minimum CLI loopback checks passed five flows each: pending JSON frame, actionable one-shot failed download preserving the destination, same-origin signed PUT without platform credentials or implicit completion, upload-complete confirmation, and one stale candidate decision without refresh/retry. No external storage, SMTP or model backend was contacted by these checks.

Artifacts under `packages/python-sdk/dist/`:

- Wheel SHA-256: `00cf10243387b0938a6eb5b51b3011b6aebf65b6f4f0245ad0b71a6071fe7154`.
- Sdist SHA-256: `0b8fa4423944547f81423079563af379ccc4cc5aa0503c7e41c621e9ac398bc8`.

The dedicated test mode was destroyed after fresh doctor ownership/zero-session verification. Task worker worktrees were removed through Orca with source branches retained. Package/API environments and distribution artifacts remain. Temporary install environments, loopback fixtures and logs are cleaned after acceptance. The originating branch still has HEAD `7ed68a5a6`, two commits ahead of cached origin/main, with the pending earlier 38 operations plus this 71-operation batch; no originating-branch commit, push or release was performed.

No deployed application HTTP server, browser, remote CI, real storage upload/download, SMTP, Celery execution or GPU/backend inference acceptance is claimed. The database integration uses the actual ASGI API and disposable PostgreSQL with mocked storage/queue side effects. Remaining public operations and their prerequisites continue in the next audited cohort plan.

## Final public API completion

All 558 adopted public REST operations and six separately inventoried public WebSocket channels have concrete SDK/CLI consumers, typed contracts, offline discovery and local acceptance. Ten executor-private operations remain excluded. The final route inventory, worker integration, 1079 SDK checks, API regressions, installed dependency floors, artifact hashes and validation limits are recorded in [Task workflows and public streams](1792181220_sdk-task-and-stream-completion.md). Earlier counts and receipts above describe their respective implementation cohorts. No originating commit, push, release, deployment or remote CI is implied.
