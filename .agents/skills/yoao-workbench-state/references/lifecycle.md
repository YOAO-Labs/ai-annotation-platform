# Workbench lifecycle cases

## Layout and preferences

- Flush a retiring layout session's dirty snapshot to its original context; do not discard it when a new context becomes active. Serialize outstanding writes and prevent old responses from updating the new owner's state/cache.
- Read current schema constants and supported panels. Keep frontend and API validation consistent, including normalized integer floating bounds and context-specific panels.
- Newer/corrupt snapshots must retain recovery/read-only behavior. A restore failure must not silently overwrite the saved layout.
- Presentation groups differ from stored preference categories. When reorganizing settings, inspect `getFieldValue`, `buildFieldPatch`, and `lockableFieldName`; do not rename storage paths just to move a field.
- Keep project overrides separate from personal preferences. A display override must not be persisted as the user's own value.
- Exercise load failure/retry, rapid edits, save failure, unmount flush, and cross-user transitions when changing these paths.

- Named presets and automatic current-layout persistence have separate writers: `useWorkbenchNamedPresets.ts` owns `workspace.namedPresets`, while `useWorkbenchWorkspaceLayout.ts` owns current contexts. Preserve the preset revision check and unknown entries during whole-map replacement; omission can delete another saved preset.

## Editing and playback

- Trace keyboard, buttons, camera edits, numeric inputs, and propagation through the shared write guards. Disabled visible controls do not protect alternate entry points.
- Task navigation is latest-wins. Keep the scheduler abort signal and recheck it after every asynchronous leave guard before committing URL/task state.
- Native Mask mutations snapshot task/frame/tool/selection/generation and annotation version before submission. Recheck ownership after awaits; conflicts and network failures preserve the current draft for retry, while a late refresh must not repaint a retired session.
- Video Mask correction owns its captured annotation/frame/session/segment and single-flight save. Context drift rejects the commit rather than applying it to the newly selected object.
- Tracker seed collection owns the propagation dialog lifecycle. Opening a new propagation flow clears the previous brush range through the existing chapter-domain command; closing, switching task, or retiring the job clears only that flow's seed state. Do not duplicate these states in the shell model.
- Keep pending/invalid/saving/error states meaningful. Invalid input must cancel an older scheduled commit; task-lock responses and releases must not leak across tasks.
- Opening settings should pause video without snapping to a different frame when frame/draft stability is required. Inspect the existing playback controller's `snapToGrid` option.
- Scene playback waits for the intended frame to be ready. During task resolution, a temporary null task is not necessarily a terminal failure. Retain timeout and failure handling.
- Cancellation/rollback belongs to the current transaction owner; a child must not restore an old object's geometry into a newly selected object.
- Shared-query consumers should use the query lifecycle's cancellation signal rather than one consumer's private AbortController cancelling everyone.

- Capture the selected object and operation scope, not just the visible card: single-track and multi-target actions differ. Exercise each affected geometry's real edit/delete path; bbox parity does not prove polygon, mask or track behavior.

## Discussion and asynchronous decisions

- `shell/CommentInput.tsx` owns contentEditable text, mention chips and IME composition. Do not synchronize React editor state or submit Enter during active composition; preserve the caret around mention boundaries and commit final text at composition end. Verify Chinese input and the first typed character, not only Latin text or a programmatic input event.
- Decision dialogs can outlive their account, task, queue or checked selection. Capture the initiating scope, recheck it after the await, and do not apply the old answer to a new selection. For an inert action, first check whether the browser suppressed a native prompt and whether a request was actually sent.

## Tracker restoration

Refresh may begin before token hydration. Restoration must retry when authentication becomes usable, reconnect running jobs, and avoid duplicate subscriptions. Task switching during an in-flight preview/review request must not resurrect old candidates or emit old-task notifications.

Check socket/timer teardown and generation/task identity after awaits, including `enterReview` and preview fetches. Use the adjacent tracker tests and `apps/api/tests/test_video_tracker_jobs_list.py` for API-list semantics.

## Shared renderer and floating panels

One point-cloud renderer serves the main viewport and tri-views. Floating DOM layers, backgrounds, render overlays, clipping and pointer hit-testing must agree with the shared canvas. Do not introduce a second renderer to cover a layering bug. Inspect panel close/reopen and multiple overlapping floating groups as applicable.

## Authenticated original resources

- Use `apps/web/src/api/originals.ts` for authenticated original identity and bytes. It validates the exact platform download path; never attach the account Bearer to a guessed or provider-supplied URL.
- Own Blob URLs, ImageBitmaps, decoded frames, caches and GPU resources by account/session plus resource/content identity. A task change, logout/login as the same user, or content revision retires the old owner. Token refresh within the same owner is not a new content identity; retain the existing bounded retry rather than inventing cross-account fallbacks.
- Await actual readiness before accepting edits. A screenshot or mocked bitmap-construction call does not prove the committed production preview is visible. Verify the changed interaction, stale-response rejection, resource disposal, and saved/readback state when persistence is affected.
