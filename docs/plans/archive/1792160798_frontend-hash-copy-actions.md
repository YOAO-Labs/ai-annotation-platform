# Frontend hash and internal identifier copy actions

> Status: completed; local commit and acceptance follow-ups are recorded below.
> Baseline: `feat/frontend-opt` at `47197fd3b`, inspected on 2026-10-16.
> Scope: hashes and opaque UUID/internal IDs, authorized by the user's instruction to implement this plan.

## Goal

Replace visually exposed hashes with compact copy actions. Users should read meaningful names, statuses, versions, filenames, and measurements without having to scan technical identity strings. Full original identities must remain available through copying.

This plan is an implementation handoff in English, following repository instruction conventions. Product labels and the user-facing explanation remain Chinese.

## Recommended direction

Add one shared `CopyValueButton` using the existing Button, Tooltip, Icon, and Toast adapters. Move the current registry clipboard helper into a shared library and reuse it. Migrate the inventoried hash displays and opaque internal ID displays to this owner.

This is a small shared implementation with a medium breadth migration: **more than eight existing frontend files are involved**. Do not add a dependency, new setting, service, backend endpoint, database change, or release version.

The minimal alternative is replacing each display with a local icon button. Reject that alternative because clipboard failure handling, keyboard behavior, and event containment would be repeated across dozens of fields. Retaining a shortened hash plus a copy icon also leaves the visual noise the user asked to remove.

## Interaction contract

| Situation                       | Required behavior                                                                                                                                                                             |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Hash present                    | Render a copy icon in place of the hash text. Keep a nearby meaningful field label, such as 权重 or 配置摘要.                                                                                 |
| Opaque internal ID present      | Render the same copy action beside the object's name or labeled ID field.                                                                                                                     |
| Pointer hover or keyboard focus | Tooltip reads **复制哈希** for hashes or **复制 ID** for internal IDs. Give the button an accessible name identifying the specific field, for example 复制权重哈希.                           |
| Activation                      | Copy the complete original string. Preserve `sha256:` prefixes and UUID formatting; never copy an abbreviated display or surrounding prose.                                                   |
| Success                         | Announce 已复制哈希 or 已复制 ID through the existing toast adapter. Keep the interaction stateless unless a caller genuinely needs extra feedback.                                           |
| Failure                         | Show 复制失败，请检查浏览器剪贴板权限. Do not report success before the write completes.                                                                                                      |
| Missing value                   | Keep the existing empty-state wording, such as — or 尚未生成. Render no active copy action for it.                                                                                            |
| Keyboard                        | Native button semantics, visible focus, Tab navigation, Enter/Space activation, and Tooltip support on focus.                                                                                 |
| Clickable parent                | Copying must not navigate, download, select another object, submit a form, toggle a disclosure, or invoke an annotation action.                                                               |
| Dense layout                    | Use the existing compact button styling and semantic tokens. Keep the action discoverable without requiring hover on an entire row; verify usable targets on touch layouts.                   |
| List distinction                | Keep meaningful names, filenames, source descriptions, timestamps, frame numbers, ordinals, and version numbers. Resolve duplicate names before hiding the only visible distinguishing value. |

Illustrative product presentation:

- 权重 SHA-256: [copy icon], with tooltip 复制哈希.
- 候选模型名称 [copy icon], with tooltip 复制 ID.
- Revision 3 · 当前 [copy icon], with tooltip 复制哈希.

These are presentation examples, not additional UI controls.

## Existing owners and required changes

- `apps/web/src/components/ui/Button.tsx`, `Tooltip.tsx`, `Icon.tsx`, and `Toast.tsx` already provide the required presentation, focus, icon, and notification behavior. Compose them; do not change their public APIs.
- `apps/web/src/pages/ModelMarket/registry/registryShared.ts:47` owns `copyToClipboard`, including Clipboard API and legacy copy fallback. Move it to `apps/web/src/lib/clipboard.ts`. Preserve the fallback and ensure temporary DOM cleanup even on failure.
- `apps/web/src/pages/ModelMarket/registry/registryUi.tsx:18` and `:59` contain `CopyableId` and `ShortCopyableId`. Both expose value text. Migrate callers to the shared action, then remove the unused implementations. `CopyableId` currently has no callers.
- `formatShortId` still serves human-readable disambiguation and issue text. Keep it where useful; removing displayed hashes does not justify deleting all ID formatting.
- `apps/web/src/pages/ModelMarket/runtime/InstanceDetailSheet.tsx:91` has a separate clipboard handler. Route its ID action through the shared owner when migrating this surface.
- Create `apps/web/src/components/ui/CopyValueButton.tsx`. Its data is the original value, its semantic kind is hash or ID, and its accessible context is the field label. Missing values are rendered by the caller so existing wording survives.
- Keep clipboard operations in the user's activation handler. Suppress containing row actions. Where a parent is itself a button or link, make the copy action a sibling instead of creating nested interactive elements.
- Native select options remain text-only. For the Tracker candidate selector, use a readable candidate ordinal with label and frame number, and place the selected candidate's hash copy action beside the selector.

The browser Clipboard API requires a secure context and can reject permission. This is why the existing fallback and an explicit failure state must be retained. See [MDN Clipboard.writeText](https://developer.mozilla.org/en-US/docs/Web/API/Clipboard/writeText).

## Hash display inventory

Paths below are relative to `apps/web/src/pages/`. Line numbers identify the inspected baseline; implementation must search the current file again.

| Surface                    | Files and inspected locations                                                                       | Migration detail                                                                                                                           |
| -------------------------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Training warehouse         | `Training/TrainingWarehouseCards.tsx:121,129`                                                       | Container digest and weight SHA; keep filenames, framework, and runtime capability labels.                                                 |
| Parameter identity         | `Training/TrainingParameterInventory.tsx:139`                                                       | Schema digest; keep the readable schema identifier and native source links.                                                                |
| Create and import training | `Training/TrainingCreatePage.tsx:1327,1951`                                                         | Original base-weight SHA and imported initialization digest; retain compatibility warnings and inspection actions.                         |
| Frozen training review     | `Training/TrainingRunReview.tsx:49-58,84-87`                                                        | Explicitly mark identity fields in the existing row definitions. Keep class lists, task labels, and numeric values visible.                |
| Training monitor           | `Training/TrainingRunMonitor.tsx:517,582,621`                                                       | Artifact SHA, origin digest, and config digest. Make artifact download and hash copy sibling actions.                                      |
| Training reuse             | `Training/TrainingReuseDialog.tsx:41,52,58`                                                         | Split mixed name/hash strings into a readable model name plus hash action.                                                                 |
| Initialization source      | `Training/TrainingInitializationSource.tsx:186,191`                                                 | Validation digest and weight SHA. Keep weight size and native class ordering visible.                                                      |
| Study creation and trials  | `Training/TrainingStudyCreate.tsx:269,275`; `Training/TrainingStudyPanel.tsx:231,279,327`           | Plan, per-trial config, and evaluation compatibility digests.                                                                              |
| Comparison                 | `Training/TrainingComparisonResults.tsx:114`                                                        | Best weight SHA; keep metric comparisons and source names.                                                                                 |
| Evaluation identity        | `Training/SampleEvaluationPage.tsx:591,593,595`; `Training/SampleEvaluationGallery.tsx:249,251,253` | Weight, benchmark, spec, original image, label, and derived-image identities. Preserve 尚未生成.                                           |
| Active learning            | `Training/ActiveLearningCreate.tsx:455`; `Training/ActiveLearningRound.tsx:313,315,317,435`         | Preview, round, model, inference environment, and sample digests. Keep thumbnail and dimensions.                                           |
| Model lineage              | `Training/ModelLineagePage.tsx:313-317`                                                             | The fact renderer mixes hashes, IDs, timestamps, and ordinary facts. Classify known identity fields explicitly; never replace all strings. |
| Learning policy            | `Projects/ProjectLearningPolicyPanel.tsx:485,574`                                                   | Current and draft initialization source digests.                                                                                           |
| Data version               | `Projects/TrainingDatasetInsights.tsx:1051`; `Projects/TrainingDatasetSampleComparison.tsx:190`     | Snapshot and compared content hashes. Preserve dataset version names and comparison behavior.                                              |
| Project models             | `Projects/ProjectModelsPanel.tsx:119,120`                                                           | Validation and weight identities; preserve model name, metrics, and class binding information.                                             |
| Mask repair                | `Workbench/shell/MaskRepairSheet.tsx:299,315`                                                       | Plan and result digests; retain receipt lifetime, batch status, and rollback checks.                                                       |
| Mask quality control       | `Workbench/shell/MaskQcPanel.tsx:642`                                                               | Replace digest-based option captions with distinct candidate ordinals; copy the selected full digest outside the select.                   |
| Sensor calibration         | `Workbench/stages/three-d/SensorCalibrationSheet.tsx:128,286`                                       | Current and historical revision digests; keep revision number, timestamp, current badge, and edit/conflict behavior.                       |

This inventory contains **22 existing hash-display files**. Internal hash calculations, query keys, validation checks, payloads, mask cache identities, and Blurhash image rendering are outside the presentation change.

## Internal ID scope and safeguards

Apply the same interaction to opaque identifiers displayed as metadata, with tooltip 复制 ID. A human-readable catalog key such as a base model name, schema name, device index, or task sequence is not automatically an opaque ID.

Known shared callers:

- `ModelMarket/registry/ServicePoolsSection.tsx:467,603`: service pool and instance IDs.
- `ModelMarket/registry/BackendInstancesSection.tsx:357,364`: instance and owning pool IDs.
- `ModelMarket/registry/GPUResourcesSection.tsx:286`: GPU resource ID; keep device index and node identity.
- `ModelMarket/registry/ProjectBindingsSection.tsx:303,355`: project and pool IDs; keep names.
- `ModelMarket/runtime/ServicePoolRuntimeTable.tsx:242`: service pool short ID.
- `ModelMarket/runtime/InstanceDetailSheet.tsx:114,365` and `EnvOnlyContainerCard.tsx:106`: instance and physical device identifiers.
- `Training/ModelLineagePage.tsx:309`, `Training/TrainingInitializationSource.tsx:170,182`, `Training/TrainingRunReview.tsx`, `Training/TrainingRunMonitor.tsx`, and `Projects/ProjectModelsPanel.tsx:116-118`: node, model version, dataset, run, and attempt references.

Additional opaque ID displays found by a bounded read-only audit:

| Surface                             | Files and inspected locations                                                                                                           | Migration detail                                                                                                                                                                                    |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Evaluation and deployment selection | `Training/SampleEvaluationPage.tsx:408,412,433`; `Training/SampleEvaluationCreate.tsx:257,439`; `Training/ActiveLearningCreate.tsx:307` | Model version, dataset, execution attempt, deployment, backend, and task UUIDs. Native option captions need a readable discriminator. Copy actions for checkbox rows must be siblings of the label. |
| Evaluation samples                  | `Training/SampleEvaluationGallery.tsx:188,218`                                                                                          | Sample-list rows are buttons. Keep sample ordinal/dimensions and place any copy action outside that button; copy the selected task ID in its detail header.                                         |
| Dataset lineage and preview         | `Projects/TrainingDatasetInsights.tsx:374,407,968`; `Projects/TrainingDatasetSamplePreview.tsx:309`                                     | Parent/version/task and annotation IDs. Preserve version names and sample/annotation context.                                                                                                       |
| Exclusions and task detail          | `Projects/ProjectLearningPage.tsx:1284`; `Projects/data-manager/DataManagerTaskActions.tsx:663`                                         | Task UUID fallbacks. Keep available human task display ID, filename, and exclusion reason.                                                                                                          |
| Object/track data manager           | `Projects/data-manager/EntityDataManagerLens.tsx:176,194,203`                                                                           | Track/annotation identities used for correlation. Keep class, task location, frame/range, and version; verify rows still have a visible distinction.                                                |
| Dashboard audit entries             | `Dashboard/AdminDashboard.tsx:457-458`; `Dashboard/DashboardPage.tsx:607-608`                                                           | Copy full target ID beside its target type and action description.                                                                                                                                  |
| Remaining model market metadata     | `ModelMarket/registry/BackendInstancesSection.tsx:793`; `Projects/ProjectModelsPanel.tsx:510,826`                                       | Instance ID and model/backend UUID fallbacks. Use the already available object names; preserve an explicit discriminator if names are missing.                                                      |

For unnamed objects, keep a contextual caption such as 第 2 次执行 or a filename. Preserve existing navigation links and use an adjacent copy action. If the supplied view has no meaningful caption, retain the existing necessary disambiguation text rather than reducing a list to identical icons or fetching names through a new API.

Preserve readable business IDs such as T-61, frame numbers, annotation sequence labels, revision/attempt ordinals, backend names, URLs, and hardware model names. Keep original values in editable inputs, exports, logs, and explicitly raw diagnostic/configuration views.

## Implementation order

1. Add the shared clipboard owner and copy action, with focused behavior tests.
2. Migrate all hash inventory entries and the shared registry callers. Replace mixed text/hash strings with explicit readable text and a copy action; preserve current query and mutation inputs.
3. Migrate the remaining opaque metadata IDs using the safeguards above. Recheck hash, SHA, digest, UUID, short-ID, and rendering-helper searches to catch indirect renderers.
4. Update affected behavior tests, user documentation, and the Unreleased changelog. Review the complete diff and finish the validation below.

These are steps in one scoped change. No data migration, new credentials, third-party account, API generation, or runtime worker refresh is required.

## Verification

### Automated behavior

- Add focused tests for the shared component and clipboard helper: full-value writes, prefixed digests, UUID values, async success, permission rejection, legacy fallback success/failure, and temporary-element cleanup.
- Verify keyboard activation and focus tooltip, a descriptive accessible name, no form submission, and no parent row activation.
- Keep empty values in caller tests; assert the existing sentinel and absence of an enabled copy action.
- Update tests that require a hash in page text to assert the actual copied full value. For example, `Projects/TrainingDatasetInsights.test.tsx:117` currently expects raw hash text in the comparison dialog.
- Exercise one representative training review, evaluation gallery, lineage mixed-fact renderer, registry row, artifact download, Tracker selection, and calibration revision case.
- Retain existing stale-owner, compatibility, frozen-identity, repair rollback, and calibration conflict tests. UI simplification must not change their request payloads or validation.

Run these repository commands after implementation:

- `pnpm --filter @anno/web typecheck`
- `pnpm --filter @anno/web lint` (includes the required CSS-token check)
- `pnpm --filter @anno/web test -- src/components/ui/CopyValueButton.test.tsx src/lib/clipboard.test.ts`
- Run the existing tests for the migrated Training, Projects, ModelMarket, and Workbench surfaces; use the actual changed-file set to select them.
- `git diff --check`

### Live browser acceptance

`pnpm` and `agent-browser` are installed. Servers, authentication, and runtime URLs were not inspected or launched during planning. Before live acceptance, read the project agent-browser skill and determine the intended worktree URL.

Check representative training, evaluation, dataset, model market, mask, and calibration views in light/dark mode and a narrow viewport. Confirm no ordinary hash text remains at migrated fields, names and status information are still clear, tooltip copy text appears on hover/focus, real clipboard contents match the complete source, and denied clipboard access gives a failure message. Confirm copying does not navigate/download/select and that normal row, download, selection, and edit actions still work. Inspect recent network requests and console errors for those exercised flows.

Do not reseed data or start expensive renderer qualification for this presentation change. Report browser acceptance separately from unit/static checks and remote CI.

## Documentation and completion

When implementing, update:

- `docs-site/user-guide/online-learning/index.md`: readable identity fields and full-value copy behavior.
- `docs-site/user-guide/superadmin/model-market.md`: ID copy actions beside named resources.
- `docs-site/user-guide/workbench/mask-brush.md` and `pointcloud-projection.md`: relevant repair/calibration identity copy behavior.
- `docs-site/dev/reference/design-system.md`: the shared copy action and accessibility contract.
- `CHANGELOG.md`: an Unreleased entry explaining reduced hash/ID clutter and retained full-value copying.

Append an Outcome section after implementation with actual validation evidence and documentation paths. Reverting this frontend change restores the previous presentation without touching stored identities.

## Planning evidence and risk

The baseline working tree was clean. This planning task inspected source, callers, existing tests, package scripts, instruction/plan conventions, and the Clipboard API documentation. No application source was modified and no build, application test, server launch, clipboard action, browser validation, commit, or push was performed.

The main assumption is that an opaque ID is supplementary metadata. Where it is the only way to distinguish or choose objects, hiding it without a readable replacement would impair usability. The plan preserves that distinction and places copy actions outside native select options and existing interactive parents.

## Outcome

- Implemented on `feat/frontend-opt` as working-tree changes. No final branch commit or push was requested. Isolated worker commits `7b7f885f8`, `641be05e2`, and `c70da9a9a` were integrated without creating commits on this branch; their clean task worktrees were removed and the worker branches retain the patch history.
- Added `components/ui/CopyValueButton.tsx` and `lib/clipboard.ts`, reusing existing adapters. Hashes and opaque IDs have generic hover/focus tooltips, specific accessible names, full original-value copying, success/failure feedback, and no parent-row activation or form submission. Missing values retain their existing wording.
- Migrated all 22 inventoried hash-display files and the listed internal ID surfaces. The follow-up rendering audit also found and migrated capability fingerprints in `ModelMarket/registry/CapabilityDriftReviewDialog.tsx`. Removed duplicated registry copy components and the old local clipboard helper. Preserved readable catalog keys, business display IDs, names, metrics, timestamps, versions, and raw diagnostic/configuration output.
- Added readable sample, deployment, source, object, and track ordinals where UUIDs had provided the only visible distinction. Native select/checkbox labels and artifact-download buttons keep copy actions as siblings. Tracker candidate labels retain model/revision/frame context while copying instance IDs separately.
- Automated validation covers **428 distinct tests**: Training 188, Projects 112, shared/Workbench/registry 73, runtime observation 26, and Dashboard 29. Initial failures from outdated text assertions, clipboard-test setup, ambiguous checkbox-name matching, and missing virtual-list geometry were corrected; the affected suites passed on rerun. Existing frozen-identity, stale-owner, qualification, recovery, repair, and calibration checks remain exercised.
- `pnpm --filter @anno/web typecheck` passed. `pnpm --filter @anno/web lint` passed, including CSS tokens, with five existing ESLint warnings and three existing z-index advisories. Changed files were formatted with the installed Prettier. `git diff --check` passed. No remote CI result is claimed.
- The user subsequently requested a complete logged-in integration environment and manual acceptance, authorizing dedicated test records. Provisioned the owned `e2e` mode with `pnpm dev:worktree`, leaving the primary services and shared environment symlinks untouched. Web runs at `http://127.0.0.1:3100`, API at `http://127.0.0.1:8100`; database `aap_wt_2c52d214e0c06e2f_e2e` is at head `0189`, with a dedicated Redis, nine owned buckets, and ordinary/maintenance workers. Final doctor reported healthy/running with no errors.
- Headed Chrome used the real login form and authenticated production routes, with persisted PostgreSQL records and real MinIO objects. Full values were compared against the real macOS clipboard, resolving the earlier fixture-browser readback limitation. Verified warehouse image identity, training configuration/artifact, dataset ID/snapshot/sample, model/lineage, evaluation, service pool/instance/details, object/track lists, Mask repair plan/region feedback, and current/historical calibration. The evidence ledger contains 19 API-to-clipboard matches, plus the warehouse prefixed-digest check. Normal download, preview, row detail, and dialog actions remained available; copying did not trigger them. Deliberately rejecting both clipboard paths produced the failure toast and removed the temporary textarea, after which both methods were restored.
- Applied the user's follow-up requirement that copy actions stay beside their field label or resource name. Updated the affected detail lists, headers, registry resource names, dataset identity rows, and Mask feedback author row. The 302-test related frontend rerun passed; final typecheck and lint passed with the same existing warnings. Desktop and 393-pixel evaluation rows were measured on the same visual line, without horizontal overflow or nested controls. Light/dark views were inspected. Workbench screens retain their existing desktop-width guard below 768 pixels; no mobile Workbench usability or physical touch-device qualification is claimed.
- Live acceptance exposed two existing blockers and fixed them: PostgreSQL `infinity` expiry caused a naive/aware datetime comparison failure in historical Mask comparison, and the enlarged camera portal obscured the calibration sheet. Expiry filtering now happens in PostgreSQL; the added unbounded-retention regression failed before the fix, and all seven `test_mask_qc_m3.py` cases passed afterward in the owned disposable `test` database. Ruff check/format passed. Live comparison and both immutable-content requests returned 200. The enlarged camera is hidden while calibration is open, then restored with the same camera on close; real screenshot and hit testing confirmed the copy controls are visible and unobscured. The test-mode Redis was stopped after tests; the e2e environment remains running.
- Evidence and user review instructions are in `.worktree/e2e/data/manual-acceptance.md`, with screenshots and clipboard checks in `.worktree/e2e/data/hash-copy-evidence/`. The final corrected flow produced no captured API 4xx/5xx requests or JavaScript page errors. The earlier primary-API and fixture-only acceptance is superseded by this logged-in environment.
- Training, model, and evaluation history uses explicitly seeded UI records and stored fixture artifacts; this acceptance does not claim a real GPU training or inference run. The Mask QC CPU scan actually ran to completion through the owned worker. No annotation repair was executed, and no task was approved/rejected. No remote CI, commit, push, or release was requested.
- User documentation: `docs-site/user-guide/online-learning/index.md`, `docs-site/user-guide/superadmin/model-market.md`, `docs-site/user-guide/workbench/mask-brush.md`, and `docs-site/user-guide/workbench/pointcloud-projection.md`.
- Developer documentation: `docs-site/dev/reference/design-system.md`. User impact is recorded under `CHANGELOG.md` Unreleased. No release version was assigned.

### Browser acceptance follow-up fixes

- Addressed the three issues confirmed by the delegated browser review: object-track cells now show distinct list-local ordinals, repeated IDs reuse their ordinal, and copy accessible names include the ordinal; the training draft summary uses a readable model caption with an adjacent full-ID copy action; warehouse resources retain the readable node label and copy the complete resource identity.
- Added five regression cases covering repeated tracks, appended rows, row selection isolation, initialized and unverified model captions, resource copying, and the unconfigured-resource sentinel. The three affected frontend suites passed all 76 tests. Typecheck and lint passed; lint retained the five existing warnings and three z-index advisories. `git diff --check` passed.
- Real headed Chrome revalidation confirmed both training changes and the object-table fix with API-to-system-clipboard comparisons. Buttons remained on the same line at desktop and 393-pixel widths. The object list retained ordinals after loading additional rows. An initially headless worker could not validate the native clipboard; its five failed probe records were retained and classified as a launch-mode error, then the two relevant controls passed in a verified headed session.
- An inadvertently expanded frontend run also exposed four failures in the unchanged chart/video modules. A targeted rerun of those two files produced 61 passes and four failures out of 65; no full-suite or remote CI success is claimed. These failures are recorded separately from the requested UI fixes.
- Current presentation inventory contains 40 files and 127 copy-action call sites. The earlier review's missing-data branches remain unqualified. Evidence: `.worktree/e2e/data/three-fix-acceptance/report.md`. Updated user guidance in `projects/data-manager.md` and `online-learning/index.md`, plus the Unreleased changelog. No commit or push was requested.

### Test infrastructure follow-up

- The user subsequently requested a local commit followed by resolution of the four test failures. The presentation changes were committed first as `282112fbd`; all commit hooks passed.
- Root cause: the global no-op ResizeObserver added for Radix Tooltip made layout-sensitive tests take their observer-enabled path. Recharts replaced its initial dimensions with jsdom's zero-sized rectangle, while the video tests' window resize events no longer notified the viewport owner. An isolated setup probe disabling that observer made all four failing tests pass, without modifying production code or the shared checkout.
- Chart tests now provide measured container bounds and retain their real SVG-click assertions, including stored attribute values rather than display labels. Video fixtures register the actual observed viewport node and explicitly deliver its resize callback; restoration, fallback-dimension rejection, native metadata adoption, and retired-source guards remain asserted.
- The sibling sweep found two Recharts bar queries and three window-resize emulations; all five affected sites are covered by the fixture corrections. The two remaining viewport rectangle spies test pointer geometry directly and do not depend on a resize notification. Summary-only chart smoke tests remain unchanged.
- The two target files passed all 65 tests. The complete frontend suite then passed all 5,994 tests, with zero failures and zero skipped tests. No production behavior, validation gate, or test expectation was relaxed. Remote CI and push were not requested.
