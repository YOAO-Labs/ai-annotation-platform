# Annotation discussion and quality SDK completion

> Status: implemented and locally verified; the overall public API goal remains active.
> Baseline: 568 snapshot operations, 463 covered, 95 uncovered and ten private executor exclusions.
> Previous cohort: [Project workflows](1792177548_sdk-project-workflows.md).

## Adopted scope

Continue all stable public REST interfaces, including privileged operations. Keep the ten private executor training operations excluded. Existing Workbench provenance, mutation/cache effects or administrator roles are not reasons to exclude a public operation. No release, deployment, originating-branch commit or push is implied.

This cohort adds the 51 uncovered operations in annotation-comments (ten), annotation-history (two), bug-reports (ten), mask-formats (seven), mask-qc (13), and point-cloud-quality (nine). The sets are disjoint. The expected inventory after implementation and verification is 514 covered, 44 uncovered and ten excluded; the last 44 belong to task Workbench routes. Verify exact keys from api-coverage.toml before promotion.

## Implementation partitions

- Discussion/history/bug reports: 22. Trace thread pagination, annotation/task identity, attachment init/sign/download, author/moderation permissions and the deliberate global administrator bug-report policy. Do not authorize project scope from unverified caller-supplied bug-report project/task metadata. Preserve raw/wrapped file formats, soft-delete and comment edits, with explicit confirmation where the existing workflow requires it.
- Mask formats: seven. Keep catalogue/readiness separate from import preflight, signed upload, creation, status and resume. Preserve exact preflight identity, source/storage format, bounds and resumable import rules. Do not construct a second format converter in the SDK.
- Mask QC: 13. Read summaries/issues/regions and comparison content with exact parent visibility. Run, dry-run, repair, resume and rollback retain server digests, versions, affected selections and eligibility; completion of a compute job is distinct from accepted repair. Rollback is an explicit mutation, never an automatic failure fallback.
- Point-cloud quality: nine. Keep run and evaluation identities, issue state and evaluation promotion distinct. Trace project roles, task/frame anchors, result evidence, cache effects and stale-input rejection before exposing a method. No GPU/browser renderer validation is claimed by mocked SDK tests.

## Integration constraints

Read affected API schemas, service invariants and frontend callers. Reuse existing COCO RLE, content references, job, upload/download, confirmation and partial-result helpers; retain request validation at trust boundaries. Use concrete methods and literal paths matching snapshot placeholders. Response schemas tolerate additive fields; destructive request schemas reject unknown fields so a misspelled dry-run/selection does not silently broaden a write.

Writers use isolated worktrees from the originating checkout's current local HEAD and return real commits. Pending parent prerequisites are not in that HEAD: supply only the required helpers/models explicitly, exclude temporary copies from worker commits and state the dependency in test reports. The parent owns aggregate/public/root CLI integration, shared contracts, catalogue/coverage, documentation, API artifacts and disposable-DB checks. Existing owner classes may be edited in disjoint sections; reconcile shared imports without losing earlier work.

## Acceptance and finish

Check resource pairs, live authorization, pagination before limits, binary/JSON/multipart shape, signed credential isolation, confirmation refusal before effects, partial receipts, stable idempotency/preflight identity and conflict behavior. Mock external inference/storage/mail/queue effects; DB-backed tests only use a freshly verified disposable worktree test mode. Distinguish API ASGI fixtures and installed loopback from deployed runtime and remote CI.

Regenerate/check API artifacts and offline catalogue, run relevant SDK/API checks, docs and package build, inspect final diff and git diff --check. Test current and supported lower dependency floors for core/CLI/TUI as affected. Clean only owned temporary resources and clean integrated worktrees, retain worker commits and preserve unrelated work.

The overall goal remains active until all 558 adopted public REST operations are implemented and the completion audit verifies their actual contracts and agent usability. The final audit must also inventory documented stable public streams and downloads outside OpenAPI; the REST count alone does not prove every public protocol is supported. Counts or an unexecuted plan alone do not prove completion.

## Implementation and acceptance receipt

Integrated worker commits e00a79e6f820263bc0affb08fccc5f381db20cd1 (Mask formats), 399230fa9 and 877036afc (point-cloud quality and additive config reads), d79bfda44902257a7d0bb200043b532574902b4b (Mask QC), 7751199dbac04d460361147643bb90dd2348da18 (discussion/history/bugs), and a79ad1ef52ea6b6ca90253ddb1fc8729031b469c (API regressions). The originating checkout retains those changes without a new originating commit. Temporary parent helper/model copies were restored or removed before worker commits.

Six Client namespaces and six CLI groups expose the 51 literal REST operations. The manifest and catalog now classify 568 operations as 514 covered, 44 uncovered, ten executor-only excluded and zero planned. Public exports total 718; all 529 SDK method descriptions expand offline in installed core, CLI, TUI and supported lower dependency environments.

API corrections reuse the actual task discussion visibility helpers for both history paths, preserve soft-deleted history, require export.annotations for Mask export preflight, restrict reporter patches to content fields, reject explicit null for non-nullable bug content and preserve administrative null clears. Point-cloud dispatch failures retain actual persisted failed run/job UUIDs. CLI structured errors expose safe reason/run_id/job_id. SDK responses preserve extended bug states, reuse canonical DiscussionActions, reject oversized screenshots before requests and hide signed storage error bodies/URLs.

Validation performed:

- SDK full suite: 992 passed; the final discussion file passed 21 cases after adding two storage-upload error regressions, giving 994 distinct passing SDK cases across these runs. No implementation changes followed those checks.
- API ASGI/DB checks: 36 scope/history/bug cases plus 69 existing comment/Mask format/Mask QC/point-cloud quality cases, all passed. The archive duplicate-name warning is intentional fixture behavior. External queue/storage/mail effects were mocked.
- Fresh Python 3.11.14 wheel installations: core-only, CLI, TUI and lower floor (Pydantic 2.5.3, HTTPX 0.27.2, Typer 0.12.5, Click 8.1.8). All exports/descriptions loaded; core remained free of CLI/TUI dependencies. Installed loopback requests: 20 core and 22 in each other environment. Verified mixed discussions, history, extended bug status/Markdown, credential-free 302 download and atomic failure, signed PUT, exact import/repair proofs, partial CLI exit, confirmation refusal before HTTP and dispatch recovery IDs.
- Wheel and sdist built; 162 source/guide/catalog files byte-matched current source in both artifacts. Wheel SHA-256: e4da7ea31901ddf4ccb64ceeac2b623399d246121b859c5a8d74217d16a27eb5. Sdist SHA-256: c93c4cb91bb617213c805efa5c8f103b71d0dc3a2529637de8e740469617942c.
- OpenAPI export/check, frontend API codegen, offline catalog generation/check, pinned Ruff check/format, docs build, 181 frontmatter documents, 184-page navigation and 31 SDK Python snippets passed. Tracked/staged whitespace checks passed, and all 159 then-untracked files passed independent whitespace checks after the next plan was written.

The disposable test database aap_wt_a13d73521739f081_test, its owned Redis and nine buckets were doctor-verified before use and destroyed with exact confirmation after acceptance. Four clean integrated worktrees were removed through Orca; worker branches and commits remain. Temporary installation environments and receipts are cleaned after recording this evidence. Originating HEAD remains 7ed68a5a667c43058917bdec5a9ee809cde052b1, cached origin/main comparison is 0 behind / 2 ahead. No push, release, deployment, browser/GPU qualification, live external backend test or remote CI result is claimed.

## Remaining scope

The last 44 REST operations are task Workbench reads, previews, locks/jobs, point-cloud camera and scene-track mutations, prediction decisions and geometry commits. Six documented public WebSocket streams are mounted outside OpenAPI and require a separate protocol inventory and consumer implementation. Hidden metrics discovery/scrape and test-seed HTTP endpoints remain internal/test-only; they are not silently counted as public REST completion. Continue with [Task workflows and public streams](1792181220_sdk-task-and-stream-completion.md); 514/558 is progress, not overall completion.
