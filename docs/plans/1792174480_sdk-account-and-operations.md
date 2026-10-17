# Account, administration and operations SDK coverage

> Status: implemented and locally verified; originating-branch changes remain pending.
> Baseline: 568 operations, 305 covered, 253 uncovered and ten private executor exclusions.
> Previous batch: [Public REST SDK completion](1792172192_sdk-public-api-completion.md), 71 implemented operations beyond the earlier pending 38.

## Outcome and accounting

Adopt 106 additional uncovered public operations: account/avatar/health/capabilities 19, users/groups/invitations 30, operational and ML administration 44, and the 13 remaining auth operations required for complete account/invitation flows. The sets are disjoint. Confirm exact keys against the manifest before integration; change classifications only after real call sites exist. The expected resulting inventory, if the complete cohort lands without snapshot drift, is 411 covered, 147 uncovered and ten excluded. This is progress toward the 558-operation scope, not completion or a release milestone.

All user-authorized stable public interfaces remain in scope, including network probes, SMTP tests, lifecycle changes and privileged operational controls. Do not run those external side effects during SDK verification; use local fixtures and explicit CLI confirmations. No originating-branch commit, push, deployment or release bump is implied.

## Cohorts and contracts

| Slice                                                                  | Operations | Main requirements                                                                                                                 |
| ---------------------------------------------------------------------- | ---------: | --------------------------------------------------------------------------------------------------------------------------------- |
| Account/profile/preferences/avatar plus public avatar download         |         12 | Preserve Client.me; add account/avatar resources, multipart input and binary output.                                              |
| Root health and ML capability discovery                                |          7 | Real origin-root paths, degraded 503 JSON, conditional-response avoidance and probe effects.                                      |
| Users and groups                                                       |         23 | Paged queries plus compatibility arrays, managed flags, versioned lifecycle preview/apply and grouping scope.                     |
| Invitation administration                                              |          7 | Tokens and email are separate; preserve partial bulk results and rotate/revoke semantics.                                         |
| Authentication and invitation acceptance                               |         13 | Trace login/refresh/logout/register/reset/verification contracts before implementation; no implicit persistence or token refresh. |
| System settings, audit and storage connections                         |         19 | Versions/nulls/secrets, scoped audit, streamed exports, allowlists and outbound network tests.                                    |
| Preannotation operations, system health, analytics and alias frequency |          6 | Server owner filtering, partial clear results, fixed analytics panels and project authority.                                      |
| ML diagnostic/control and project backend operations                   |         19 | Backend capabilities, multipart frames, actual shared-resource effects, quiescence and no automatic write retry.                  |

### Account and authentication

The 11 account routes are PATCH /auth/me; avatar POST/PATCH/DELETE; deactivation-request POST/DELETE; preferences GET/PATCH; heartbeat POST; password POST; task-events:batch POST. Public GET /avatars/{token} returns WebP bytes. Account handlers use authenticated identity but do not declare separate API-key scopes; task events additionally recheck task/project capability. Preserve existing Client.me and aap me.

Preferences are JSON maps, not a second client layout engine. Preserve omitted/null/opaque values, deep-merge versus atomic-replacement boundaries and namedPresetsRevision CAS. Do not resubmit a full GET result. Task events require bounded, timezone-aware validated intervals and stable caller-provided client_id; mixed batches can return partial acceptance. Do not generate telemetry or heartbeat automatically.

Password input uses hidden prompts or stdin, never command-line secret arguments. A successful password change is 204 and does not imply JWT/API-key revocation. Avatar uploads are multipart with actual server image/size limits; replacement may delete the old object. Avatar download is public binary with ETag/304; omit conditional headers unless implementing complete cache semantics. Deactivation has a seven-day cooling period and notification side effects; never execute it against a real account during tests.

The 13 auth routes are POST forgot-password, invitations/accept, login, logout, logout-all, refresh, register, register-open, reset-password, send-verification-email, verify-email; GET invitations/{token} and registration-status. Complete their route/service contract audit before coding. Invitation detail/register are public, existing-user acceptance checks email identity, and registration returns JWT. Model exact credential/token shapes and expose explicit operations without mutating saved SDK credentials or silently refreshing sessions. Treat invitation/reset/refresh tokens as credentials in CLI output/error handling.

### Health and capabilities

Five health routes are /health, /health/celery, /health/db, /health/minio and /health/redis. httpx merges /health into the current /api/v1 base, so normal request('/health') is incorrect. Introduce explicit transport origin-root semantics, validate actual request URLs and teach both call-site/catalog extractors the literal root-path declaration; never claim fake /api/v1/health coverage. Actual degraded responses use 503 JSON and must remain inspectable rather than disappearing into a status exception. Any transport accepted-status handling must skip needless retries for an explicitly accepted health result and retain default error behavior elsewhere.

ML protocol/instances GETs are authenticated. Omit If-None-Match to avoid empty 304 parsing. Instances may fan out live setup probes when cached capability evidence is absent. Do not describe it as a zero-cost static read.

The already-covered API-key update route has an expiry semantic gap: existing update(..., expires_in_days=None) omits the value, whereas API PATCH null clears expiry. Prefer a typed explicit patch method that preserves the existing convenience method's documented behavior; test omitted versus null without conflating this semantic repair with another API operation.

### Users, groups and invitations

Users have 18 uncovered operations: array/query/stats/export; invite and bulk invite preview/apply; bulk group preview/apply; direct group patch; role preview/patch; offboarding preview/commit; deactivate/reactivate; admin-reset-password; DELETE. Groups have array/query/create/update/delete. Invitations have array/query/stats/export, resend, send-email and DELETE. Enumerate literal paths from the manifest and actual parameter names; prefer paged query routes for agents while retaining legacy arrays.

User page flags distinguish read scope from lifecycle authority. Direct PATCH users/{user_id}/group currently lacks the managed-target check used by bulk assignment; reuse the established policy and add foreign-project-admin regressions before exposure. Group deletion clears user associations. Offboarding commit preserves preview_version and explicit transfer choices; stale evidence requires a deliberate new preview. Emergency suspension revokes keys; reactivation does not restore them.

Account role and project_member_role are separate. Project invitations require explicit project role despite an outdated schema comment. Invite creation/bulk apply generate links without emailing; resend rotates/revives token without emailing; only send-email invokes SMTP. Bulk invite can partially succeed and a fresh invitation invalidates older pending tokens. Mock all SMTP and keep token-bearing responses/errors deliberate.

### Operations and ML

System settings are super_admin routes. Preserve expected_version conflicts, password_set rather than plaintext secrets, empty password clearing and reset key validation. SMTP test emails the current administrator and needs explicit confirmation. Storage connections enforce creator/global authority plus existing host/SSRF controls; connectivity test can return 200/ok=false. Allowlist writes have no CAS and require confirmation. Never echo stored connection credentials.

Audit requires a prerequisite policy fix: documented project administrators see project-related records, but global list currently exposes unfiltered actors/IP/details. Trace existing project audit ownership and choose a server-enforced scope that preserves superadmin global access; do not use client filtering. Archives/monthly summary/export are superadmin operations. Exports are CSV/JSON attachments, capped at 50,000 rows, and record an audit event; use one-shot streamed downloads. Archive reads currently load a full month before pagination; document actual cost.

Preannotation queue/summary currently return all projects to project administrators while their project caller filters in the browser and bulk-clear already enforces ownership. Filter server-side using Project.owner_id for project administrators; preserve superadmin global access. Alias frequency must also resolve legitimate project management before reading predictions. Add foreign-manager/no-side-effect regressions.

Operational controls keep their exact effects: bulk-clear mode can remove predictions or reset task/review state with per-item results; fixed analytics panels can return 503 before DuckDB initialization. ML overview can enumerate buckets, observe/probe perform network calls, smoke test may reload/unload, and project warmup can evict shared weights. Setup/capabilities return cached/raw upstream shapes; refresh is an explicit probe. Predict-test returns transient inference, predict-frame persists Prediction, and frame inputs use multipart with JSON context/config. Project disable deletes only the association; global configuration changes and quiescent unload have different authority. Preserve 409/503 rather than force/fallback.

## Ownership and acceptance

Use bounded independent workers with isolated Orca worktrees from current local HEAD. The parent owns aggregate/public/root CLI wiring, shared transport/extractors, coverage/catalog, documentation, generated artifacts and disposable-DB integration. Workers own disjoint new modules and any named API prerequisites, return real local commits, and do not provision shared runtimes or contact SMTP/backend services.

The pending parent contains required shared JSON/list/header helpers beyond committed HEAD. Be explicit about worker test prerequisites; do not commit temporary helper copies or hide integration limitations. Keep actual route literals, concrete resource methods and literal CLI SDK calls so discovery remains truthful. No generic JSON executor, local preference engine or backend protocol framework is needed.

Verify boundaries and resource relationships, pagination and export formats, secrets/confirmation refusal, CAS/idempotency, partial results, multipart bytes, binary response handling, root URLs and accepted degraded statuses. API prerequisite tests use a freshly verified disposable worktree test mode, with SMTP/storage/backend/queue effects mocked. Regenerate/check artifacts; run focused then SDK checks, required backend checks, docs/build/format/diff checks and installed core/CLI/TUI plus older-floor smoke after package changes. Report fixture/loopback, database ASGI, deployed runtime and remote CI separately. Keep the overall goal active while adopted public operations remain.

## Implementation receipt

All 106 real public call sites were promoted together: 411 covered, 147 uncovered, ten private executor exclusions, zero planned. The adopted public scope is 558 operations; coverage is progress, not a complete parity or release claim. API-key typed patch repairs an already-covered route and does not increase the count.

The parent integrated isolated feature commits 6d3abfe923, 26f5b1a21, bf406f2728, ef1887403d and 7eb03054ea, followed by corrections 9b878d36e5, fcc6528d96, 3f34cbd7d0 and 2af24dca94. Parent-owned work includes root transport semantics, aggregate wiring, API-key expiry/null behavior, safe public health models, audit ownership predicates, discovery enrichment, coverage/catalog and documentation. Worker branches retain the commits. No originating-branch commit, push, release or deployment was performed.

Server-side fixes apply established project authority before counts, limits or effects: preannotation queue/summary, alias frequency, direct user grouping and backend warmup. Audit scope handles canonical project/task/batch/annotation/feedback/invitation relations, verified dataset/video contexts and deleted batch history, while canonical foreign targets override misleading detail/path. Global bug reports are not granted project scope from unvalidated caller metadata. Public health omits raw exception details. Connector input rejects unknown config/userinfo and inconsistent SFTP credentials, with legacy response redaction and key-mode inference. Privileged ML URL probes retain private/internal destinations and roles, validate HTTP(S) syntax and return safe error categories; this is not a new destination ACL.

Verification:

- Full SDK suite: 862 passed. The first aggregate run exposed worker-only Client property fixtures and request-schema ordering assumptions; those fixtures were removed after real binding, typed-model ordering preserved, and the complete rerun passed. A final public-health CLI check verified URL-only configuration; all 204 affected CLI/health checks and the complete suite passed after that change.
- API: 139 distinct passing cases across four JUnit receipts before cleanup, including 111 operations integration/pure cases. Public health additionally passed five mock cases earlier. All DB-backed tests used the verified disposable worktree test mode; no live SMTP, external connector or backend inference was executed.
- Installed wheel: four fresh core/CLI/TUI/minimum-floor environments resolved 421 method descriptions and all 528 public exports with warnings treated as errors. Core used no CLI dependency. Loopback fixtures exercised root 503, preference null/CAS transport, explicit login token ownership, expiry clearing and audit export; CLI also checked degradation output and refusal before email/token mutation.
- Floor: Python 3.11.14, Pydantic 2.5.3, HTTPX 0.27.2, Typer 0.12.5, Click 8.1.8. Modern installs used Pydantic 2.14.1; TUI import used Textual 8.2.8.
- Wheel/sdist built with uv; all 127 packaged source/guide/catalog files byte-match current source. Wheel SHA-256: d157e4ec27c434be9113b338e6ec987849c2a9cdd9f512f065735ca1d6251372. Sdist SHA-256: 45bcac288826641b5d203109856a144c3c8f0030517841197b7519d8d660183d.
- OpenAPI snapshot, frontend types and offline catalog regenerated and checked; documentation build, 179 frontmatter files, 182 navigation pages and 28 SDK Python snippet syntax checks passed. Ruff check/format, tracked diff whitespace and all 127 untracked-file whitespace checks passed.

The six clean worker worktrees were removed through Orca; their branches/commits remain. The verified disposable test database, dedicated Redis and nine buckets were destroyed with the exact mode confirmation. Temporary installed environments, loopback fixtures and JUnit files were removed after their summaries were recorded here. The originating index was restored to its initial empty state; pending source changes remain intact at HEAD 7ed68a5a667c43058917bdec5a9ee809cde052b1, with cached origin/main divergence 0 behind / 2 ahead.

Next cohort: [Project workflows](1792177548_sdk-project-workflows.md). Remaining public operations stay uncovered until concrete methods and behavioral checks land. Deployed services and remote CI were not exercised.
