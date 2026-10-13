# Workbench shortcuts interface and customization

> Status: completed and archived. Both increments shipped, and the later workbench media recapture replaced and reviewed the shortcut demonstration.
> Source baseline: `fb7ce93e`, reviewed on 2026-09-14.
> The user requested alignment with Settings / Annotation Guide, a Settings-like presentation, clearer content, and an assessment of custom shortcuts. The user subsequently clarified that “domain” was undecided and that avoiding shortcut conflicts was the primary concern.
> Follow-up: the user proposed digits 1–9 plus 0 for category access and questioned whether letter bindings should be limited. The revised recommendation below reserves ten direct category slots, removes letter-based category selection, and keeps the category count unlimited. This replaces the earlier recommendation to preserve historical category-letter reservations.

## Recommendation

Use the same dialog geometry as Workbench Settings and Annotation Guide, with a Settings-like navigation column and one scrolling content area. Separate presentation categories from execution contexts. Let users edit named actions; the application determines where those actions can run and explains conflicts in ordinary language.

Deliver two independently useful increments: an accurate reference interface, then account-level customization for a bounded set of commands already routed through the central dispatcher. Keep complex tool-local interactions visible and reserved until their owners support the same binding contract. A larger dialog alone is the minimal option, but it leaves nested scrolling, outdated descriptions, and ambiguous duplicate keys unresolved.

The implementation is larger than eight files. The reference interface is a moderate frontend change; customization is the larger portion because matching, context ownership, persistence, hints, and regression coverage must agree. No additional service, library, environment variable, third-party account, or credential is required.

## Evidence from the current implementation

| Finding                                                                                                          | Evidence                                                                                                                                        | Design consequence                                                                                  |
| ---------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| The shortcut dialog uses width 860 and content-dependent height.                                                 | `apps/web/src/pages/Workbench/shell/HotkeyCheatSheet.tsx`, `components/ui/Modal.tsx`                                                            | Adopt the established Workbench dialog geometry.                                                    |
| Settings and Guide already share desktop and narrow-screen geometry.                                             | `shell/WorkbenchSettingsDialog.tsx:167`, `sidebar/GuidePanel.tsx:142`                                                                           | Reuse these values without changing the global Modal adapter.                                       |
| The shortcut list has both a scrolling outer body and independently scrolling groups.                            | `HotkeyCheatSheet.tsx:17`, browser inspection                                                                                                   | Use one content scrollbar with stable navigation and search.                                        |
| Long descriptions combine command names, activation conditions, implementation vocabulary, and pointer gestures. | `state/hotkeys.ts`, `HotkeyCheatSheet.tsx`                                                                                                      | Separate action, binding, applicability, and secondary help.                                        |
| The reference table is not the executable source of truth.                                                       | `HOTKEYS` contains display strings; `dispatchKey` matches events separately.                                                                    | Introduce stable command identities and structured bindings before enabling edits.                  |
| Some reference content is outdated.                                                                              | Image Tab still says “next user box”; execution uses `imageCycleInCategory`. Backquote cross-category navigation is missing from the reference. | Audit descriptions against handlers and preserve current behavior.                                  |
| Frequency is counted by action type rather than individual command.                                              | `state/hotkeyUsage.ts`; several tools share `setTool`; local storage is not user-scoped.                                                        | Remove the current count and frequency-sort presentation. Use a stable curated common-actions view. |
| Settings/Guide have dedicated interaction guards; the shortcut dialog does not.                                  | `state/workbenchInteractionGuards.ts`, Shell `disabled: workbenchSettingsOpen`                                                                  | Add an equivalent shortcut-dialog boundary before introducing key recording.                        |
| Preferences already support account isolation and deep-merge writes.                                             | `schemas/user.py`, `api/v1/me.py`, `src/api/auth.ts`, `state/useUserPreferences.ts`                                                             | Extend existing preferences, with one shortcut writer and explicit reset semantics.                 |
| Project attribute bindings already exist, with limited support.                                                  | Boolean/select attributes use unique digits 1–9; image selection resolves attributes before category digits; video returns earlier.             | Show the real scope and source; do not imply video attribute shortcuts already work.                |

Browser inspection used an isolated agent-browser session against the existing primary-checkout server. The shortcut, settings, and guide component files were compared with this checkout and were identical. At a 1280 × 633 viewport, the shortcut dialog measured 860 × 585 and Settings measured 1120 × approximately 538. The shortcut body had four additional overflowing groups. The existing project had no visible Guide entry; Guide geometry was verified from source, not from a live Guide dialog.

No browser runtime errors or failed API responses were observed in this inspection; the console contained an existing Konva layer-count warning. The task was left through normal navigation, its acquired lock was released with HTTP 204, the browser session was closed, and both temporary screenshots were deleted. This was inspection of existing behavior, not acceptance testing of the proposed change.

## Interface specification

### Geometry and interaction

- At widths of at least 768px, use width `min(1120px, calc(100vw - 64px))`, height `min(820px, 85dvh)`, maximum height `calc(100dvh - 64px)`, and the same radius, overlay, and layer as Settings/Guide.
- Below 768px, use the established full-viewport treatment with safe-area padding; move category navigation above the content.
- Keep the desktop navigation column 220px wide, matching Settings. Only the right content body scrolls. Search, current category heading, and save state remain visible.
- Reuse local Radix/shadcn Dialog and Tabs, semantic theme tokens, compact typography, and existing icons. Do not redesign Settings or Guide or introduce a generic dialog framework.
- Open from the existing question-mark entry and `?`. Give the entry an accessible name such as “快捷键”. On open, focus search; keep focus in the dialog; on close, restore the entry focus.
- Add `data-workbench-hotkeys` to the dialog and overlay and include it in the shared event guards. Opening/closing events must not continue into background listeners after the portal disappears.
- Apply the same background playback/input policy as Settings. Clear held-key state when opening, blurring, switching tasks, or unmounting.
- Escape closes the reference interface. During binding recording it first cancels recording, leaving the dialog open. Outside click cancels an unconfirmed recording before closing; already confirmed writes remain owned by the preference writer.

### Information architecture

Use purpose-based navigation: “常用”, “绘制与工具”, “选择与编辑”, “画布与视角”, “AI 与审核”, “播放与轨迹”, “任务与系统”, and “鼠标操作”. Keep project category/attribute shortcuts as a clearly labeled section under Selection and Editing.

Use a separate explicit filter for “当前工作台” and “全部类型”. Default to the current workbench type plus applicable common commands; all categories remain discoverable. Selecting All allows cross-type browsing without changing any active bindings. Never use the currently selected UI filter as an execution scope.

Search action names, aliases, actual bindings, applicability text, and project attribute labels. A search spans categories within the selected type filter and shows category/type context in results. An empty result displays a message and actions to clear the query or include all types. Remember the selected category during the mounted Workbench session; clear the query on reopen. Do not write navigation/search state into account preferences.

Each row contains a concise action name, actual binding, applicability text, and optional secondary explanation. In the customization increment, add Default / Customized / Fixed state and a row action. Examples of content shape:

| Action          | Binding | Applies when            |
| --------------- | ------- | ----------------------- |
| 矩形框工具      | B       | 图片；Mask 未接管该按键 |
| 切换笔刷        | B       | Mask 编辑中             |
| 锁定 / 解锁轨迹 | L       | 视频；已选中轨迹        |
| 正向播放        | L       | 视频；未选中轨迹        |

These examples describe existing routes. The implementation must derive conditions from the owners, including any drawing or popup exclusions, rather than generalizing these short labels into incomplete predicates.

Show combinations with explicit `+` separators, alternatives with “或”, and alternate bindings under the same command. Split opposite directions and different tools into separate commands. Render Ctrl on Windows/Linux and the corresponding Command label on macOS. Translate implementation-facing terms such as `user 框`, `wheel`, and `schema` into product language. Put dragging, modifier-plus-click, and wheel gestures in Mouse Operations.

The Common view is a small curated set of relevant tool selection, undo/redo, navigation, and AI decision actions. It must not claim to represent personal usage. Remove the current frequency checkbox and action-type counts; no new telemetry is needed.

## Conflict model

The application, not the user, owns contexts. Model a finite set of existing facts: stage type, annotate/review mode, focus owner, open interaction layer, active tool, draft state, selection kind, and ordinary-prediction versus interactive-candidate state. Reuse the current state owners; do not create a second Workbench state tree or a user-authored condition language.

Two commands conflict if the same normalized binding can match both in any supported overlapping context. Check the whole applicable domain, not merely the current task or current selection. Temporary unavailability, missing permissions, or a disabled AI capability must not make an otherwise overlapping binding appear permanently safe.

| Case                                                               | Decision                                                                                                           |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| Image and video commands share a key.                              | Allow if their stage types are disjoint.                                                                           |
| A common navigation command overlaps video frame navigation.       | Reject; both can run in video.                                                                                     |
| Image L locks a selection and otherwise selects the polyline tool. | Allow only with explicit complementary selection predicates.                                                       |
| Mask B and image rectangle B share a key.                          | Express the existing Mask ownership as an exclusion on the outer command; a priority number alone is insufficient. |
| A new binding overlaps a fixed tool-local command.                 | Reject and name the fixed command and context.                                                                     |
| A new binding overlaps category/attribute bindings.                | Apply the explicit project-key policy; these keys must be present in the conflict inventory.                       |
| Context overlap cannot be established as disjoint.                 | Reject the edit conservatively and retain the existing binding.                                                    |

Keep event ownership for native controls, dialogs, and active tool sessions. Do not offer automatic “replace the other action” or silently resolve collisions by letting one command win. User-facing conflicts should say, for example, “与‘下一题’冲突：在视频工作台中两者会同时生效”. Offer cancel/re-record and navigation to the conflicting action.

Input, textarea, select, contenteditable, comboboxes, menus, dock tabs, comment editors, and focused timeline controls retain their own keyboard behavior. A keyboard event executes at most one Workbench command, including across capture and bubble listeners. Ignore composing/IME and AltGr input. Treat keyup, key repeat, blur, and task changes as part of the existing owner contract, not just keydown matching.

For editable bindings, preserve the current character-oriented key semantics using normalized `KeyboardEvent.key` and explicit modifiers, with `Mod` representing the platform primary modifier. Match modifiers exactly. Preserve existing physical-key exceptions such as Backquote as fixed bindings in this increment. Caps Lock does not create a new binding; Shift does. Reject modifier-only input and known browser/OS combinations such as tab/window close, browser location, reload, and developer tools. Do not promise interception of shortcuts the browser does not deliver.

Preserve documented default combinations and explicit alternate keys except for the deliberate category-key and zero-key changes specified below. Some current letter branches also accept undocumented Shift/Alt variants because they ignore modifiers; stop that accidental fall-through for commands moved to the resolver, and cover it as part of the conflict fix. Do not describe this as preserving every historical key event.

### Project category and attribute keys

Apply this revised category contract in Increment B, atomically with the affected handlers, palette labels, and documentation. Increment A must continue to describe the behavior actually running until that change ships.

The limit applies to direct shortcut slots, not to the number of categories. Use unmodified 1–9 for the first nine categories and unmodified 0 for the tenth, based on the current tool-binding unit's configured order. Do not renumber these slots when searching, selecting a different category, or updating recent-use history. Fewer than ten categories leave the unused slots reserved and inactive. Switching tool units uses that unit's category list, as today.

Remove category A–Z mappings from both the canvas fallback and category-picker direct selection; letters become tool/action keys on the canvas and search text inside the picker. This also removes the historical I/Q/R/T/X/Y/Z reservation. A moved tool binding must never turn into category selection through fallback. The change does not delete or reorder categories, and does not limit how many categories a project can define.

Categories beyond the first ten remain available through the existing searchable picker, mouse selection, and recent-category entries. Improve picker keyboard navigation so Up/Down moves through filtered results, Enter selects the highlighted result, and Escape cancels. Search is initially focused. Digits typed in search remain text; direct digit selection is available only when focus is in the non-editable results area. With an empty query, Enter retains the existing default-category behavior; with a nonempty query and no result, it performs no selection. Composing input never confirms a class. Preserve the distinction between selecting a class for the next annotation and relabeling an existing annotation in each caller.

Do not introduce a new global picker shortcut in this increment: retain existing change-class entry points and make the category panel/picker keyboard reachable. The existing selected-annotation C route keeps its current meaning. A general picker-launch command can be considered separately rather than adding another competing single-letter shortcut here.

The current zero key is not free everywhere:

- `stage/VideoKonvaStage.tsx:1948` uses bare 0 for actual-size view. Move this to Shift+0, retain the action, and label it accurately.
- `stages/three-d/TriOrthoView.tsx:423` uses bare 0 to reset the focused orthographic view's zoom. Move it to Shift+0 only in that focused view. Its event must not also reach category selection or the global view-reset handler.
- Existing Mod+0 remains the documented view-reset action. Match modifiers exactly so it never selects category ten.
- Represent Shift+0 with the physical top-row `Digit0` plus Shift, as an explicit fixed-binding exception, since its produced character varies by keyboard layout. Plain main-row and NumLock-on keypad digits select categories; NumLock-off navigation keys retain their normal meaning.

Also resolve the existing image attribute collision: selecting an annotation alone must no longer make a canvas digit edit an attribute. Retain project-configured attribute keys and their schema, but activate them only while the user explicitly focuses a labeled, non-editable attribute-shortcut region in the attribute editor. Provide a focusable region with visible focus and a short “属性快捷键已启用” hint; clicking/focusing its heading activates that region. Only events originating inside this region can invoke attribute shortcuts. Native input/select/contenteditable fields keep their own behavior, and leaving the region immediately restores canvas category ownership. Continue limiting this attribute behavior to the already supported image flow; do not imply new video attribute support.

Use one shared digit-to-slot helper for execution, `ClassPalette`, `ClassPickerPopover`, `TaskQueuePanel`, and `ContinuousCreationControls`. Display badges only for the ten actual digit slots. Treat picker and attribute regions as explicit input owners, so no category key or custom command also fires in the background.

Implement the attribute focus contract in `shell/AttributeForm.tsx` with explicit opt-in from the existing image selection consumers (`ImageSelectionCardContent.tsx` and the relevant `AIInspectorPanel.tsx` branch). Reuse their attribute update callbacks and update the current “selected annotation” shortcut tooltip. Do not activate this behavior implicitly in creation forms, video forms, or other consumers of the shared component.

Defaulting to the first ten categories costs no setup. User-assigned favorite categories for these ten slots is a possible later extension, not part of this increment. If added later, assignments must persist by project/tool/category identity and must never reorder automatically with usage. Do not add two-stroke numeric sequences or modifier banks for categories in the initial design.

## Customization scope and persistence

### Initial editable commands

Open customization for the central image/video discrete commands below. Each direction, target tool, and state flag has a stable ID; an action type such as `setTool` is not an ID.

- Image tool selection currently exposed by `dispatchKey`: select, rectangle, rotated box, polygon, polyline, keypoint, Mask, AI tool cycle, and Magic Box.
- Video tool selection currently exposed by `dispatchKey`: select, rectangle, rotated box, keypoint, track, Mask, smart point, smart box, exemplar, Magic Box, and polygon. Preserve the atomic tool-and-frame/track-scope contract of `requestVideoTool`.
- Common previous/next task navigation, using the same binding in each supported stage and checking against fixed point-cloud commands.
- Image selection visibility/locking; video selected-track visibility/locking/outside/occluded toggles and frame bookmarks.
- Video previous/next frame, sampled-grid navigation, source-frame microsteps, and previous/next selected-track keyframe. Sampling changes the command's existing behavior, not its identity. Focused timeline navigation remains local and is labeled separately.

Everything outside this explicit list remains Fixed in this increment. This includes point-cloud-local commands, standard clipboard/history keys, submit/delete and AI decisions, Enter/Escape/Tab, category/attribute selection, SAM/Mask-local controls, Space press/drag, J/K/L playback gestures, pointer gestures, and physical-key exceptions. Fixed commands still participate in conflict checks. Extending the editable list requires connecting the corresponding handler and hint to the same resolver and adding its interaction regression.

Do not add project-wide administrator keymaps, presets for other applications, macros, multi-stroke sequences, imports/exports, or user-created domains. Personal bindings follow the account across projects; project attribute definitions retain their existing ownership.

### Recording and saving

- Clicking a binding enters an inline recorder. The UI displays the candidate combination and any conflict before confirmation. Escape cancels; the recorded combination cannot trigger any background action.
- Each command supports one primary and at most one alternate binding; editing replaces its complete binding list. Preserve existing alternatives when opening the editor.
- Provide “停用” for editable commands, “恢复默认” per command, “仅看已修改”, and a reset action whose label names the affected type. A type reset excludes common bindings; common resets have their own explicit action.
- Confirmation initiates saving immediately. Keep the last acknowledged binding active until the write succeeds. On failure retain the pending edit and show retry/cancel; never label the draft as saved. The single writer survives dialog closure during the mounted Workbench session.

### Preference contract

Extend `workbench.shortcuts` in the existing preference schema with `schemaVersion: 1` and override buckets `common`, `image`, and `video`. Each bucket maps a stable command ID to its complete structured binding list or `null`. Missing or `null` means default; an empty list means disabled. The absent subtree uses existing defaults and requires no data migration. Point-cloud fixed commands do not need a writable bucket in this increment.

PATCH only changed command entries. A reset submits `null` for the affected entries; submitting an empty object does not remove entries under the current deep merge. A nonempty list atomically replaces all bindings of that command. Preserve unrelated preferences and overrides when writes complete or caches refresh.

Use `useUserPreferences` for reads and one new shortcut preference owner for writes, keyed by user ID. Serialize/coalesce repeated edits to the same command so an older completion cannot replace a newer value. Refetch after success rather than replacing the entire preference cache with a stale snapshot. Loading failure must not authorize saving defaults; disable editing and offer retry. An account change retires the old writer and prevents late responses from reaching the new account.

Validate the envelope, modifiers, key format, command IDs, domains, and per-command limits in the API. Recheck effective conflicts in the frontend on load and project/context change; malformed or incompatible stored overrides must not enable competing commands. Retain affected stored values for explicit correction instead of overwriting them with defaults. If multiple commands unexpectedly match at runtime, execute neither, report the conflict once, and retain pointer access to the actions.

Separate strict writes from tolerant reads. Extend the preference response path with an opaque fallback for malformed or newer shortcut data, following the existing workspace-envelope pattern; the present whole-object `UserPreferences.model_validate` must not make an invalid shortcut subtree fail the entire preference GET. Preserve original incompatible values and unknown command IDs for recovery while the frontend excludes them from execution and identifies the affected overrides. An unrelated preference PATCH must preserve this stored subtree without revalidating it as a new shortcut write. A targeted shortcut PATCH validates changed entries strictly, preserves untouched opaque entries, and still allows explicitly resetting an affected entry to `null`.

Deployment is additive: land the compatible API schema before enabling client writes. UI rollback can stop using overrides without touching annotations. Keep additive schema support while stored overrides exist; rolling the API back to a schema that forbids the new subtree is not a safe rollback.

## Implementation ownership

```text
Existing Workbench state owners --> context facts
Command definitions + fixed-key inventory + account overrides
                              |
                     effective binding resolver
                     /          |            \
          shortcut dialog   tool/menu hints   event ownership
                                                |
                                      existing action handlers
Dialog confirmed edits --> shortcut preference writer --> existing preferences API
```

| Area                              | File targets and responsibility                                                                                                                                                                                                                              |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Interface                         | Rework `shell/HotkeyCheatSheet.tsx`; retain its public component entry. Add focused component coverage. Pass the current stage/context through `WorkbenchLayout.tsx` / `useWorkbenchShellModel.tsx`.                                                         |
| Definitions and matching          | Evolve `state/hotkeys.ts`; add `state/hotkeyBindings.ts` for normalization, effective bindings, and overlap checks. Keep business action execution in existing handlers.                                                                                     |
| Persistence                       | Add `state/useWorkbenchShortcutPreferences.ts`; consume `useUserPreferences.ts`; extend `src/api/auth.ts`, `apps/api/app/schemas/user.py`, and relevant preference route validation.                                                                         |
| Event boundaries                  | Extend `state/workbenchInteractionGuards.ts`, `useWorkbenchHotkeys.ts`, and the Shell. Audit fixed owners so their actual matching agrees with their reserved inventory.                                                                                     |
| Fixed owners to verify            | `stages/image/useImageAnnotationActions.ts`, `stage/VideoKonvaStage.tsx`, `stage/VideoPlaybackOverlay.tsx`, `stages/three-d/ThreeDWorkbench.tsx`, `stages/three-d/usePointCloudScene.ts`, `stages/three-d/SceneTimeline.tsx`, and `modes/useReviewMode.tsx`. |
| Visible hints                     | `shell/ToolDock.tsx`, `stage/tools/*.ts`, `shell/Topbar.tsx`, and affected selection/timeline menus. Query effective bindings for editable commands; remove stale hardcoded hints for them.                                                                  |
| Category hints and reservations   | `shell/ClassPalette.tsx`, `ClassPickerPopover.tsx`, `TaskQueuePanel.tsx`, and `ContinuousCreationControls.tsx`; preserve mapping order and distinguish popup/canvas ownership.                                                                               |
| Generated/reference documentation | Update `docs-site/scripts/generate-hotkeys.mjs` and `docs-site/user-guide/workbench/hotkeys.generated.md`; preserve static parser validation rather than executing application modules.                                                                      |

Do not emulate a remapped command by synthesizing its old key event. Do not leave old hardcoded matches active alongside the new binding. Fixed listeners may keep their business handlers, but they must honor event ownership and their declared modifier rules. Tighten accidental modifier fall-through where necessary: current loose branches such as 3D V must not also process a common modified binding. Include `TriOrthoView.tsx` and the image attribute-editor owner in the revised category-key change.

## Delivery and acceptance

### Increment A: reference interface

Implement aligned geometry, purpose-based navigation, explicit type filtering, search, readable rows, fixed gesture presentation, corrected reference content, and dialog isolation. Keep existing default shortcut execution. Modifier normalization, ten-slot category access, retirement of category letters, zero-key relocation, and attribute focus ownership belong to Increment B. This increment can ship without any preference API extension or editable binding UI.

Update `docs-site/user-guide/workbench/index.md`, the generated shortcut reference, relevant Settings cross-links, and `CHANGELOG.md` Unreleased. Remove claims that the current display table necessarily equals execution. Update the affected shortcut media flow and refresh its documentation video/poster through the project media workflow so the shown interface matches the change.

### Increment B: scoped customization

Implement stable command IDs, the resolver and complete fixed-key inventory, effective hints, the explicit editable list, preference contract, recorder, conflicts, disabled bindings, reset/retry states, and the revised category contract. Ship category digits, removal of letter selection, picker navigation, zero-key relocation, and attribute focus ownership together. A metadata-only editor that cannot change execution is not a deliverable.

Extend user guidance with default/custom/fixed meanings and concrete conflict examples. Update `docs-site/dev/concepts/workbench-shell.md`, affected preference API documentation and README contract references, the generated API artifacts, and Unreleased. No release version, commit, push, or publish step is assigned by this plan.

### Verification

| Path                   | Acceptance                                                                                                                                                                                             |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Geometry               | At 1280×720, 1440×900, and 1920×1080, shortcut/Settings/Guide geometry agrees for the same viewport. At 375×812 and 768px width, controls stay reachable and the page has no horizontal overflow.      |
| Content                | Image Tab and Backquote match execution; all keyboard references and actual fixed gestures have accurate type and state labels. Search includes attributes and has a useful empty state.               |
| Reference interaction  | One content scroll area, stable search/navigation, dark/light themes, focus restore, outside click, Escape, and browser zoom.                                                                          |
| Default behavior       | Preserve video tool/frame scope, Mask local undo, SAM candidates, review actions, and native timeline navigation; explicitly verify the documented category, zero-key, and modifier changes.           |
| Customization          | New keys execute once; old keys no longer invoke the edited command; alternatives and disabled commands work; displayed hints agree with effective bindings.                                           |
| Conflicts              | Reject overlapping common/type/tool commands and fixed reservations; allow proven disjoint states/types; modifiers and project category/attribute keys are included.                                   |
| Category compatibility | Test 0/1/9/10/11/35+ categories, project/tool changes, stable slots under filtering, retired letter selection, picker search/arrows/Enter/IME, NumLock, and no action for missing slots.               |
| Digit ownership        | Plain 0 selects slot ten; Shift+0 performs the appropriate view action; Mod+0 resets the view. Attribute keys work only in their explicitly focused region and never edit from canvas selection alone. |
| Isolation              | Recording, comments, Chinese IME, menus, Settings, Guide, dock tabs, and timeline focus never trigger unintended background changes. Opening while Space is held does not leave pan/playback stuck.    |
| Persistence            | Reload, type/project/account switching, two quick writes, failed load/save, retry, per-command reset, and type reset preserve unrelated preferences. Unknown stored data does not trigger overwrite.   |

Run targeted Vitest coverage for `hotkeys`, `useWorkbenchHotkeys`, `workbenchInteractionGuards`, the new binding/preference helpers, the dialog, and affected hint consumers, then web typecheck and lint including `pnpm --filter @anno/web lint:css-tokens`. Run `pnpm docs:hotkeys`, inspect the generated diff, and run the affected documentation checks.

For the preference API increment, use the checkout-local Python virtual environment and a verified disposable database via the managed test-mode launcher; run `apps/api/tests/test_user_preferences.py` and `apps/api/tests/test_me_preferences.py` plus new binding-schema cases. Regenerate OpenAPI/types using the repository contract workflow. Run existing `workbench-settings.spec.ts` and focused shortcut E2E coverage against an isolated test runtime, and use agent-browser for actual layout/focus inspection. Do not seed the everyday development database.

After each verification, remove only artifacts and temporary data created by that verification. Preserve requested documentation assets. Review the final diff and run `git diff --check`; distinguish local checks, browser results, and remote CI in the implementation report.

## Assumptions and tradeoffs

The most fragile assumption is that each fixed listener's real activation condition can be represented accurately. If this is false, the conflict checker could approve a key that a legacy listener still consumes. The design handles this by keeping such commands fixed, conservatively reserving ambiguous overlaps, and requiring an ownership regression before opening another command for customization.

A full global keymap that bans every duplicate would be simpler to explain but unnecessarily prevents image/video and mutually exclusive tool-state reuse. A priority-only domain system would accept hidden collisions. The chosen overlap model permits verified reuse while making conflicting edits reviewable.

Reuse the existing [Radix Dialog](https://www.radix-ui.com/primitives/docs/components/dialog) for dialog behavior. Providing remapping/disable controls for editable character shortcuts also follows the mechanism described by [W3C's character-key-shortcut guidance](https://www.w3.org/WAI/WCAG21/Understanding/character-key-shortcuts.html); this plan does not claim a complete accessibility audit.

## Outcome

- Implementation and review fixes are included with this plan on `feat/workbench_opt260914`; release assignment remains with maintainers.
- The shortcut dialog follows the Settings/Guide geometry and supports category/type filters, effective-binding and attribute search, keyboard recording, disabling, and conflict-checked resets. Category selection uses ten stable digit slots while all categories remain searchable.
- Dispatch resolves editable commands independently of their old physical keys and explicitly scopes image, video, and 3D contexts. Fixed-key conflicts include review actions and local canvas/Mask/SAM gestures, with contextual exceptions for established defaults.
- Account preference writes share a per-command in-flight barrier across Workbench mounts. Editing previews include pending changes; runtime bindings use confirmed data. Targeted cache updates preserve independent settings and late responses cannot remove a newer owner's queue. API writes validate supplied fields while retaining untouched opaque data.
- User/API/developer documentation and the Unreleased changelog describe the current behavior. OpenAPI, generated TypeScript clients, and shortcut documentation were regenerated; documentation codegen checks and the docs build passed.
- Verification: the full web suite passed (518 files / 5154 tests), including dispatch, persistence, recorder, class-picker, tool-hint, and 3D helper regressions. TypeScript build checking and web lint passed with two existing warnings outside this change. The two preference API suites passed 120 tests in the verified disposable test database. The existing Settings E2E and shortcut E2E passed all three cases in the isolated E2E environment.
- Desktop browser checks confirmed 1120px dialog width, focused search, browser/review-key rejection, recording cancellation on category changes, `I` task navigation after reload, `X` video frame navigation, and inactive old bindings. Browser errors were empty; test preferences were restored. The existing desktop-only Workbench gate remains in effect at narrow phone widths.
- Documentation media: the subsequent workbench media recapture replaced and reviewed `hotkey-cheatsheet.mp4` and its poster; the asset is registered in the maintained media manifest and review registry.
- Remaining work: none within this plan; later shortcut additions must extend the current command registry, conflict inventory, generated reference, and media workflow as applicable.
