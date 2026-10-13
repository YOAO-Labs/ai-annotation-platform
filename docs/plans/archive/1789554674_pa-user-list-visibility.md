# Project-admin user visibility: align "Users & Permissions" with member assignment

> Status: implemented — code, tests, and docs landed; awaiting review.
> Date: 2026-09-16
> Issue: [#115](https://github.com/yyq19990828/ai-annotation-platform/issues/115) — 项目管理员在成员分配中可见未分配标注员，但在用户与权限列表中不可见.
> Requested outcome: a project admin finds the same enabled, not-yet-assigned annotators/reviewers in **both** the member-assignment entry points and the **Users & Permissions** list, and can also **see enabled super admins there (read-only)**, while every account-management write (role change, deactivate, delete, password reset, group assignment) stays bounded to the admin's own projects and stays fully denied against super-admin accounts.
> Motivation: two entry points return different populations for the same actor, so admins conclude an account does not exist and cannot look people up before assigning them; super admins are also invisible, so admins cannot even find who to contact about cross-project accounts (role-impact copy already tells them to "请联系超级管理员核对").
> Non-goals: changing who can **manage** accounts; widening visibility of other project admins, viewers, or disabled accounts (incl. deactivated super admins); touching project member lists / performance / task data scopes; changing invitation flows.
>
> **Revision 2 (maintainer, 2026-09-16, after live acceptance):** read-only rows for unassigned workers were rejected — the disabled "仅可查看" state is wrong product-wise. Until annotator/reviewer identity becomes project-driven (planned follow-up), project admins **manage every enabled annotator/reviewer account** (unassigned or in other projects' memberships) for account-level writes: role switch (annotator ↔ reviewer), password reset, group assignment, edit. Lifecycle writes that hand over work (offboarding / deactivate / delete) additionally require the target's projects to be owned by the actor — unassigned accounts pass, straddling/foreign members stay gated on a super admin. Enabled super admins remain strictly read-only; deactivated accounts, other project admins, and viewers remain out of scope. Project-filtered queries stay project-bounded. The rest of this document describes Revision 1 as implemented; the Revision 2 deltas are marked inline and the code/tests/docs now encode Revision 2.

## Root cause (verified in this checkout)

- `apps/api/app/services/management.py` — `user_scope_clause` limits a project admin's management queries to `self ∪ members of projects they own`; `build_user_query` → `fetch_user_page` / `user_stats` reuse it, and `apps/api/app/api/v1/users.py::export_users` builds on `build_user_query`. So `GET /users/query`, `/users/stats`, `/users/export` all hide unassigned workers.
- `apps/api/app/api/v1/users.py::list_users` — the legacy picker deliberately bypasses that scope: for a project admin, `GET /users?role=annotator|reviewer&status=active` returns the **full platform candidate list** (comment: "指派候选人场景：必须看到全量 annotator / reviewer").
- `apps/web/src/components/projects/AssignMemberModal.tsx` (existing project → "添加项目成员") calls `usersApi.list({ role })` → sees unassigned workers.
- `apps/web/src/components/projects/steps/Step6Members.tsx` (new-project wizard) calls `useUsers()` **without** a role and filters client-side → for a project admin it currently shows only own-project members, i.e. the wizard is _inconsistent with the assign modal_, not with the Users page. The issue asks both member-assignment entries to agree, so the wizard must switch to the role-scoped candidate queries too.

## Design decisions

### D1 — Separate read visibility from manage scope (backend, `app/services/management.py`)

Keep two clauses with distinct jobs:

- `user_scope_clause(actor, project_id)` — **manage scope**, unchanged: `self ∪ own-project members` (∩ project when given). Still used by write guards: `users.py::_group_assignment_item`, `apply_bulk_group_assignment`.
- New `user_visibility_clause(actor, project_id)` — **read scope**, consumed by `build_user_query` (therefore automatically shared by `/users/query`, `/users/stats`, `/users/export`):
  - super_admin: `true()` plus the existing explicit project narrowing — unchanged.
  - project*admin, **no** `project_id`: `or*(self, own-project members, User.is_active == True ∧ User.role ∈ {annotator, reviewer, super_admin})`. Super admins become visible (maintainer request, 2026-09-16): read-only contact/lookup, never manageable.
  - project_admin **with** `project_id`: exactly today's `user_scope_clause(...) ∩ project members`. Project-filtered member listing stays project-bounded; a foreign project id still yields nothing, so no cross-project membership disclosure through the project filter.
  - Role / status / group / search filters compose on top as today:
    - `status=active` (default): unassigned enabled annotators/reviewers and enabled super admins appear — the fix.
    - `status=inactive`: widened arm requires `is_active`, so unassigned disabled accounts **and deactivated super admins** stay hidden — no expansion to disabled accounts.
    - `role=viewer|project_admin`: widened arm contributes nothing, so only own members (and self) match — no expansion to other admins/viewers.

`user_stats` and `export_users` already call `build_user_query`, so list / stats / export stay consistent by construction (acceptance: "搜索、角色筛选、分页和列表统计采用一致的人员可见范围"). Export fields and `user.export` audit logging are untouched.

### D2 — Expose `is_managed` on `/users/query` items (backend + types)

Visibility must not imply editability, and the frontend cannot derive "is this user in one of my projects" without another query. Add a request-relative flag:

- `app/schemas/management.py`: new `UserPageItem(UserOut)` with `is_managed: bool`; `UserPage.items: list[UserPageItem]`.
- `users.py::query_users`: for super_admin all rows `True`; for project_admin compute the managed id set once per request (reuse `_managed_user_ids` as one `SELECT`) and flag the paged rows. Other endpoints keep returning `UserOut`.
- Regenerate API artifacts: `pnpm openapi:export`, `pnpm codegen` (updates `apps/api/openapi.snapshot.json` and `apps/web/src/api/generated/types.gen.ts`); extend the hand-written `UserResponse` extension in `apps/web/src/api/users.ts` with `is_managed?: boolean`.

### D3 — All write endpoints stay strictly guarded (backend, no code change expected)

`preview_user_role_change` / `change_user_role` (403/404 "该用户不在你管理的项目内"; for super-admin targets the assignable-role check fires first with "项目管理员仅能在审核员 / 标注员 之间切换角色"), `admin_reset_password` (role-level check: super_admin is level 0, project_admin level 1 → 403 "只能重置等级低于你的用户的密码"), `deactivate_user`, `delete_user` (both fail the `_PA_ASSIGNABLE_ROLES` check with 403), `UserLifecycleService` offboarding, and bulk group assignment (manage scope, itemized "用户不存在或不在管理范围内") are untouched — direct API writes cannot escalate, and super-admin accounts are doubly protected (role matrix + manage scope). Regression tests pin this.

### D4 — UsersPage action buttons reflect manageability (frontend)

`apps/web/src/pages/Users/UsersPage.tsx` gates row actions by `EDITABLE_TARGET_ROLES_BY_ACTOR` only. Add `u.is_managed !== false` to that condition for the edit / reset-password / offboarding / delete group, with the existing disabled-button title pattern ("该用户不在你管理的项目内，仅可查看" for project admins; super_admin always sees `is_managed: true`, so nothing changes for them). This prevents the new confusing case: an unassigned annotator whose edit button opens a modal that 404s. Cross-project members (in own projects) keep today's behavior — buttons enabled, writes fail with the existing explicit 403 reasons.

Super-admin rows: `super_admin` is not in a project admin's `editableTargets`, so they already fall into the existing disabled-button fallback branch (title "无权修改该用户") — visible but not operable, exactly the requested "无法操作这个身份". Optionally specialize the disabled title for this case (e.g. 「仅可查看：超级管理员账号」) for clarity.

Bulk "批量分配数据组" keeps itemized preview errors for unmanaged selections; no change.

### D5 — Wizard `Step6Members` uses the same candidate queries as the assign modal (frontend)

Replace `useUsers()` + client-side role filter with two role-scoped queries (`useUsers({ role: "annotator" })`, `useUsers({ role: "reviewer" })`), merged and deduped by id, ordered by `created_at` desc. Both then hit the backend's wide-candidate branch for project admins, and super-admin behavior is unchanged. This makes "新建项目分配成员" agree with "添加项目成员" and with the Users page, closing the acceptance item "成员分配和『用户与权限』中都能找到".

### D6 — Legacy `GET /users` picker semantics unchanged (backend, guarded by tests)

`list_users` keeps: `role=annotator|reviewer&status=active` → full candidates; otherwise strict scope for project admins. `useUsers()` without role is relied on by the delete-transfer receiver list ("转交给（同项目启用用户）", validated server-side against managed projects) and by Audit/Dashboard pickers — widening it would leak unmanaged users into the transfer dropdown and other pickers. Only `Step6Members` changes how it queries (D5).

## Resulting visibility matrix (project admin actor)

Visibility (all rows below as of Revision 1; unchanged in Revision 2):

| Population                                             | Before | After | Rationale                                                                    |
| ------------------------------------------------------ | ------ | ----- | ---------------------------------------------------------------------------- |
| Self                                                   | ✅     | ✅    | unchanged                                                                    |
| Members of own projects (any role/status)              | ✅     | ✅    | unchanged                                                                    |
| Enabled annotator/reviewer, no project                 | ❌     | ✅    | **the fix** — matches assign modal                                           |
| Enabled annotator/reviewer of a foreign project        | ❌     | ✅    | same population the picker already exposes                                   |
| Enabled super admins                                   | ❌     | ✅ 👁 | maintainer request: read-only visibility (contact/lookup); all writes denied |
| Deactivated super admin (outside own projects)         | ❌     | ❌    | no expansion to disabled accounts                                            |
| Disabled accounts outside own projects                 | ❌     | ❌    | issue: no expansion to disabled                                              |
| Other project admins / viewers                         | ❌     | ❌    | issue: no expansion to admins/viewers                                        |
| Any of the above, filtered by a **foreign** project id | ❌     | ❌    | project member lists stay project-bounded                                    |

Manage scope (Revision 2):

| Target                                                | Account writes (role switch / reset / group / edit) | Lifecycle writes (offboarding / deactivate / delete) |
| ----------------------------------------------------- | --------------------------------------------------- | ---------------------------------------------------- |
| Own-project members (incl. straddling)                | ✅                                                  | straddling → 403 (super admin handles), else ✅      |
| Enabled annotator/reviewer, unassigned                | ✅ (**Revision 2**, was 👁 in Revision 1)           | ✅ (**Revision 2**)                                  |
| Enabled annotator/reviewer in foreign project(s) only | ✅ (**Revision 2**)                                 | 403 — handover needs the project owner / super admin |
| Enabled super admin                                   | ❌ (role matrix + level checks)                     | ❌                                                   |
| Viewer / other project admin / deactivated accounts   | ❌ (invisible)                                      | ❌                                                   |

Direct API writes cannot escalate: per-operation role matrix (annotator ↔ reviewer only, lower-level password reset, assignable-role deactivate/delete) still applies on top of the manage scope.

## Work breakdown

1. **Backend scope split** — `apps/api/app/services/management.py`: add `user_visibility_clause`, rewire `build_user_query`; docstrings updated to describe read vs manage scope (the current docstring claims one strict rule for everything).
2. **`is_managed` flag** — `app/schemas/management.py`, `app/api/v1/users.py::query_users`; regenerate OpenAPI snapshot + web generated types (`pnpm openapi:export && pnpm codegen`).
3. **Frontend** — `UsersPage.tsx` button gating (D4), `api/users.ts` type extension, `Step6Members.tsx` candidate queries (D5).
4. **Tests** — see plan below.
5. **Docs** — `docs-site/user-guide/superadmin/user-management.md` (列表与筛选 + 编辑用户 sections: what project admins can see vs manage — enabled annotators/reviewers incl. unassigned, and enabled super admins read-only; row actions apply only to own-project members; deactivated accounts remain out of scope), `CHANGELOG.md` Unreleased → `Fixed`.
6. **Live validation** — real browser pass per the repo's agent-browser workflow, after tests pass (below).

## Test plan

New backend file `apps/api/tests/test_users_visibility_scope.py` (factory/httpx fixtures per `test_management_api.py` conventions), disposable test DB per `tests/conftest.py`:

- project_admin `/users/query` default active: sees self, own members, **unassigned active annotator + reviewer**, **enabled super admin**; does **not** see unassigned viewer, second project_admin, inactive annotator, **deactivated super admin**.
- `status=inactive`: own inactive member visible, unassigned inactive annotator and inactive super admin hidden.
- `role=super_admin`: enabled super admins listed for project_admin (read-only lookup); `role=viewer`: only own members with that role.
- `project_id=<own>` → own members only; `project_id=<foreign>` → empty (no leak).
- `search=<unassigned annotator name>` → found (exact issue repro).
- `/users/stats` and `/users/export?format=json` totals equal `/users/query` under the same filters (three-way consistency).
- `is_managed`: `false` for unassigned annotator and for the super-admin row, `true` for own member; when the actor **is** a super admin, every row is `true`.
- Write guards on an unassigned annotator as project_admin: `PATCH /role` → 403, `POST /admin-reset-password` → 403, `POST /deactivate` → 403, `DELETE` → 403, `POST /groups/bulk` item → error, `GET /role/preview` → 404 without leaking email/name (mirrors `test_management_consistency.py`).
- Write guards on a super admin as project_admin: `PATCH /role` → 403 (assignable-role check), `POST /admin-reset-password` → 403 (role-level check), `POST /deactivate` → 403, `DELETE` → 403, `GET /role/preview` → 404 without leaking email/name.
- super_admin unchanged: full list incl. everyone (one assertion; deep behavior already covered elsewhere).
- `GET /users` picker regression: `role=annotator&status=active` includes the unassigned annotator; no-role `status=active` stays strict for project_admin.

Update existing tests that encode the old strict semantics:

- `test_management_filter_contract.py::test_users_query_stats_and_export_keep_the_same_filtered_scope` — its `outside` user is an **active annotator**, so after D1 it becomes visible and the `total == 1` assertion breaks. Rework: keep the three-way-consistency assertions, make the excluded population a viewer and an inactive annotator, and add an explicit "active unassigned annotator included" case.
- `test_management_api.py::test_management_user_query_stats_export_share_scope_and_filters` — uses `search=Managed`, unaffected; verify still green.
- `test_management_consistency.py::test_management_previews_do_not_reveal_out_of_scope_users` — write previews keep manage scope, unaffected; verify still green.

Frontend (vitest, follow existing harnesses):

- `apps/web/src/pages/Users/UsersPage.test.tsx`: render as `project_admin`; row with `is_managed: false` shows disabled action buttons with explanatory title; super-admin row shows the disabled (view-only) action button; row with `is_managed: true` (own member) keeps enabled buttons; super_admin actor path unaffected.
- `apps/web/src/components/projects/CreateProjectWizard.test.tsx` (covers Step6): candidate list merges annotator + reviewer role queries (mock `useUsers` asserting the role params), dedupes, and excludes other roles.

Verification commands (after implementation):

- API: `cd apps/api && uv run pytest tests/test_users_visibility_scope.py tests/test_management_api.py tests/test_management_filter_contract.py tests/test_management_consistency.py tests/test_users_role_matrix.py tests/test_openapi_contract.py` (against `TEST_DATABASE_URL`-configured disposable database).
- Web: `pnpm --filter @anno/web lint && pnpm --filter @anno/web typecheck && pnpm --filter @anno/web test` (targeted files first, then full suite), `pnpm openapi:check`.
- Repo: `pnpm lint:python`, `pnpm format:check`, `git diff --check`.
- Live browser (agent-browser per repo skill; local dev stack): seed one project admin + enabled unassigned annotator; verify (a) wizard step 6 lists the annotator, (b) "添加项目成员" lists the annotator, (c) Users & Permissions finds the annotator via search, (d) its row actions render disabled with the reason, (e) the enabled super admin appears in Users & Permissions for the project admin with view-only row actions, (f) direct `PATCH /users/{id}/role` as project admin returns 403 (annotator and super-admin targets).

## Acceptance mapping (issue #115 + maintainer addition)

- Both entry points find enabled unassigned annotators/reviewers — D1 + D5 (+ existing picker).
- Project admins can see enabled super admins in Users & Permissions but cannot operate on them (maintainer addition, 2026-09-16) — D1 widened arm + D2/D4 disabled actions + D3 write guards.
- Search / role filter / pagination / stats use one visibility range; export path checked, fields and permission constraints intact — D1 shared `build_user_query`; export assertions in tests.
- No-management accounts have clear frontend state; direct write calls cannot escalate — D2 + D4 + write-guard tests.
- Project member / performance / task scopes unchanged — `project_id` branch kept strict; no changes outside user management.
- Regression tests incl. unassigned users, other-project users, management permissions, super-admin behavior — test plan above.
- Real-browser validation of both entries + user-guide and CHANGELOG updates — verification and docs sections.

## Risks and mitigations

- **Counts jump for project admins** ("成员" tab, "团队成员" card, export row counts): expected consequence of the fix; called out in docs/CHANGELOG.
- **Query cost** of the widened clause: `or_(…, role IN (…) AND is_active)` on the users table; user tables are small and the clause replaces, not adds, a full-table filter shape for the same endpoints. No index work anticipated.
- **Contract churn**: `UserPage.items` element gains one optional boolean; additive — regenerate artifacts so `test_openapi_contract.py` and generated types stay aligned.
- **UX regressions in other `useUsers()` consumers**: avoided by D6 (picker semantics untouched; only the wizard changes how it calls it).
- **Future drift between the two clauses**: mitigated by naming + docstrings in `management.py` and by tests asserting read vs manage divergence explicitly.

## Outcome

- Landed commits: `fix(users): unify project-admin person visibility across entry points (#115)` plus the Revision 2 manage-scope patch on `feat/issue-115` (see `git log`)
- Release milestone: Not yet determined
- User documentation: `docs-site/user-guide/superadmin/user-management.md` (可见范围 + 标注员/质检员可操作说明)
- Developer documentation: schema/contract change captured in `apps/api/openapi.snapshot.json` (`UserPageItem.is_managed`); no ADR needed (no architectural decision beyond the documented read/manage scope split in `app/services/management.py` docstrings)
- CHANGELOG: Unreleased `Fixed` entry (updated for Revision 2)
- Tests: `apps/api/tests/test_users_visibility_scope.py` (visibility matrix, is_managed flags incl. foreign rows, account writes allowed on unassigned + foreign targets, lifecycle gates, offboarding on unassigned worker, super-admin guards), reworked `test_management_filter_contract.py`, `test_management_consistency.py` (out-of-scope population switched to viewer), `UsersPage.test.tsx` project-admin case, new `Step6Members.test.tsx`
- Verification (Revision 2): backend pytest green on the worktree test DB (visibility scope, filter contract, consistency, role matrix, management api, lifecycle, delete transfer, GDPR, OpenAPI contract); web vitest / lint / typecheck green; live browser re-acceptance on the worktree dev stack confirmed operable rows and working writes end-to-end
- Remaining work: none for this issue; the project-driven identity refactor for annotator/reviewer is a planned follow-up by the maintainer
