# Decision dialog consolidation (confirm / alert / reason / leave guards)

> Status: approved — execution started 2026-09-17 via orchestrated waves T0–T4 (see [Execution](#execution)).
> Date: 2026-09-16 (draft), 2026-09-17 (approved)
> Requested outcome: one owned, theme-aware **decision-dialog** capability for confirmation, destructive actions, required-reason input, and leave guards; replace native `window.confirm` / `window.alert` / `window.prompt` and unify the bespoke reason modals so the same capability has one visual language with a premium, on-brand finish.
> Motivation: `window.prompt` was silently suppressed in an embedded browser, so the Review-page batch reject sent **no request at all** (fixed in `2ee3bc16`). The same failure mode applies to `alert` and, on some embedded/enterprise/automation browsers, to `confirm` — where a blocked confirm turns a destructive action into a silent no-op.
> Non-goals: content dialogs (`components/ui/Modal`), rich multi-field forms, a dialog framework rewrite, OS notifications.

## Findings

### Native blocking dialogs still in use

- **`window.prompt`: 0 real call sites.** Only ReviewPage used it (`pages/Review/ReviewPage.tsx:557`), replaced with `RejectBatchModal` in `2ee3bc16`. Remaining grep hits are test titles.
- **`alert()`: 2 real call sites**, both in `components/datasets/ImportDatasetWizard.tsx:638,642` (`.zip`-only and size-limit validation). Remaining grep hits are Markdown/XSS test fixtures. `window.alert` has no other callers.
- **`confirm()` / `window.confirm()`: ~28 call sites across ~18 files.** Full inventory in [Migration inventory](#migration-inventory). Four of these are the same logical capability implemented three different ways:
  - `pages/Settings/useUnsavedSettingsGuard.ts:19` and `pages/Settings/SettingsPage.tsx:104` duplicate the same "unsaved settings, leave?" copy.
  - `pages/Workbench/state/useWorkbenchShellModel.tsx:3139,3197` pass `window.confirm` as the `confirmFn` into `promptMaskLeaveChoice` (`pages/Workbench/state/useMaskEditorSession.ts:47`).
  - `pages/Workbench/stages/image/useImageAnnotationActions.ts:1250` passes `window.confirm` into `promptEmptyRasterMaskChoice` (`…/useImageAnnotationActions.ts:224`).

### Existing dialog surfaces already fragment

- `components/ui/Modal.tsx` is a locally owned content dialog (43 call sites).
- `components/shadcn/ui/alert-dialog.tsx` is the Radix AlertDialog primitive, already imported by 9 files (including workbench `useWorkbenchShellModel.tsx` and `stage/MaskConversionDialog.tsx`), each wiring its own `open` state and footer buttons inline.
- Several overlays use ad-hoc `createPortal(..., document.body)` (e.g. `shell/OfflineQueueDrawer.tsx`, `shell/ClassPickerPopover.tsx`, `stages/three-d/ThreeDWorkbench.tsx`).
- There is **no shared imperative confirm/alert/reason helper** today. `grep` finds no `useConfirm` / `confirmDialog` / `ConfirmDialog`.

### Mount and test seams to reuse

- `App.tsx` renders `<ToastRack />` twice — once for the normal shell (`App.tsx:202`) and once for `FullScreenWorkbench` (`App.tsx:248`). A decision-dialog host must follow the same dual-mount rule; the full-screen Workbench routes do not mount `AppShell`.
- `useWorkbenchShellModel` already abstracts the leave decision behind `onLeaveDirty?: (...) => Promise<MaskSessionGuardChoice>` (`useMaskEditorSession.ts:62`), a usable async seam.
- `window.beforeunload` is registered in 5 files (e.g. `Settings/SystemSettingsSection.tsx:696`, `Workbench/state/useWorkbenchShellModel.tsx:3271`). **`beforeunload` must stay native**; the browser forbids async dialogs there.
- 8 e2e specs handle native dialogs with `page.on("dialog")` plus message sniffing (e.g. `e2e/tests/mask-primary-actions.spec.ts:251` accepts only when `dialog.message().includes("丢弃")`).

### Value assessment (why this is not a hotfix)

| Dimension                           | Value       | Note                                                                                                          |
| ----------------------------------- | ----------- | ------------------------------------------------------------------------------------------------------------- |
| Visual consistency / premium finish | High        | Native dialogs cannot follow `data-theme`, `--sc-*` semantic tokens, type scale, spacing, or motion.          |
| Robustness                          | Medium      | `prompt` already proven blocked; `alert` can be swallowed; a blocked destructive `confirm` is a silent no-op. |
| Testability                         | Medium-high | Replaces brittle `dialog.message()` sniffing with deterministic selectors.                                    |
| Docs / screenshots                  | Medium      | Native dialogs cannot be captured; in-app dialogs can join the docs media pipeline.                           |
| Accessibility                       | Medium      | Radix `AlertDialog` supplies focus trap, roles, and ESC; native is usable but uncontrolled.                   |
| Effort / risk                       | Medium      | ~28 sites; risk concentrated in Workbench leave/delete guards and the async conversion.                       |

The concrete regression risk today is limited to `alert` (2) and future embedded-browser suppression; the larger payoff is a single visual language and a lower-maintenance confirmation path.

## Recommendation and scope

Build **one** decision-dialog capability and migrate in phases. Do not mix it with the batch-flow change already landed in `2ee3bc16`.

Define two dialog classes and keep them separate:

1. **Decision dialog** (new, this plan): short, blocking decisions — confirm, destructive confirm, multi-choice, required text input. One imperative service and one host.
2. **Content dialog** (`components/ui/Modal`, unchanged): forms, previews, multi-section content.

Implementation shape:

- Back it with the existing Radix `components/shadcn/ui/alert-dialog.tsx` primitives.
- Expose an **imperative, promise-based service** so call sites in hooks and non-React modules stay one line and do not need JSX:
  - `confirmDialog(options): Promise<boolean>`
  - `choiceDialog(options): Promise<ChoiceKey | null>` (multi-button, replaces the two-sequential-`confirm` pattern)
  - `inputDialog(options): Promise<string | null>` (required-reason; replaces `window.prompt` and subsumes `RejectBatchModal` / `RejectReasonModal`)
  - `alertDialog(options): Promise<void>` (replaces `window.alert`, e.g. validation notices)
- Store: a zustand store keyed like `Toast` (`components/ui/Toast.tsx` uses `create(...)` + `ToastRack`), resolved through a single `<DecisionDialogHost />`.
- Host mount: `App.tsx`, next to both `<ToastRack />` renders (`:202` and `:248`), so the Workbench routes are covered.

The end state: one component tree, one copy voice, one set of focus/keyboard semantics, and no native decision dialogs except `beforeunload`.

## API sketch

```ts
// apps/web/src/components/ui/decisionDialog.ts
export type DecisionTone = "default" | "danger";

export interface ConfirmDialogOptions {
  tone?: DecisionTone;
  title: string;
  description?: string; // plain text, wraps
  details?: React.ReactNode; // affected counts / names, optional
  confirmLabel: string; // verb, e.g. "删除数据集"
  cancelLabel?: string; // default "取消"
  icon?: IconName; // default "warning" for danger
  // Destructive dialogs focus Cancel by default; neutral ones focus Confirm.
  defaultFocus?: "confirm" | "cancel";
}

export function confirmDialog(o: ConfirmDialogOptions): Promise<boolean>;

export interface ChoiceOption {
  key: string;
  label: string;
  tone?: DecisionTone;
  description?: string;
}
export function choiceDialog(o: {
  title: string;
  description?: string;
  options: ChoiceOption[];
}): Promise<string | null>;

export function inputDialog(o: {
  title: string;
  description?: string;
  label: string;
  placeholder?: string;
  required?: boolean;
  maxLength?: number;
  confirmLabel: string;
  tone?: DecisionTone;
  validate?: (value: string) => string | null; // inline error
}): Promise<string | null>;

export function alertDialog(o: {
  title: string;
  description?: string;
  details?: React.ReactNode;
  confirmLabel?: string;
}): Promise<void>;
```

Call-site shape (importable from hooks and modules):

```ts
if (!(await confirmDialog({ tone: "danger", title: "删除数据集", confirmLabel: "删除" }))) return;
const reason = await inputDialog({
  title: "整批退回",
  label: "退回原因",
  required: true,
  maxLength: 500,
  confirmLabel: "确认驳回",
  tone: "danger",
});
if (!reason) return;
```

Optional convenience hook `useDecisionDialogs()` is not required; keep one service to avoid two entry points.

## Visual and premium spec

- **Tokens only**: use semantic utilities and `--sc-*` (e.g. `text-status-danger`, `bg-status-danger-soft`, `border-border`, `bg-card`); no bare/arbitrary colors. Run `pnpm --filter @anno/web lint:css-tokens`.
- **Danger treatment**: restrained — status-colored icon and confirm button, soft danger background for the icon container, no full-bleed red.
- **Copy voice**: title states the action and object (`删除数据集`), description states the consequence and scope, confirm uses the verb. Templates live in the service consumer, not in the component.
- **Details slot**: surface counts/names where available (`将删除 3 个数据集 · 1,204 张图片`), which native dialogs cannot do.
- **Focus and keyboard**: destructive → focus Cancel; neutral → focus Confirm; Esc and overlay click cancel; `AlertDialog` handles roles and focus trap.
- **Pending state**: the service is boolean-only (decision [1](#decisions)); the dialog never shows a pending state. Callers keep owning their spinners/loading states exactly as they do today.
- **Motion**: reuse existing `data-[state=open]` animation utilities and respect reduced motion; keep `z-modal` layering consistent with `Modal`/`Toast`.
- **Icons**: Lucide via `Icon`; default `warning`, allow `trash`/`x`.
- **Consistency with existing success/error feedback**: results still go through `Toast`, unchanged.

## Migration inventory

Risk key: L = low (self-contained), M = medium (async conversion, shared helper), H = high (Workbench hot path / guard).

### Group A — validation alerts (do first)

| File:line                                         | Current                       | Target                                   |
| ------------------------------------------------- | ----------------------------- | ---------------------------------------- |
| `components/datasets/ImportDatasetWizard.tsx:638` | `alert("请选择 .zip 文件")`   | `alertDialog(...)` or inline field error |
| `components/datasets/ImportDatasetWizard.tsx:642` | `alert("ZIP 包不能超过 …MB")` | `alertDialog(...)`                       |

Prefer inline field error for file-picker validation; use `alertDialog` only if a modal notice is wanted. Either way remove `alert`. Risk: L.

### Group B — management / destructive confirms

| File:line                                                | Action                          | Notes                                     |
| -------------------------------------------------------- | ------------------------------- | ----------------------------------------- |
| `components/users/ApiKeysPanel.tsx:164,178`              | rotate / revoke API key         | destructive; add consequence copy         |
| `components/connections/StorageConnectionsPanel.tsx:498` | delete connector                | destructive                               |
| `components/bugreport/BugReportDrawer.tsx:454`           | delete bug report               | destructive                               |
| `pages/Datasets/DatasetsPage.tsx:225`                    | delete dataset(s)               | destructive; add count via `details`      |
| `pages/Projects/sections/ClassesSection.tsx:187,208`     | class-unit rename/delete guards | M — shared via two helpers                |
| `pages/ProjectTemplates/ProjectTemplatesPage.tsx:121`    | template action guard           | L                                         |
| `pages/Dashboard/MyBatchesCard.tsx:223,392`              | batch submit confirm(s)         | reuse the same tone/voice as AnnotatePage |
| `pages/Annotate/AnnotatePage.tsx:369`                    | batch submit confirm            | pair with MyBatchesCard                   |

Risk: L–M. All are outside the canvas hot path.

### Group C — reason / multi-choice dialogs to subsume

| File:line                                                                                       | Current                                      | Target                                                              |
| ----------------------------------------------------------------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------- |
| `pages/Projects/sections/RejectBatchModal.tsx`                                                  | bespoke modal                                | `inputDialog` (keep the same copy)                                  |
| `pages/Review/RejectReasonModal.tsx`                                                            | bespoke modal                                | `choiceDialog` + optional `inputDialog`                             |
| `pages/Projects/sections/ReverseTransitionModal.tsx` / reset / lock modals                      | bespoke modals with reason                   | evaluate `inputDialog`; keep bespoke if they need multi-field forms |
| `pages/Workbench/state/useMaskEditorSession.ts:47` (`promptMaskLeaveChoice`)                    | two sequential confirms for a 3-state choice | `choiceDialog({ options: [save, discard, continue] })`              |
| `pages/Workbench/stages/image/useImageAnnotationActions.ts:224` (`promptEmptyRasterMaskChoice`) | two sequential confirms                      | `choiceDialog`                                                      |

Risk: M. The two `prompt*Choice` helpers currently take a synchronous `confirmFn`; converting them to `async` is the first real async boundary and must update their callers (`useWorkbenchShellModel.tsx:3139,3197`, `useImageAnnotationActions.ts:1250`).

### Group D — Workbench hot-path / leave guards (highest risk, do last)

| File:line                                                             | Action                                         |
| --------------------------------------------------------------------- | ---------------------------------------------- |
| `pages/Workbench/state/useWorkbenchShellModel.tsx:2644`               | project config side-effect confirm             |
| `pages/Workbench/state/useWorkbenchShellModel.tsx:3139,3197`          | Mask leave guard (via `promptMaskLeaveChoice`) |
| `pages/Workbench/state/useWorkbenchShellModel.tsx:4522`               | overwrite unsaved Mask draft from clipboard    |
| `pages/Workbench/state/useWorkbenchShellModel.tsx:6953`               | batch delete video tracks                      |
| `pages/Workbench/shell/OfflineQueueDrawer.tsx:175`                    | discard all offline operations                 |
| `pages/Workbench/shell/MaskQcPanel.tsx:340`                           | apply QC action to issue region                |
| `pages/Workbench/stage/VideoTrackSidebar.tsx:458,467,775`             | delete tracks / Mask track                     |
| `pages/Workbench/stage/VideoTrackerReviewBar.tsx:107`                 | overwrite protected keyframes                  |
| `pages/Workbench/stages/three-d/ThreeDWorkbench.tsx:2159`             | remove 2D member from camera                   |
| `pages/Workbench/stages/video/useVideoAnnotationActions.ts:914`       | expand interpolation to all frames             |
| `pages/Workbench/stages/image/useImageAnnotationActions.ts:1321,1329` | Mask deletion confirms                         |

`pages/Workbench/stages/three-d/FramePicker.tsx:223` is a **false positive** (local `confirm()` submit function) and must not be migrated.

Risk: H. These run inside canvas/video interactions with generation keys and undo history; every conversion must preserve ordering and re-validate state after the await.

### Hard cases that cannot use the async service as-is

- `pages/Settings/useUnsavedSettingsGuard.ts:19` overrides React Router `navigator.push/replace`, which must stay synchronous. The app mounts `<BrowserRouter>` (`main.tsx`), not a data router, so `useBlocker` is unavailable. Convert with a deferred-navigation pattern: the override stays synchronous, `confirmDialog` decides asynchronously, and confirm replays the captured transition through the original navigator method; keep `beforeunload` native.
- `pages/Settings/SettingsPage.tsx:104` duplicates the same copy; fold into the same guard.
- All `beforeunload` handlers stay native.

## Phases

Each phase is independently mergeable and reviewable.

- **Phase 0 — capability.** Add `decisionDialog` service + `<DecisionDialogHost />` + mount in `App.tsx` at both `ToastRack` sites. Unit tests for promise resolution, focus defaults, Esc/overlay cancel, danger tone, input validation, pending lock. No business call site changes.
- **Phase 1 — alerts + management confirms.** Group A (remove both `alert`) and Group B. Add a small e2e or component test for one destructive flow.
- **Phase 2 — reason / choice consolidation.** Group C, including making `prompt*Choice` async and deleting `RejectBatchModal`/`RejectReasonModal` where fully subsumed.
- **Phase 3 — Workbench guards.** Group D plus the two hard cases. Regression-focus: Mask leave, video track deletion, offline queue, three-d member removal.
- **Phase 4 — guardrail.** Add an eslint restriction in `apps/web/eslint.config.js` banning `window.confirm`, `window.alert`, and `window.prompt` (allow `beforeunload`), and update the 8 e2e specs that sniff `dialog.message()` to interact with the in-app dialog.

## Risks and mitigations

- **Async await gap.** Converting `if (confirm()) act()` to `if (await confirmDialog()) act()` introduces a suspension point. ReviewPage already guards this class with `queueScopeKeyRef`; every converted site must re-check its owner/scope/task generation after the await before acting. Add this to the review checklist for Phases 2–3.
- **Dual mount.** The full-screen Workbench does not render `AppShell`. The host must be mounted at both `App.tsx` sites (mirror `ToastRack`), and a Workbench test must assert the dialog renders there.
- **`beforeunload` is synchronous by spec.** Never route it through the async service.
- **Navigation guard.** `navigator.push/replace` overrides cannot await; use a router blocker, not the generic service.
- **Scope creep into content forms.** Keep `Modal` for multi-field forms; do not force them into `inputDialog`.
- **Test churn.** 8 specs use native-dialog sniffing; update them per phase to avoid mixed states.
- **No behavior change in copy semantics.** Preserve existing wording unless the copy is being unified deliberately; record copy changes in the changelog.

## Testing and verification

- Unit/component (`vitest`): service promise semantics; destructive default focus; input `required`/`maxLength`/`validate`; pending lock; danger tokens.
- Component: one representative migration per group asserts the dialog text and that the action runs only after confirm.
- e2e (`playwright`): update the native-dialog specs to click the in-app dialog; add one Mask-leave and one track-deletion path.
- Guardrail test: a lint check that fails on a newly introduced `window.confirm/alert/prompt`.
- Screenshots: capture the new danger confirm for the user guide (native dialogs cannot be captured).
- Commands: `pnpm --filter @anno/web typecheck`, `lint`, `lint:css-tokens`, `test`, and the affected `test:e2e` specs; `prettier --check` for changed docs.

## Documentation impact

- `docs-site/user-guide/`: instructions that mention confirming a destructive action should reference the in-app dialog where wording changes.
- `docs-site/dev/reference/design-system.md`: add the decision-dialog class and the "one capability, one visual language" rule.
- `CHANGELOG.md` Unreleased: user-visible copy/behaviour changes (per phase); the `alert`→notice change is user-visible, the internal service is not.
- No API, schema, environment variable, database, or dependency changes.

## Decisions

Resolved 2026-09-17 when the plan was approved:

1. **`confirmDialog` stays boolean-only** (`Promise<boolean>`). No async confirm handler, no built-in pending state; callers own spinners and disable states as they do today. One-line call sites and a pure, testable service win over built-in pending UI.
2. **Reason-modal subsumption**: `RejectBatchModal` (single required textarea, ≤500 chars) is fully subsumed by `inputDialog`, copy preserved. `RejectReasonModal` maps to `choiceDialog` + optional `inputDialog`. `ReverseTransitionModal` and the reset/lock modals: migrate to `inputDialog` only where the form is a single reason field; keep on `Modal` if genuinely multi-field — evaluator judgment, recorded in the task report.
3. **eslint guardrail lands as an `error`** in Phase 4, scoped so test fixtures/e2e are exempt; it ships only after grep proves zero `window.confirm`/`window.alert`/`window.prompt` call sites remain in `apps/web/src`.
4. **Ship as one orchestrated change on `feat/platform_opt260917`** with one commit per task (phases stay independently reviewable); no version bump.

## Execution

Orchestrated waves (isolated worktrees per modifying worker; integration and validation on the originating branch between waves):

| Task | Scope                                                                                  | Wave            | Model         |
| ---- | -------------------------------------------------------------------------------------- | --------------- | ------------- |
| T0   | Phase 0 — capability (service, host, dual mount, unit tests)                           | 1               | glm-5.3 (max) |
| T1   | Phase 1 — Group A alerts + Group B management confirms                                 | 2 (after T0)    | glm-5.3-flash |
| T2   | Phase 2 — Group C Projects/Review modal consolidation                                  | 2 (after T0)    | glm-5.3 (max) |
| T3   | Phases 2–3 — Workbench `prompt*Choice` + Group D guards + Settings deferred navigation | 2 (after T0)    | glm-5.3 (max) |
| T4   | Phase 4 — eslint guardrail, e2e dialog-spec updates, design-system docs                | 3 (after T1–T3) | glm-5.3-flash |

T3 additionally updates the eight e2e specs that sniff `page.on("dialog")` for the flows it migrates; T4 covers any remainder.

## Outcome

Implemented 2026-09-17 via orchestrated waves T0–T4 plus follow-up T3.5 (Run `run_c32ffbdd3101`), integrated as six commits on `feat/platform_opt260917` (`26ec1af7` T0 → `d0603a1d` T2 → `e580f06e` T1 → `3a623542` T3 → `4881ac07` T4 → `f27b9c76` T3.5). All phases landed: the decision-dialog capability, all native `confirm`/`alert` migrations (zero call sites remain, enforced by an eslint error rule), the bespoke reason-modal consolidation (`RejectBatchModal`/`ReverseTransitionModal`/`ResetBatchModal`/`AdminLockModal`/`RejectReasonModal` deleted), the Settings deferred-navigation guard, and the 8 e2e dialog-sniffing specs rewritten against `role="alertdialog"`. Verified per integration and finally: `typecheck`, `lint`, `lint:css-tokens`, full vitest suite, `git diff --check`. Known follow-up: the review-guide `reject-form.png` screenshot still shows the old single-window reject form (doc-media refresh, aap-doc-media), and the full e2e suite runs centrally (CI/dev stack) rather than in worker worktrees.
