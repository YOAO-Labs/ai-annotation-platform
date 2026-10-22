# T02 — Training configuration workspace

> Status: completed — delivered and verified on 2026-10-15
> Priority: first
> Depends on: [T01 parameter catalog](1792035920_training-parameter-catalog.md)
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and existing owners

Managers configure a training experiment without navigating a long modal or losing the meaning of advanced options. The project remains the creation entry. Preserve global `/training` browsing and the existing project tabs.

Reuse [ProjectTrainingPanel](../../../apps/web/src/pages/Projects/ProjectTrainingPanel.tsx), [ProjectLearningPage](../../../apps/web/src/pages/Projects/ProjectLearningPage.tsx), [routing](../../../apps/web/src/App.tsx), [typed training API](../../../apps/web/src/api/trainingRuns.ts), and current preflight/create semantics. The Workbench [SchemaForm](../../../apps/web/src/pages/Workbench/components/SchemaForm/index.tsx) offers patterns but its scalar-only controls and numeric formatting are insufficient for complete training configuration; avoid changing Workbench behavior as collateral work.

## UI and state contract

- Add a project-managed full-page creation view with steps: data/model, parameters, review/start. Keep older `learning?tab=training&new=1` links working by redirecting into the new view.
- Desktop uses category navigation, the active form, and a sticky experiment summary. Mobile uses grouped accordions and an accessible bottom action area. Preserve the application theme, compact typography, semantic tokens, and local UI adapters.
- Categories cover basic training, optimizer, learning-rate schedule, augmentation, loss, fine-tuning, evaluation/checkpoints, and performance/reproducibility. Common and all-parameter views share one state; search matches Chinese names, native keys, and reviewed aliases. Filter modified fields and reset individual fields/groups.
- Show effective values, units, constraints, and origin. Scientific notation and small learning rates must remain exact. Use suitable enum, optional, list, range-pair, and union controls; hiding a conditional field cannot silently submit an invalid value.
- Framework/model changes preserve experiment name and authorized data selection, show compatible retained settings and incompatible overrides, and require explicit acceptance before discarding edited values. Do not silently substitute another base model or optimizer.
- Maintain a local recoverable draft scoped to user, project, and schema identity. Store configuration only, never credentials or signed URLs. Revalidate after refresh; authority revocation prevents actions even when a draft remains locally cached.
- YAML import uses a safe data parser and a bounded file. Show proposed changes, unsupported keys, and type errors before applying. Export a portable recipe definition with schema/runtime identities and resolved overrides; exclude server paths and download credentials. Import never starts a job.
- Any execution-affecting change invalidates preflight. Duplicate clicks reuse the existing request identity; changed intent gets a new identity. Late responses cannot submit or navigate a different project/draft.

```text
Project entry -> creation route -> shared draft state
                              +-> catalog-driven grouped controls
                              +-> review summary -> existing preflight -> create
```

## Scope and acceptance

Candidate additions are a creation page, training-specific schema controls, draft/import helpers, and route integration; retain list/detail ownership in the existing training panel. This is likely more than eight files including tests/docs. No new server service or account is needed; browser YAML dependency reuse must be checked before adding a parser.

Verify empty/invalid imports, duplicate YAML keys, union types, schema upgrades, conditional fields, exact numeric round-trips, reset/modified filtering, refresh/back, rapid project/framework switching, preflight invalidation, and idempotent creation. Inspect actual requests/console, keyboard focus/errors, dark mode, and 390px layout. A meaningful accepted creation must produce the same frozen configuration shown in review.

The fragile assumption is that the catalog can express field applicability and dependencies; T01 must settle that contract first. The minimal option retains the modal with a small common-field expansion. Rollback returns project creation to the existing modal while keeping read-only access to richer frozen configurations. Apply the shared verification, cleanup, documentation, and promotion protocol.

## Promotion review — 2026-10-15

Baseline: T01 commit `0c552c411`. All inventory, native contract, legacy serialization and live resume gates have passed. The existing modal is now too narrow for the 36/35 qualified fields, and its mounted-only mutation guards cannot protect a changed draft. Replace creation ownership while retaining list/detail ownership. This is a large frontend change across more than eight files, with no API, database, executor image, environment variable or credential change.

### Design and interaction decisions

The direction is the existing compact annotation application: neutral semantic surfaces, small Lucide controls, existing page widths, borders and typography, and no decorative animation. Use Tailwind semantic classes and current UI adapters exclusively. The page first establishes data/model identity, then grouped parameters, then a verified review and start action. Desktop category navigation and an experiment summary frame the editable form; narrow screens use native accessible group disclosures and a reachable sticky action area.

The canonical route is `/projects/:id/learning/new`, protected by `RequireProjectAccess(project.manage)`. Legacy `learning?new=1` links replace-navigate there, carrying a controlled framework hint and return-run context. The list entry stays inside the project; the global training page gains no new create action.

New drafts require explicit data/base-model selection. Catalog polling never replaces a selected framework/model. A requested switch fetches the target schema, presents retained and discarded edited fields and the exact target model, and applies only after explicit acceptance. Name and data selection survive. An unavailable selection or changed schema blocks new submission without erasing the draft.

### Exact implementation surfaces

- `apps/web/src/pages/Training/`: `TrainingCreatePage.tsx`, training-specific parameter controls/editor/review components, `trainingConfiguration.ts` for bounded rules/import/export, `trainingDraftStorage.ts` and a draft owner hook for local recovery, and focused helper/page tests.
- `apps/web/src/pages/Projects/ProjectTrainingPanel.tsx` and its test: remove creation-modal state; retain experiment list/detail/actions and project navigation.
- `apps/web/src/pages/Projects/ProjectLearningPage.tsx`: old-link redirection and remove the ready-dataset pagination formerly owned by the modal.
- `apps/web/src/App.tsx`: lazy route integration with the existing project-management guard. `apps/web/package.json` and `pnpm-lock.yaml`: declare the already locked `yaml@2.8.4` directly; do not use an undeclared transitive dependency.
- `apps/web/src/api/trainingRuns.ts`: optional abort signals for catalog/schema/preflight requests and reuse of the generated catalog template type, including announced parameter identities. No wire API changes.
- `docs-site/user-guide/online-learning/index.md`, `CHANGELOG.md`, this promoted plan, backlog index and dependent draft links.

### Configuration and import contract

Submit only `configuration {schema_id, schema_digest, values}` through existing preflight/create endpoints. Never combine it with legacy `parameters`. Keep editable overrides separate from effective defaults. Preserve small/scientific numbers and strict boolean/null/list types; invalid text remains editable but cannot pass preflight. The browser validates only the catalog's bounded rule vocabulary. Python remains authoritative for cross-field dependencies: keep fields visible and display preflight `effective_values`, `origins` and `inactive_fields`; do not create a second authoritative dependency table.

YAML uses a 64 KiB limit, one core-schema document, unique string keys, no aliases/merge/custom tags or prototype keys, finite JSON data and bounded nesting. Accept a current-context editable-parameter fragment or a portable `aap-training-configuration` document with format version 1, framework/base model, schema/version/runtime identity and `values`. Native dataset YAML and full observed configurations are rejected by the editable-key whitelist. Normalize reviewed aliases before collision/type validation. Preview all replacement/reset changes and all errors; apply atomically only while the preview's owner and revision still match. Export canonical editable overrides plus identity, excluding paths, credentials and signed URLs; import never enqueues.

### Draft, session and request ownership

Local records bind user, project, schema ID/digest and exact model choice. Hydrate synchronously into a state carrying its owner; persistence is allowed only for that owner and session. Retain changed-definition drafts for an explicit migration preview. Storage failures are visible. Store configuration only, never bearer tokens or URLs.

Every asynchronous schema switch, file read, preflight and create captures an immutable owner/session/draft revision and payload. A synchronous draft ref retires old callbacks before React commits the next render. Recheck current authentication ownership; fresh project access and the API's own authority checks guard actions. Late results cannot update a new owner/revision, clear its storage, or navigate it.

Changing intent rotates the request ID and clears preflight. Creation captures the exact payload including preflight digest. Persist a credential-free pending-submission envelope before sending, so a lost response and refresh can retry the identical request. While its outcome is unknown, offer retry, inspect the list, or explicitly discard the pending intent before editing; do not run a fresh preflight over it. A matching success clears only its own draft and returns to the existing experiment detail.

### Verification and rollback

- Pure tests: strict scalar/union/pair values, scientific-number round trips, bounded YAML, duplicate keys/aliases, rejected tags/prototype keys/deep input, canonical aliases, atomic preview and owner/schema-bound draft/pending-envelope recovery.
- Component tests: old-link compatibility; input/preflight invalidation; lost create response with same payload after refresh; late preflight/create/import under changed draft/session/project; incompatible framework switch; pagination and missing selections; errors and permissions.
- Run targeted Vitest, web type checking, web lint/CSS tokens, Markdown format/link checks, docs build and `git diff --check`. API contracts are unchanged; no backend suite or disposable database is needed for this frontend node.
- Browser: real existing project, keyboard, both themes, 390px width, draft refresh, import preview, preflight and one bounded native creation. Verify its frozen configuration equals the displayed review. Inspect network and console. Delete only the temporary run/artifacts/jobs/notifications and owned browser/draft/download/build artifacts; preserve the two original acceptance runs and running development services.
- Rollback restores the previous creation modal/entry while retaining readable T01 configurations and pending request identities; do not reinterpret or delete accepted experiments. Keep dormant local records versioned for explicit recovery.

## Delivery evidence

The independent project creation route now owns the three-step workflow. The existing training panel owns list/detail/actions only. Catalog-driven controls expose all 36/35 qualified parameters, preserve explicit overrides separately from defaults, and render the server's effective values, origins and inactive fields before start. Framework/model switches and schema migrations show retained/discarded overrides before acceptance. Import previews replace overrides atomically; exports contain canonical editable values and portable identities only.

Draft records and asynchronous callbacks bind account, session, project, draft revision and parameter identity. Pending creates retain the exact request body across refresh and bypass a changed catalog only for an identical idempotent retry. Fresh actions recheck project authority. A catalog fingerprint includes the resource, complete selected base-model identity, image/version and announced parameter definitions. Changed definitions retire preflight and trigger a definition refresh; stale import and migration previews cannot overwrite current state.

Verification completed:

- 53 focused Vitest cases across five files passed. Coverage includes strict scalar/union/range values, scientific notation, safe YAML and canonical aliases, storage ownership and exact pending recovery, old-link redirects, framework migration, preflight invalidation, catalog changes, authority failure, late callbacks, refreshed retries and older data selection. The final creation-page file passed all 16 cases after adding the framework-switch regression.
- Full web type checking passed. Web lint passed with zero errors and five existing warnings; final changed-file lint and the CSS token gate passed. Formatting and `git diff --check` passed. Documentation build passed in 34.31 seconds with existing syntax-highlighting notices.
- Real browser checks covered desktop and 390px layouts, both themes, draft refresh, YAML import/export, keyboard errors/scientific input, modified filtering/reset and active API calls. WCAG A/AA automation reported zero violations; its incomplete check is not claimed as manual accessibility certification. Console error capture was empty. The bottom action area reserves space for the existing feedback control.
- One temporary two-epoch Ultralytics experiment was created through the actual page using eight imported overrides, including AdamW and `lr0: 1e-6`. All 36 displayed editable values matched preflight. The created frozen configuration and digest matched preflight exactly. Training succeeded; the downloaded `native-config.json` passed size/SHA checks and verified 68 observed fields against the same configuration digest.
- The temporary run/artifacts, its one AsyncJob and one notification, local drafts, downloaded YAML, screenshots, browser/vault and generated test/build artifacts were removed. The two original acceptance runs, three original unread notifications, data version, theme preference and running development services were preserved.

No API/executor implementation change, database migration, version bump, push or remote CI result belongs to this node.

## Outcome

- Delivered scope: Delivered the project-owned configuration route, grouped native controls, preflight, owned draft recovery and YAML import/export with exact pending-request replay.
- Completion evidence: the existing `Delivery evidence` section above records implementation and verification. Archival on 2026-10-22 reuses that record; application tests and live acceptance were not rerun for this documentation move.
- Enduring documentation: [configuration.md](../../../docs-site/user-guide/online-learning/configuration.md), [index.md](../../../docs-site/user-guide/online-learning/index.md); user impact is recorded in `CHANGELOG.md`.
- Release milestone: no new milestone or version is assigned by this archival.
- Remaining boundaries: preserve the exclusions and qualification limits recorded above; new task, inference or deployment scope requires its own plan and acceptance.
