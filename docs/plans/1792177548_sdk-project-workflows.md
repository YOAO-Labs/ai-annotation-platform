# Project workflows and the remaining public SDK scope

> Status: implemented and locally verified; originating-branch changes remain pending.
> Baseline: 568 snapshot operations, 411 covered, 147 uncovered and ten private executor exclusions.
> Previous cohort: [Account and operations](1792174480_sdk-account-and-operations.md).

## Scope and sequence

The user adopted all stable public REST interfaces including operations. Keep the ten executor-private training operations excluded; do not reclassify public Workbench, cache, diagnostic or administrator operations merely because they have side effects. No release milestone or originating-branch commit is inferred.

The remaining 147 operations form four disjoint groups:

| Group                                                                                              | Operations | Next action                                                                                                      |
| -------------------------------------------------------------------------------------------------- | ---------: | ---------------------------------------------------------------------------------------------------------------- |
| Project, batch, dataset, dashboard, performance, guide asset, search and prediction administration |         52 | Implement the next cohort below.                                                                                 |
| Annotation comments/history and bug reports                                                        |         22 | Trace attachment uploads, pagination, global administration and project relationships.                           |
| Mask formats/QC and point-cloud quality                                                            |         29 | Preserve dry-run/preflight digests, run identity, resumable repair and promotion authority.                      |
| Task Workbench operations                                                                          |         44 | Split into media/locks, inference/candidates and 3D/scene/geometry commands without creating a generic executor. |

## Next cohort: 52 concrete operations

Use exact method/path keys from api-coverage.toml, not inferred counts. Partition independent implementations as follows:

- Projects 11, project performance four and search one: 16. Add paged project queries, current readiness/usage/relations, explicit rename/transfer/orphan cleanup and LiDAR preflight. Performance export streams once with exact membership authority; search remains role-scoped.
- Batches: 13. Include assignment/distribution preview/apply, admin locks, submit-review, split, bulk archive/delete, unclassified counts, batch audit and LiDAR preflight. Preserve explicit selection, current preview_version and destructive scope; do not auto-refresh conflicts.
- Datasets six and guide assets four: ten. Keep async job IDs, exact import budgets and connector visibility. Upload guide bytes through signed URLs with credential isolation, then explicitly complete; cleanup/backfill/retry side effects require existing confirmations.
- Dashboard seven, prediction administration five and project annotation import one: 13. Preserve current actor scope, paged/detail shapes and audit export. Failed prediction retry/dismiss/restore and purge are different effects. Annotation import validates target project and payload before dispatch.

The expected inventory after all 52 land without drift is 463 covered, 95 uncovered and ten excluded. Promote only actual validated call sites. This remains progress toward 558 public operations.

## Integration and acceptance

Read API authorization, service helpers and current frontend callers before defining SDK models. Reuse response owners and existing preview/apply/export/upload helpers; extend concrete resources where that is the smallest complete change. No model version bump, protocol framework or generic JSON tool executor is needed.

Modifying workers use isolated worktrees based on the originating checkout's current local HEAD and return real commits. The parent owns aggregate/public/CLI wiring, shared transport/output/discovery, docs, coverage/catalog, generated API artifacts and disposable test-mode integration. Pending parent-only helpers must be supplied explicitly if worker tests need them; do not commit temporary compatibility copies.

Test resource relationships, rejection before effects, partial receipts, pagination totals, exact multipart/binary formats, signed URL isolation, CAS/preflight identity and interrupted exports. Mock SMTP/connectors/backend/queue side effects; database tests run only through the verified disposable worktree mode. Regenerate affected artifacts, review final diff and check installed optional extras after package changes. Do not repeat unaffected prior acceptance solely because work resumes.

Report local fixture/ASGI results separately from deployed runtime and remote CI. Preserve unrelated changes and the local-commit/push boundary. The overall goal remains active until all adopted public operations are implemented and verified.

## Current implementation receipt

All 52 concrete REST call sites are integrated: 463 covered, 95 uncovered, ten private executor exclusions and zero planned. Worker commits are f8752a7a863968b6996e142d4dcf2bd9936cd7d9 (batches), f116a52d7 (management), 02710a96288ee138a40bc1b6a789ca4a44315b47 (projects/performance/search), and 4d072f911 (datasets/guide assets). Temporary parent transport/output prerequisites were restored before each worker commit. The parent preserved earlier pending work while combining import conflicts; no originating-branch commit or push was made.

The implementation extends existing Projects/Batches/Datasets/Dashboard/Predictions/Annotations owners and adds four cohesive resources. Project and batch LiDAR preflight share export models; response models preserve unknown fields. Guide MIME schemas reflect the five actual image types, signed URL repr is hidden, prediction purge rejects unknown input, and annotation import emits its complete partial receipt before CLI exit 1.

API prerequisites reuse current authority: task search applies membership-role assignment/batch rules before LIMIT with the collaborative-video exception; scoped people counts/export/timeline stay in the selected project; batch audit checks the parent pair while retaining verified deleted history. Batch LiDAR preflight and export share export.annotations capability checks. Random splits reject more batches than splittable tasks before creation.

Local acceptance: final full SDK suite 927 passed; 109 distinct API cases passed across five JUnit receipts, including current-scope rejection before dispatch, owner/reviewer export allowance, canonical foreign-target precedence, deleted history, limit starvation, role-specific search, nonempty splits, performance, guide assets, annotation import and failed-prediction flows. DB tests ran only in the verified disposable worktree test mode. A new CLI fixture initially omitted its platform configuration; after correction, targeted partial-receipt checks and the final complete suite passed.

Four fresh wheel environments passed with warnings treated as errors: core, CLI, TUI import, and Python 3.11.14 / Pydantic 2.5.3 / HTTPX 0.27.2 / Typer 0.12.5 / Click 8.1.8. All resolved 474 SDK method descriptions and 617 public exports. Installed loopback checked paging/search, exact distribution preview/apply version and selection, purge dry-run, guide init/bare PUT/complete, synchronous annotation partial import, complete performance CSV export without page limits, three CLI partial-result exit paths, and refusal before a mutation. Core made ten fixture requests; CLI/TUI/lower-floor made 13 each, with no external services.

Wheel/sdist built with uv; all 143 packaged Python/guide/catalog files byte-match current source. Wheel SHA-256: 9eec6572d930b3d5fbcb3e121240ffc889154c6f9c30f39bbda1da2fff8c23da. Sdist SHA-256: e993a20cb806d77904b2905ec30af72c6cd9fcc37bbadb4ec48705a40169f7de. API snapshot, frontend artifacts and offline catalogue were regenerated and checked. Documentation build, 180 frontmatter files, 183 navigation pages and 30 SDK Python snippet syntax checks passed. Ruff, formatting, tracked diff whitespace and all 157 untracked-file whitespace checks passed.

The four clean worker worktrees were removed through Orca and their commits/branches retained. The disposable test database, dedicated Redis and nine owned buckets were destroyed using the verified mode confirmation; temporary wheel environments, loopback fixture and JUnit receipts were removed after recording these summaries. Parent HEAD remains 7ed68a5a667c43058917bdec5a9ee809cde052b1, index empty, cached origin/main divergence zero behind / two ahead. No deployed runtime, real SMTP/GPU inference or remote CI was exercised.

Dimension backfill's existing lack of ordering/cursor is documented rather than changed into an unsolicited API pagination feature. Failed width-null rows remain eligible; processed=0 with failures is not completion. Media backfill helps items missing thumbnails and returns no AsyncJob ID. Prior prediction-import upload-size handling is an existing API limitation, not a prerequisite for the new capped annotation import.

Next: [Annotation discussion and quality](1792179022_sdk-annotation-quality.md), followed by the 44 remaining task Workbench operations. No release milestone is assigned.
