# Notification coverage and unread counts

> Status: implemented (patch A + settings scope + patch B); see `## Outcome`.
> Date: 2026-09-15
> Requested outcome: visible unread counts and timely personal notifications while annotating or reviewing.
> Scope addition: expose notification settings inside Workbench settings and let users customize notification types.

## Findings

- `apps/web/src/App.tsx:161` mounts `useNotificationSocket` inside `AppShell`. The sibling `FullScreenWorkbench` routes for annotation and review do not mount it. Entering either Workbench removes the personal notification connection.
- `apps/web/src/components/shell/NotificationsPopover.tsx:340` already owns the trigger, list, target resolution, and unread query. Its trigger displays a dot at line 449, although the unread number is already available.
- `apps/web/src/hooks/useNotifications.ts` polls unread counts every 30 seconds. The Workbench has no consumer, so it also lacks this fallback. The list polls only while its panel is mounted.
- `apps/web/src/hooks/useNotificationSocket.ts` only shows a toast for `task.rejected`; connection opening does not invalidate notification queries.
- Existing REST endpoints provide account-wide unread counts, pagination, read operations, and deletion. WebSocket messages include notification IDs and a separate `notifications.sync` control event.
- `apps/web/src/stores/authQueryCache.ts` already clears and cancels cached queries on account/token changes. Reuse this owner.
- Notification navigation currently calls React Router directly. The Workbench owns video/Mask leave checks and task switching in `state/useWorkbenchShellModel.tsx`; a new notification entry must use those owners.
- Read/delete synchronization is published inside `NotificationService` before REST handlers commit. Another client can receive the event and fetch the old count. Some legacy new-notification producers also publish before commit; discussion producers already support deferred publication.
- `pages/Settings/SettingsPage.tsx:731` already renders account-wide per-type receipt settings using `GET/PUT /notification-preferences`. The Workbench settings dialog does not expose them. `notification_preferences.channels` is already JSONB, but the current API only exposes `in_app` and `email` and replaces the entire channels object on update.

## Recommendation and scope

The minimal option is to add the existing popover and socket hook to the Workbench and replace the dot with a number. Prefer moving the socket hook to the stable application root instead: it avoids connection teardown on ordinary route changes while reusing the same UI and backend.

Deliver coverage and configurable preferences as one coherent change. A second, independently useful patch fixes commit ordering for read/delete synchronization. Neither patch needs a new service, dependency, credential, environment variable, database table, or migration. Per-type popup customization adds a backward-compatible field to the existing preferences API and a preferences synchronization reason to the existing WebSocket control event.

The user selected **a persistent unread count plus transient alerts for important messages**. This preference is confirmed; implementation has not been requested.

The user requested a notification category in Workbench settings with per-type customization and explicitly selected separate controls for **receipt** and **transient alerts**. Both controls are confirmed scope. A receipt-only version was considered but does not satisfy the selected behavior. Implementation has not been requested.

Included surfaces: the main application, image/video/point-cloud annotation Workbenches, and the review Workbench. Reuse the common Workbench top bar across stages.

Out of scope: operating-system notifications, browser push subscriptions, sounds, email, a new notification-center page, project-specific overrides, per-thread unread counts, and a platform-wide rewrite of notification producers. The existing mobile Workbench block below 768px remains the product boundary.

## Product behavior

### Entry and count

- Keep the main-shell entry and add the same bell to the right-hand utilities in `pages/Workbench/shell/Topbar.tsx`, immediately before the Bug/theme/settings group. Keep it outside the group's responsive hidden container and outside the auto-hidden bottom-right buttons.
- Render no badge at zero, exact numbers for 1–99, and `99+` for 100 or more. The accessible label and tooltip expose the exact count, for example `通知，128 条未读`.
- Counts come from `/notifications/unread-count`, independent of loaded pages, filters, project, task, and annotation stage. Do not increment counts locally from incoming events.
- Preserve the last successful count during transient refresh failures. On initial failure, show the bell with an unavailable-count tooltip, rather than presenting a confirmed zero. The list distinguishes loading, fetch failure with retry, and an empty result.
- Opening the panel does not mark everything read. Preserve existing explicit row-read, mark-all-read, delete, and clear-read behavior; opening a row counts as reading it even if its destination is unavailable or the user cancels navigation.

### Panel and editing

- Reuse `NotificationsPopover`, its target resolver, and `ShellPopover`; retain filtering, date grouping, pagination, and job/export details.
- Keep the shared panel geometry and semantic overlay tokens. Ensure the badge is not clipped and the panel fits at 768, 1024, and 1440px widths in both themes.
- Include the notification panel and trigger in `workbenchInteractionGuards.ts`. While the panel is open, keyboard and pointer events used by it must not draw, delete annotations, change tools, or start playback. Escape closes the panel and restores focus to its trigger. Opening the panel does not submit a task or commit a draft.
- Add an optional navigation callback to `NotificationsPopover` for every route-changing action, including Bug navigation and error recovery links. The main shell retains normal navigation; the Workbench supplies its own callback through `Topbar` props from `useWorkbenchShellModel`.
- For same-project task changes, reuse `selectTask` and its cancellation/leave checks before applying the resolved URL. For cross-project or non-Workbench destinations, run the existing video and Mask leave checks before changing the route. Revalidate account and request ownership after each awaited check. Avoid applying the same admission twice when discussion URL hydration runs.
- Cancellation or failed saving retains the current task, frame, selection, and draft. Job detail dialogs and same-page feedback drawers do not trigger a leave flow. Keep the existing current-target permission checks, reviewer routing, discussion anchors, and `returnTo` behavior.

### Live updates and reminders

- Mount `useNotificationSocket` once in `App`, above route switching, and remove the `AppShell` call. Connect only for an authenticated session; ensure cleanup on logout, account replacement, and token renewal. Retired callbacks and pending token-refresh results cannot change a later session.
- Reuse existing notification query keys and auth cache cleanup. Every rendered bell observes the same unread query; list requests remain panel-driven.
- Invalidate notifications on connection open/reopen and on real notification/sync events. Preserve the existing job and failed-prediction query invalidation behavior. Ignore heartbeat frames and malformed messages.
- Keep the existing 30-second foreground unread polling fallback and explicitly refresh on browser focus/reconnect. Hidden tabs may be throttled by the browser; do not promise a strict background deadline. Reconnection fetches persisted state without replaying historical alerts.
- Default important-message alert types: `task.rejected`, `batch.rejected`, `batch.review_reopened`, `batch.admin_locked`, `annotation.comment_mentioned`, `feedback.comment_mentioned`, `feedback.reply_created`, `job.failed`, and `export.failed`. Users can change the effective alert choice for every known type through notification preferences. Other received types update the count and list without a toast.
- Use the existing toast adapter and durations. Alerts never steal focus or navigate automatically. Only a visible tab produces transient alerts; all open sessions synchronize counts.
- Deduplicate alerts by notification ID, retaining at most the latest 200 IDs for the current account. Group important events arriving within one second into one toast: use a short message for one event and `收到 N 条重要通知，请查看通知中心` for multiple events. Clear pending batches on logout/account changes, and never toast for `notifications.sync`, heartbeat, or REST history refresh.
- Preserve notification preference semantics: `in_app=false` suppresses persistence and live delivery for future events. Do not reinterpret it as a toast-only mute switch. Existing stored messages remain visible.

### Workbench notification settings

- Add a `通知` category to the Workbench settings navigation. Editing its controls keeps the current Workbench open. Include notification labels and descriptions in the dialog's existing global search.
- Reuse one `NotificationPreferencesPanel` from both this category and the personal settings page's existing `通知偏好` section. Both entry points use the same account-scoped query and mutation hooks, labels, and grouping. Do not store a second copy in `workbench` rendering preferences or localStorage.
- Use a dedicated category panel in the Workbench dialog, with its own loading/error/retry state. A rendering-preference loading error must not prevent users from opening notification preferences, and notification errors must not disable canvas settings. Keep these rows outside `buildFieldPatch` and `useWorkbenchConfig`.
- Display types under tasks/review, batches, discussions/mentions, exports/background jobs, Bug feedback, and account events. Use the backend's known-type list; show readable Chinese labels rather than requiring users to understand event identifiers. Group headings organize controls without adding group-level write operations.
- Show two controls per type: `接收通知` and `弹出提示`. Explain the account-wide effect at the top: `设置随账号同步，适用于主界面及所有标注、审核工作台。`

| Receipt | Popup    | Result                                                                                                 |
| ------- | -------- | ------------------------------------------------------------------------------------------------------ |
| On      | On       | New events are saved, counted as unread, and shown as transient alerts in a visible tab.               |
| On      | Off      | New events are saved and counted; no transient alert.                                                  |
| Off     | Disabled | Future events are neither saved nor delivered. Historical notifications and their unread state remain. |

- Preserve the stored popup choice when receipt is disabled; disabling receipt does not reset it. Enabling receipt again restores that choice. The bell and existing unread count remain visible regardless of preference changes.
- Preserve every existing receipt choice. For accounts/types without a popup override, enable popups only for the default important types above. Users can enable ordinary completion events or disable important-event popups; unknown future types default to no popup.
- Auto-save each change. Disable the two controls for that type while saving, show saving status, and restore the last confirmed value with an inline retry on failure. Other types remain usable. Loading failures disable writes instead of saving guessed defaults.
- Close/category changes do not cancel already submitted saves. Account replacement invalidates the request owner so a late response cannot change another account's controls or show a stale success/failure message. New preferences affect future alerts, including batches not yet displayed; do not replay prior notifications when a toggle is enabled.

### Preferences contract and synchronization

- Extend each `GET /notification-preferences` item with an effective boolean `toast`. Keep `type`, `in_app`, and `email` intact.
- Extend the existing PUT request to accept `type` plus optional boolean `in_app` and/or `toast`; require at least one supplied setting, reject null values, and retain the existing known-type validation. Existing clients sending only `type` and `in_app` continue to work.
- Store explicit popup choices as `channels.toast` in the existing row. Merge only supplied keys atomically into JSONB during upsert, preserving omitted fields and reserved `email`. This avoids erasing popup settings when the personal page or an older client changes receipt, and avoids lost updates to different channels in concurrent tabs.
- Move the current known-type registry and the proposed popup defaults to `apps/api/app/services/notification_preferences.py`, consumed by the preferences API. The frontend reads the effective values; it does not keep a competing permanent allowlist.
- After a successful preference commit, publish `notifications.sync` with `reason=preferences` using the existing per-user channel. This event refreshes the preference query without creating a notification, changing its unread count, or showing a toast. Failed/no-op writes do not need a synchronization event.
- Add `useNotificationPreferences` with key `["notification-preferences", userId]`. Keep one active query for the authenticated socket consumer; the two settings entry points observe that cache. Use the existing 30-second foreground refresh cadence plus focus/reconnect and preference-sync invalidation, so a missed synchronization event has a recovery path.
- A real notification always invalidates its count/list queries. Its transient alert additionally requires loaded, current-account preferences with both receipt and popup enabled. While preferences are unavailable or invalidated by a preference-change event, suppress transient alerts; continue receiving and counting notifications. Do not retrospectively alert after preference loading succeeds.
- Update OpenAPI and frontend artifacts with the existing export/codegen workflow. This is an additive API change, not a requested application release.

```text
App (authenticated lifetime)
  -> useNotificationSocket -> notification query invalidation
                           -> effective per-type preferences -> existing toast adapter
Workbench settings ------\
                          -> shared preferences panel/query -> existing preferences REST
Personal settings -------/
Main TopBar ------------\
                         -> NotificationsPopover -> shared unread/list queries -> REST
Workbench Topbar -------/                         -> target resolver
                                                   -> Workbench leave checks -> navigation
```

## Delivery patches and file targets

### A. Coverage, counts, safe reminders, and settings

This is larger than eight files once integration tests and documentation are included. It now includes a preferences API extension alongside the frontend, with one shared live connection and existing data owners.

| File                                                                                                        | Change                                                                                |
| ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `apps/web/src/App.tsx`                                                                                      | Move the socket to the application lifetime.                                          |
| `apps/web/src/hooks/useNotificationSocket.ts`                                                               | Reconnect synchronization, session ownership, alert allowlist/deduplication/batching. |
| `apps/web/src/hooks/useNotifications.ts`                                                                    | Explicit focus/reconnect refresh behavior; preserve foreground polling.               |
| `apps/web/src/components/shell/NotificationsPopover.tsx`                                                    | Numeric badge, query states, optional guarded navigation callback.                    |
| `apps/web/src/pages/Workbench/shell/Topbar.tsx`                                                             | Reuse the lazy notification entry with the navigation callback.                       |
| `apps/web/src/pages/Workbench/state/useWorkbenchShellModel.tsx`                                             | Supply notification navigation through existing state owners.                         |
| `apps/web/src/pages/Workbench/state/workbenchInteractionGuards.ts`                                          | Protect notification interactions from canvas listeners.                              |
| Existing socket, notification navigation, topbar, and interaction-guard tests                               | Cover the behavior below.                                                             |
| `apps/web/src/App.notifications.test.tsx`                                                                   | Add route-lifetime coverage, including direct Workbench entry.                        |
| `docs-site/user-guide/reference/notifications.md`, `docs-site/dev/reference/ws-protocol.md`, `CHANGELOG.md` | Document the shipped UI, connection lifetime, and user impact under Unreleased.       |

Additional file targets for the settings scope:

| File                                                                                                                                               | Change                                                                                                                      |
| -------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `apps/web/src/components/notifications/NotificationPreferencesPanel.tsx` and `NotificationPreferencesPanel.test.tsx`                               | Shared grouped preference controls, loading/error/saving behavior, and labels extracted from the personal settings section. |
| `apps/web/src/hooks/useNotificationPreferences.ts` and `__tests__/useNotificationPreferences.test.tsx`                                             | Account-owned query, per-type updates, rollback, and synchronization.                                                       |
| `apps/web/src/pages/Workbench/shell/WorkbenchSettingsDialog.tsx` and its test                                                                      | Dedicated notification category, search, and independent loading state.                                                     |
| `apps/web/src/pages/Settings/SettingsPage.tsx` and its test                                                                                        | Replace the private receipt-only section with the shared panel.                                                             |
| `apps/web/src/api/notifications.ts`                                                                                                                | Extend the preferences types and update request.                                                                            |
| `apps/api/app/services/notification_preferences.py`                                                                                                | Known-type registry and effective defaults.                                                                                 |
| `apps/api/app/api/v1/notifications.py`, `apps/api/app/services/notification.py`, `apps/api/app/db/models/notification_preference.py`               | Additive preferences contract, atomic channel merge, post-commit preferences sync, and model documentation.                 |
| `apps/api/tests/test_notifications.py`                                                                                                             | Preference API compatibility, receipt/popup independence, storage defaults, and publication tests.                          |
| `docs-site/user-guide/workbench/settings.md`, `docs-site/user-guide/reference/settings.md`, `docs-site/api/`, `README.md`, generated API artifacts | Document the two entry points, account scope, default alert behavior, and additive API contract.                            |

Keep alert batching local to the socket hook; its effective type choices come from the shared preference query. Do not introduce a new global notification store, provider, or cross-tab leader election.

### B. Commit-ordered read/delete synchronization

- In `apps/api/app/services/notification.py`, remove publication from the four read/delete mutation methods and expose the existing synchronization publisher as a method callable after commit.
- In `apps/api/app/api/v1/notifications.py`, publish the existing `notifications.sync` envelope only after successful commit and only when an operation changes rows. Read operations use `reason=read`; deletion/clear operations use `reason=deleted`.
- Update `apps/api/tests/test_notifications.py` to assert ordering, rollback silence, no-op silence, ownership, and best-effort publication failure behavior. Search all four service-method callers before changing their publication responsibility.
- No new event shape or migration. API handlers already run in an existing runtime; refresh the affected API process using the runtime skill during implementation verification.
- This patch corrects read/delete synchronization only. Legacy creation paths that publish before commit can still delay badge convergence until polling. Do not claim universal immediate consistency for all notification producers; a general producer transaction change needs its own scoped review.

## Verification and acceptance

1. Enter the main shell and each annotate/review route directly, then switch between them. After effect cleanup settles, there is one active personal-notification WebSocket per tab; logout leaves none. Test late events and refresh responses from a retired account.
2. Verify badge values 0/1/99/100/128, exact accessible labels, fetch failure states, and counts with more than one page and an active type filter.
3. With a notification committed before delivery, assert that receiving the event refreshes the unread count without opening the panel. Record delivery-to-visible-update latency in the local browser; use two seconds as the local acceptance target, not a production SLA.
4. Block the socket while HTTP remains available: the foreground count converges on the next 30-second polling interval. Reconnect or return to the foreground: the next refresh occurs immediately. Historical messages do not generate a toast burst.
5. Validate default important types and user overrides, one-second batching, duplicate IDs, ordinary messages, `ping`, both read/delete and preferences synchronization, hidden tabs, and account changes.
6. Validate row read/delete, all-read, and clear-read with two sessions for one account. For patch B, verify another session reads committed state, and failed transactions do not broadcast success.
7. Open, close, scroll, filter, and keyboard-navigate the panel with image/video/3D editing active. Escape closes the panel without cancelling an unrelated drawing. Test cancellation and save failure when navigating with a pending Mask/video draft, including cross-project destinations and reviewer links.
8. Check 768/1024/1440px and light/dark themes. The bell remains visible, the number is readable, and the panel fits without covering the submit controls. Capture only needed evidence and clean temporary screenshots, test rows, traces, caches created by the run, and browser sessions afterward.
9. In Workbench settings, disable popup for a received type: a subsequent event increases the count and enters the list without a toast. Disable receipt: subsequent events are not persisted or counted; old unread items remain. Re-enable receipt and verify the saved popup choice is restored.
10. Change a preference in either settings entry, navigate to the other, reload, and use a second session. Verify account scope, synchronization, recovery after missed events, default values for old rows, and no effect on another account. Verify failed saves, failed initial loads, late responses, independent rendering-config failure, and global settings search.
11. API tests cover old receipt-only requests preserving `toast`, toast-only requests preserving receipt/email, atomic concurrent updates to different keys, invalid/null/empty updates, preference ownership, post-commit sync, and no notification row created by changing preferences.

Frontend commands from the repository root:

```bash
pnpm --filter @anno/web test src/App.notifications.test.tsx src/hooks/__tests__/useNotificationSocket.test.tsx src/components/shell/NotificationsPopover.test.tsx src/components/shell/NotificationsPopover.navigation.test.tsx src/components/shell/NotificationsPopover.navigation.resolve.test.ts src/components/shell/ShellPopover.test.tsx src/pages/Workbench/shell/Topbar.test.tsx src/pages/Workbench/state/workbenchInteractionGuards.test.ts src/pages/Workbench/state/useWorkbenchHotkeys.test.ts src/pages/Workbench/state/useDiscussionNavigation.test.tsx
pnpm --filter @anno/web test src/components/notifications/NotificationPreferencesPanel.test.tsx src/hooks/__tests__/useNotificationPreferences.test.tsx src/pages/Workbench/shell/WorkbenchSettingsDialog.test.tsx src/pages/Settings/SettingsPage.test.tsx
pnpm --filter @anno/web typecheck
pnpm --filter @anno/web lint
pnpm --filter @anno/web lint:css-tokens
git diff --check
```

For the preferences API work and patch B, load `.agents/skills/aap-runtime/SKILL.md`, verify disposable test-mode resource ownership, and use the checkout's Python virtual environment:

```bash
pnpm dev:worktree -- init --mode test
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_notifications.py'
pnpm dev:worktree -- stop --mode test
```

Use the runtime workflow's ownership-checked cleanup for test resources; never infer that a development database is disposable. Do not bump versions or deploy as part of this planning request.

After the preferences API changes, run `pnpm openapi:export`, `pnpm openapi:check`, and `pnpm --filter @anno/web codegen`, then verify the generated diff and the frontend types. Check affected SDK contracts against the snapshot without inventing an SDK release.

## Effort, risks, and rollback

- Planning estimate: patch A approximately 3–4 engineering days including preference compatibility tests and browser verification; patch B approximately half a day to one day. The settings addition accounts for roughly one extra day. Workbench navigation and account-owned preference updates are the main uncertainties.
- Fragile assumption: existing navigation owners cover the pending edit being left. If a notification destination bypasses them, the apparent UI improvement can discard a draft. The injected navigation callback and cancellation tests are mandatory for patch A.
- Reliability boundary: Redis/WebSocket publication is best effort and some creation paths precede commit. Persisted REST data and foreground polling provide recovery; browser background scheduling has no fixed deadline.
- Roll back the frontend independently while retaining the backward-compatible preferences API merge behavior, which preserves stored popup choices for receipt-only clients. No data migration is needed. Reverting the old API too restores its whole-object overwrite behavior, so later receipt edits can discard stored popup choices; avoid that rollback order when preserving custom preferences matters. Patch B remains independently revertible, and existing notification records remain readable.

## Evidence and current verification status

- Source and matching archived notification/reauth decisions were reviewed. The current implementation, rather than historical plan text, determines the proposal.
- React's effect lifecycle supports placing a subscription under a stable owner with cleanup on dependency changes: [React useEffect](https://react.dev/reference/react/useEffect).
- Continue the existing targeted invalidation approach, which refreshes active queries and marks inactive queries stale: [TanStack Query invalidation](https://tanstack.com/query/latest/docs/framework/react/guides/query-invalidation).
- Read-only browser inspection reached the login page of the primary checkout at `http://localhost:3000`; the public registration-status request returned 200 and the captured console showed no errors. There was no authenticated browser session, and this was not the current worktree's runtime. It does not constitute acceptance of the notification or Workbench flow. The temporary browser session was closed.
- No application/backend code was changed and no application tests were run during planning. Only this proposal was added; implementation tests and authenticated browser acceptance remain future work.

## Outcome

- Status: patch A（覆盖/角标/瞬时提醒/设置入口）与 patch B（读/删除提交后同步）均已实施并通过本地验证；落地提交见当前分支。
- Release milestone: Not yet determined（按计划不随本次实施发版）。
- Backend: `apps/api/app/services/notification_preferences.py`（已知类型与默认弹出注册表）、`apps/api/app/api/v1/notifications.py`（偏好契约扩展、提交后发布）、`apps/api/app/services/notification.py`（读/删除方法不再发布，公开 `publish_sync`）。
- Frontend: `apps/web/src/hooks/useNotificationSocket.ts`（App 生命周期、偏好门控、去重/批量）、`useNotificationPreferences.ts`、`components/notifications/NotificationPreferencesPanel.tsx`、`components/shell/NotificationsPopover.tsx`（数字角标、导航回调）、`pages/Workbench/shell/Topbar.tsx`、`state/notificationWorkbenchNavigation.ts`、`state/workbenchInteractionGuards.ts`、设置两入口。
- User documentation: `docs-site/user-guide/reference/notifications.md`、`docs-site/user-guide/reference/settings.md`、`docs-site/user-guide/workbench/settings.md`。
- Developer documentation: `docs-site/dev/reference/ws-protocol.md`、`docs-site/dev/concepts/audit-and-notifications.md`。
- CHANGELOG: Unreleased 增 Added（通知覆盖/提醒/偏好）与 Fixed（提交顺序）条目；OpenAPI 快照与前端生成类型已再生成（加性变更）。
- Verification: 前端目标套件 15 文件 251 用例 + 全量 527 文件通过，typecheck/lint/lint:css-tokens 通过；API `tests/test_notifications.py` 22 用例及 discussion/reopen 通知相关套件通过（一次性 test 数据库）；浏览器实测：主界面与工作台角标/实时刷新/瞬时提醒、偏好开关（含静音不落库、弹出选择保留）、双标签页偏好同步、768/1024/1440 与暗色主题面板几何、设置全局搜索。
- Remaining work: 视频/Mask 草稿离开检查的浏览器级取消流依赖视频任务种子，本轮由既有单测（`useDiscussionNavigation`、`workbenchInteractionGuards`、`notificationWorkbenchNavigation`）与守卫集成覆盖；旧通知生产者在提交前发布的路径仍按计划留待单独评审。
