# Context toolbar rollout for centered Workbench panels

> Status: implemented and locally verified on 2026-09-09; no commit or publication requested.
> Baseline: `88e5bc94`, branch `feat/toolbar_opt`, inspected on 2026-09-09.
> Foundation: [PR82](https://github.com/yyq19990828/ai-annotation-platform/pull/82), merged; its head `6d23684f` is an ancestor of the local baseline.

## Objective and boundaries

Reduce canvas obstruction from centered contextual panels while retaining direct access to required inputs, candidate decisions, progress, and recovery. Reuse `shell/ContextToolbar.tsx`: left capsule, hover/focus quick actions, centered full panel, reversible animation, non-modal dismissal, and focus restoration.

The minimum option is migrating only `SecondaryInferenceBar` using the existing interface. Recommended: three independently mergeable deliveries, starting with that migration and the small presentation extension it needs, then interactive AI, then tracker review. This orders work by state and transaction risk. No new packages, services, commands, environment variables, preference schema, API endpoints, or credentials are required by the feature. Existing development authentication and a disposable seeded database are needed for integration acceptance.

Do not unify all floating UI. Dockview owns task/AI/tracker configuration panels; `FloatingPanelShell` owns draggable cards and native floating views. Camera views, PSR, tri-view panels, dialogs, timeline controls, sticky-track hints, and the continuous-creation status are outside this migration. Keep their input, rendering, and persistence contracts. Include the continuous-creation status in overlap acceptance.

## Evidence and decisions

- PR82 explicitly keeps drafts, commands, settings, persistence, and asynchronous work outside `ContextToolbar`. Full-panel children unmount after the closing animation.
- `InteractiveToolBar` already has an accepted primary/advanced split. Its required inputs, candidate actions, and recovery cannot disappear behind an advanced disclosure. See [E1 record](1788769060_workbench-e1-ai-toolbar-layers.md).
- `SecondaryInferenceBar` owns selected capability, per-model parameters/variants, temporary text, and busy state. `useSecondaryParamPrefs` owns persisted parameters. Keep the component mounted when only its panel closes.
- `VideoTrackerReviewBar` owns pending intent protection and the manual-keyframe override confirmation. Those must survive presentation changes. Its review projection remains owned by `useVideoTrackerJobs` and existing review scope logic.
- Existing render conditions are not fully exclusive: capability recovery can coexist with secondary inference, and Mask is not excluded by the secondary bar's non-AI gate. Resolve placement deliberately rather than putting several capsules at `left-3 top-3`.
- Keep the local Radix Popover adapter. Official [Popover documentation](https://www.radix-ui.com/primitives/docs/components/popover) already provides non-modal behavior, controlled state, focus callbacks, anchors, and collision boundaries. Do not build another popup controller or replace the dependency.

Required input and scoped decisions belong in the capsule disclosure and remain reachable without opening full settings. The full tracker scope editor stays in the settings panel.

## Shared presentation contract

Extend `ContextToolbar` with one optional `primaryContent: ReactNode` slot. It appears below the summary only while the capsule disclosure is open and is hidden with that shell while the full panel is open. It is a sibling of the summary button, never nested interactive content inside that button. Existing Mask behavior is unchanged when the slot is absent.

- `summary` stays concise and non-interactive: tool/object, polarity or scope, pending count, busy/error status. Use a restrained live status region; do not announce every input change.
- `primaryContent` contains the minimum required inputs and consequential current-session actions. Allow wrapping in the actual canvas width. Long identity labels truncate with an accessible full label; counts and scope remain legible.
- `quickActions` remains optional-in-practice via an empty array and contains simple commands/toggles. It does not become a schema-driven form or business command registry. Keep the More button reachable on narrow canvases; avoid clipping it behind a `w-max` quick-action row.
- Full panels group inputs and settings vertically within the existing width cap. Secondary parameters become an inline disclosed section, removing the absolute child popup that would be clipped by the full panel's scrolling container.
- Use the actual canvas host as the full-panel collision boundary; derive available dimensions using Radix support and cap width/height accordingly. Resolve the anchor's host inside the presentation component without adding a global geometry store. Test docked and floating canvas hosts.
- Keep all primary/quick/full input inside the shared interaction guard. Test keyboard events from inputs and native selects, IME, Escape, and portal descendants. Closing via Escape restores capsule focus; clicking the canvas closes without stealing focus and preserves the existing first-click drawing behavior.
- Display toggles never invoke save, cancel, inference, preference setters, or owner resets. Display state is ephemeral and does not enter workspace snapshots.

Presentation eligibility is derived in the existing Shell composition, not stored in a new registry: active tracker review takes precedence over Mask, active interactive AI, secondary inference, and capability-recovery-only UI, in that order. Retain original stage, permission, hidden-preference, and seed-collection gates. Do not hide any independently active candidate decision flow: tracker-review activation must continue through existing session/dirty guards. Suppress competing presentation without destroying business owners. When a temporarily suppressed presentation returns, it starts compact.

State direction stays one-way:

```text
Existing task/tool/preference/review owners
    -> existing Shell composition: presentation eligibility
        -> SecondaryInferenceBar / InteractiveToolBar / VideoTrackerReviewBar
            -> ContextToolbar: display, geometry, focus only
                -> existing callbacks back to the same business owners
```

## Delivery 1: secondary inference and compact primary slot

Files: `shell/ContextToolbar.tsx`, `shell/ContextToolbar.module.css`, `shell/SecondaryInferenceBar.tsx`, `state/useWorkbenchShellModel.tsx`, relevant guard tests and component tests, and a new `e2e/tests/workbench-context-toolbars.spec.ts`. Paths without prefixes are relative to `apps/web/src/pages/Workbench/`; E2E paths are relative to `apps/web/`.

- Capsule: secondary inference, current annotation class, selected capability, busy status or missing-input hint.
- Compact primary area: Run, with the same busy/text prerequisites as the full panel. Required target text remains directly editable when the selected model requires it. Switching capabilities stays in the full panel.
- Full panel: capability groups, write-target explanation, variants, target text, inline parameters, missing attribute-field recovery, Run, and Collapse. Both Run surfaces call the same handler with the same payload and a synchronous single-flight guard.
- Keep selection, parameters, variants, prompt, and pending request state outside the full-panel children. Add a presentation key at the inner toolbar boundary for task/annotation identity; do not key the entire bar and erase its per-model state. Closing/reopening retains temporary text; persisted values still use the original preference owner.
- Snapshot the originating annotation/task for each run. A result for A may refresh A's cache but cannot clear B's busy state, label B as completed, or submit against B after selection changes. Preserve existing shared mutation/cache ownership.
- Distinguish Collapse from the existing account preference that disables the secondary panel altogether. All existing preference entry points still control the same value.
- Apply the existing Mask/AI/recovery eligibility fix in this delivery, with a regression for Mask plus selected annotation and recovery plus a valid secondary capability.

Acceptance: zero requests on hover/open/close; exactly one request on rapid double Run; params and variants survive collapse, selection changes, and reload according to existing persistence; blank required text disables Run; missing capability/read-only/hidden preference yields no entry; failure leaves values available for retry. Verify produced attributes/children after reload.

## Delivery 2: interactive AI

Files: `shell/InteractiveToolBar.tsx`, `state/useWorkbenchShellModel.tsx`, `shell/InteractiveToolBar.layers.test.tsx`, `shell/InteractiveToolBar.exemplar.test.tsx`, shared toolbar/guard tests, and `e2e/tests/workbench-ai-toolbar-layers.spec.ts`.

- Capsule: tool, polarity, output geometry, candidate index/total, and distinct capability/inference status.
- Compact primary area: current required text; candidate Previous/Next, Accept, and Cancel when relevant; appropriate recovery action on error. Capability negotiation retry and inference retry retain separate callbacks and status. Preserve pending/lock disabling and the existing acceptance class-picker flow.
- Quick actions: available polarity toggle and supported output toggles. Respect model capability restrictions, including models that cannot use negative exemplars.
- Full panel: the existing primary controls followed by the existing advanced section for backend, model, variants, threshold, and diagnostics. Keep advanced disclosure state outside popup children; controlled values remain with their current owners.
- Key only toolbar presentation by actual task/tool/session ownership. Frame or refinement-source changes close stale presentation according to existing session identity; candidate index and parameter edits are not new owners. Do not create a competing session counter.

Acceptance: image Point/Box/Exemplar/Scribble and supported video tools work from compact UI; inputs and recovery remain reachable without More; accepting/cancelling targets the current candidate once; collapse never reruns inference or clears prompts; model configuration reaches the real request; late responses after task/frame changes cannot overwrite the current owner. Re-run the existing E1 browser paths with the new compact/full locations.

## Delivery 3: video tracker candidate review

Files: `stage/VideoTrackerReviewBar.tsx`, `stage/VideoTrackerReviewBar.test.tsx`, `shell/WorkbenchShell.tsx`, `state/useWorkbenchShellModel.tsx`, and `e2e/tests/video-tracker-review-scope.spec.ts` / `video-tracker-local-review.spec.ts`.

- Keep tracker review in its existing canvas overlay host; migrate presentation through `ContextToolbar` and complete the tracker precedence rule.
- Capsule: propagation/correction identity, selected instance count, frame window, selected pending count versus job pending count, and submitting status.
- Compact primary area: explicitly labelled Accept selected and Reject selected, using the current review scope. Disable invalid/empty/stale/pending scope exactly as the full panel does. Quick actions may seek the selected window start or refresh; neither changes scope nor accepts anything.
- Full panel: job choice, instance selection, frame window, correction provenance, manual-frame protection, refresh, and existing scoped decisions. Never replace selected-scope semantics with whole-job acceptance.
- Keep `pendingIntentsRef`, pending state, latest ownership checks, and the override confirmation in `VideoTrackerReviewBar`, outside popup content. Use task/job identity for presentation reset, not every `intentKey` revision; scope edits must not remount the form or release pending protection.
- Collapse is display-only: no rejection, job cancellation, scope reset, or confirmation. The compact scope and actions remain available through hover/focus throughout review; restoring expanded configuration is always possible.

Acceptance: partial acceptance leaves the remaining candidates; rapid duplicate decisions submit once; switching job/task while confirmation or request is pending cannot overwrite another scope; protected manual frames still require the existing explicit confirmation; collapse/reopen preserves selection; completed or failed decisions update the correct job; persisted tracks survive reload. Check coexistence with Mask correction and seed collection through existing guards.

## Verification and documentation

The complete rollout touches more than eight files, including tests and documentation. Deliveries are separate reviewable patches, each usable without the next. Extend existing tests where they exercise behavior; no implementation-mirroring test suite or new test framework.

Run targeted suites for each delivery, then required static checks:

```bash
pnpm --filter @anno/web test src/pages/Workbench/shell/ContextToolbar.test.tsx src/pages/Workbench/shell/SecondaryInferenceBar.test.tsx src/pages/Workbench/state/workbenchInteractionGuards.test.ts
pnpm --filter @anno/web test src/pages/Workbench/shell/InteractiveToolBar.layers.test.tsx src/pages/Workbench/shell/InteractiveToolBar.exemplar.test.tsx
pnpm --filter @anno/web test src/pages/Workbench/stage/VideoTrackerReviewBar.test.tsx
pnpm --filter @anno/web typecheck
pnpm --filter @anno/web lint
pnpm --filter @anno/web lint:css-tokens
git diff --check
```

Run only the relevant delivery's test command plus shared regressions; full Web lint already includes CSS tokens, so the explicit token command need not be repeated after that passes. During implementation, inspect the current E2E configuration and runtime skill, verify a disposable test database, and use the affected Playwright specs with the correct isolated runtime. Do not copy historical ports from old plans.

Shared real-browser acceptance: light/dark; 1440×900 and 390×844 viewports; canvas narrowed by side docks; dock/float/redock; mouse drag crossing the capsule without hover expansion; touch click and keyboard access; fast animation reversal; reduced motion; scroll/resize during expansion; select and nested confirmation behavior; continuous-creation status overlap; console and API error inspection. Record whether ML responses are fixtures. Unit tests do not establish browser or backend acceptance.

After every test run, remove this run's reports, traces, temporary configurations, seed data, and caches. Stop only processes started for this task. Retain only explicitly delivered evidence and never alter shared environment/dependency symlinks.

Update documentation within each implementation delivery:

- Shared architecture: `docs-site/dev/concepts/workbench-shell.md`.
- Delivery 1: `docs-site/user-guide/ai/current-task-inference.md`, plus affected workbench index/settings descriptions.
- Delivery 2: `docs-site/user-guide/workbench/sam-tool.md`.
- Delivery 3: `docs-site/user-guide/workbench/video-propagate.md` and `video-track.md` where review instructions change.
- Each user-visible delivery: `CHANGELOG.md` Unreleased; append actual results to this plan after implementation. Documentation screenshots are updated through the media skill only if affected media is part of the delivery.

Rollback is a frontend patch revert in reverse delivery order, without data deletion or preference migration. Do not revert persisted annotations or tracker decisions. Implementation was subsequently authorized. Commits, remote comments, publishing, and release version assignment remain outside the requested follow-through.

## Implementation adjustments

- Source inspection disproved the assumed tracker activation guard: the review owner automatically chooses incoming candidates. Active Mask/AI tools and pending editing therefore retain priority; the tracker capsule appears after editing is completed/cancelled and the selection tool is active.
- Model/backend/output edits are configuration changes within the same settings surface, so they do not close the panel. Task, tool, video frame, and refinement-source changes still reset presentation.
- Browser reproduction caught a hook-order crash during loading-to-ready transition: the newly added capability query originally followed Shell early returns. It now executes before every loading/empty return. The same image browser flow subsequently passed and the root cause is covered by the existing Rules of Hooks lint and full-page loading acceptance.

- The user explicitly corrected the presentation direction: idle must contain only one small capsule. All primary inputs and decisions now live inside its hover disclosure. A single 160 × 36 px header and unified expanding surface replace the large permanent primary cards. This supersedes earlier always-visible-primary assumptions.
- The Workbench intentionally blocks phone-width interaction with its existing mobile dialog. Responsive browser acceptance uses the supported 1024 px viewport; phone interaction is not claimed.

## Outcome

- Landed commits: 未提交（本计划仅在本地实现与验证；状态为 no commit or publication requested）
- Release milestone: Not yet determined
- User documentation: `docs-site/user-guide/workbench/`
- Developer documentation: `docs-site/dev/concepts/`、`docs-site/dev/reference/`
- ADR: 无
- CHANGELOG: 未添加（未提交 / 未发布）
- Remaining work: 见下方 Follow-up 段落

- The final presentation is a single 160 × 36 px idle capsule. Hover/focus/touch reveals one downward-growing surface with labelled frequent actions and a final More entry. Required inputs and decisions are hidden while idle; their state remains with the existing owner.
- Related unit coverage totals 137 passing cases across the shared toolbar, Mask, secondary inference, interactive AI, tracker review, eligibility, guards and secondary cache ownership. After the last presentation edits, the affected shared/Mask and primary-owner suites passed again.
- Web type checking, Web lint/CSS token validation, and `git diff --check` passed. The temporary browser runner's untracked configuration produced one explicit-any lint warning and was removed after testing.
- Real Chromium acceptance covered image/video candidate controls and pixel-identical persistence, backend/model variants, separate recovery paths, saved-mask scribble, local tracker decisions and manual-frame confirmation, real secondary ROI/child writes and duplicate-run protection, and bbox modifier compatibility. Capsule rectangles remained 160 × 36 through 30-frame hover samples and full-panel open/close. Earlier tracker scope runs covered revision conflicts and late job/task responses.
- Secondary screenshots were inspected in idle and hover states. Supported desktop widths 1440 and 1024 were exercised in light/dark themes. The 390 px attempt correctly encountered the existing mobile-workbench blocking dialog; it is not treated as supported mobile acceptance. Full-panel screenshots caught transition frames and are not publication-ready media.
- The model HTTP transport used test fixtures. API persistence and secondary ROI processing were real against a verified disposable database. Dedicated API/Vite processes, test database, generated media objects, temporary configurations and local evidence files were cleaned after runs; shared development services and dependency/environment symlinks were left intact.
- Existing documentation recording/screenshot entry points were migrated, but published screenshots/videos were not re-recorded or re-approved. Dock/float/redock and a dedicated real-browser touch/reduced-motion matrix were not separately rerun. No remote CI, commit, push or release was performed.

## Follow-up: intrinsic idle width

The user refined the compact geometry after screenshot review: idle width should fit only the icon and key information, while hover may expand both right and down. This supersedes the fixed 160 px idle-width decision. The shared component now observes the intrinsic summary width, excludes the disclosure chevron from idle layout, and animates width alongside disclosure height with a shared 160 ms curve. The top-left anchor remains fixed; expanded width is at least 160 px.

Follow-up verification: 28 shared/Mask tests passed. A temporary real Chromium component harness checked four summary lengths, simultaneous horizontal/vertical interpolation, a fixed top-left anchor, three rapid reversals, full-panel open/close and reduced motion. Measured sample idle widths were 75 px for icon + `16 px` and 92 px for four Chinese characters, expanding to 160 px. This was presentation-component validation, not another seeded end-to-end business run. Temporary harness files, server, cache and inspected screenshot were removed.

## Follow-up: AI state summaries and settings hierarchy

The user's new screenshot rejected tool names as capsule content and the crowded full AI settings row. The current design preserves the intrinsic-width capsule and its shared motion, and specializes its summary by actual tool semantics:

| Tool           | Summary                                                                                 | Capability constraint                                                                                                 |
| -------------- | --------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Smart point    | Positive/negative marker, persistence geometry or refinement target, candidate position | Marker shows default polarity, not temporary Alt state                                                                |
| Smart box      | Persistence geometry or refinement target, candidate position                           | Box prompt does not carry negative polarity                                                                           |
| Smart scribble | Positive/negative marker and current refinement target                                  | Existing Mask ownership remains with the prompt session                                                               |
| Exemplar       | Positive/negative marker, current supported text and recall shape, candidate position   | Hide unsupported negative/text state; hide persistence controls for box-only recall without changing the stored value |
| Magic box      | Rectangle output and candidate/processing status                                        | No negative box or Mask persistence control                                                                           |

Tool icons remain; names are available through accessible labels/tooltips and the full panel heading. Busy and failure states use distinct small icons. Long summaries reserve extra room for the disclosure arrow only when expanded.

The full interactive panel uses the shared compact 448 px width, a tool heading and task instruction, labelled prompt fields, a dedicated candidate decision row, and a separate model/parameter disclosure. Empty initial sessions have no dead candidate buttons. Existing prompts that yielded no candidates retain Cancel: the Shell passes a read-only `hasPromptSession` projection from the existing points/scribbles/exemplars arrays. No new session owner or backend payload is introduced.

Unit regressions cover all five summaries, unsupported exemplar features, text/candidate updates, box-only persistence visibility and cancellation after zero results. A real Chromium component harness checked 24 tool/theme/state combinations, including both polarity colors, advanced selection, text persistence across collapse and a narrow error panel. This is component visual/interaction evidence, distinct from the subsequent seeded image/video business regression.

Final AI redesign verification: 61 relevant unit cases passed (26 layers, 7 exemplar, 6 shared toolbar, 22 Mask); all 6 seeded E1 image/video browser cases passed, including native-pixel persistence, model/variant request propagation, separate capability/inference recovery and saved-Mask scribble. Final long-summary hover and error snapshots passed an additional focused browser check. Type checking, targeted ESLint, CSS token validation and `git diff --check` passed. Test databases, model/media seed artifacts, temporary runtimes/configurations, caches and inspected screenshots were cleaned. No commit, remote CI or published media refresh was performed.

### Follow-up: confidence access and secondary inference parity

- Expose the existing exemplar confidence control in the hover disclosure, retaining its owner callback and backend-default reset.
- Secondary inference reuses the interactive toolbar's compact panel width, heading hierarchy, field geometry and Mask-style X button. Its capsule summarizes the selected capability and current text.
- Reuse SchemaForm for editable numeric confidence parameters selected by the platform role, with a narrow fallback for legacy confidence keys. Preserve other per-model values and defaults when editing the subset; do not add parameters absent from the advertised schema.
- Keep complete parameter access, model variants, field-completion warnings and the existing request single-flight/owner guard. Opening either surface must not trigger inference.

Verification for this follow-up: 52 focused component tests passed, including confidence subset preservation, model switching, default reset and close/reopen continuity. A Chromium component harness checked both toolbars in light/dark themes at 1000px and 480px viewports (8 combinations), inspected rendered screenshots, and verified the secondary request payload through intercepted HTTP responses. This was isolated component/browser verification; the database-backed secondary E2E was updated for the new quick confidence path but was not rerun in this follow-up. TypeScript, targeted ESLint, CSS token checks and diff whitespace checks passed. Temporary harnesses, servers, caches and screenshots were removed after inspection.
