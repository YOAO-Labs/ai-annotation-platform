# Documentation reading and navigation redesign

> Status: implemented and locally verified; the maintainer requested a local implementation snapshot.
> Created: 2026-09-12.
> Scope confirmed by the maintainer: a substantial visual redesign of both documentation content and navigation.

## Recommendation

Create a cohesive technical handbook: a compact navigation frame, a clear article opening, and layouts appropriate to reading, choosing a task, or looking up a contract. Keep the existing royal-blue, paper-white, and ink-black identity, but substantially change column proportions, hierarchy, navigation density, and content sequencing.

Build on the installed VitePress default theme. Use its configuration, CSS tokens, Markdown extensions, and documented layout slots. Keep the four documentation domains and canonical URLs. All documentation pages receive the shared visual treatment; four domain hubs and six representative articles receive deliberate content reorganization in this delivery.

The minimal option is a typography/spacing pass in `docs-theme.css`. It is insufficient for the confirmed scope: it leaves narrow hub cards, duplicated entry lists, buried basic workflows, and inconsistent navigation intact. Replacing the whole theme is also unnecessary; it would duplicate routing, search, mobile navigation, and outline behavior already present.

## Evidence from the current checkout

The worktree was clean before this planning task. The local preview runs VitePress 1.6.4. Desktop and mobile observations below came from `http://127.0.0.1:5173/`, not from the deployed site.

| Observation                                                                                                                                            | Evidence                                                                                                               | Design consequence                                                                                                                         |
| ------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Navigation coverage already passes for 167 rendered pages.                                                                                             | `node docs-site/scripts/check-navigation-coverage.mjs --strict`; generated/mirrored domains have their own exclusions. | Preserve reachability while improving the hierarchy. Do not treat a visual redesign as a directory migration.                              |
| The article measures 688 px at a 1440 × 960 viewport, despite a 748 px custom rule.                                                                    | Live `Polygon` page geometry; `docs-theme.css` versus the more specific scoped rule in VitePress `VPDoc.vue`.          | Correct the whole column geometry and verify computed width, rather than adding another ineffective max-width declaration.                 |
| User-guide hub cards use three columns inside the article measure, and the outline contains only two entries.                                          | `user-guide/index.md`; desktop light/dark screenshots.                                                                 | Give hubs a wider canvas without the desktop article outline. Size the grid from available content width.                                  |
| Role selection precedes two large screenshots and five task-link tables.                                                                               | `user-guide/index.md`.                                                                                                 | Put common tasks first; put role routes second; remove decorative screenshots from this routing page while keeping the assets.             |
| Polygon starts immediately with operations, then mixes basic drawing, continuous creation, sampling limits, and cancellation before its demonstration. | `user-guide/workbench/polygon.md`; live page.                                                                          | Add a short outcome/starting-condition introduction and separate the normal workflow from advanced operations and limits.                  |
| The video guide opens with both a screenshot and video, and segmented collaboration comes before basic operations.                                     | `user-guide/workbench/video-track.md`.                                                                                 | Explain the task before media; put a basic workflow before advanced collaboration.                                                         |
| API entry paths follow a large interactive reference frame.                                                                                            | `api/index.md`.                                                                                                        | Lead with authentication, a first request, and resource/task routes; retain the interactive reference below them and its full-screen link. |
| The ML Backend reference has 1,814 lines; long outline titles are truncated and architecture detail precedes endpoint lookup.                          | `dev/reference/ml-backend-protocol.md`; live reference screenshot.                                                     | Put the contract map early, group secondary headings coherently, and let outline text wrap. Preserve wire details and deep links.          |
| Navigation mixes Chinese with `Search`, `Menu`, `On this page`, `Next page`, and `How-to`.                                                             | Mobile/desktop inspection and `navigation/dev.ts`.                                                                     | Localize available default-theme labels and use consistent task-oriented Chinese navigation names.                                         |
| The default sidebar already opens the active branch.                                                                                                   | Installed `theme-default/composables/sidebar.js`.                                                                      | Reuse it; do not add a second expansion store or route watcher.                                                                            |

Baseline screenshots are local review evidence, not repository media assets:

- `/tmp/docs-redesign-current-hub-light.png` — 1440 × 960, light hub.
- `/tmp/docs-redesign-current-hub.png` — 1280 × 576, dark hub.
- `/tmp/docs-redesign-current-polygon.png` — 1440 × 960, light task article.
- `/tmp/docs-redesign-current-reference.png` — 1440 × 960, light protocol reference.
- `/tmp/docs-redesign-current-mobile.png` — 390 × 844, mobile hub.
- `/tmp/docs-redesign-current-mobile-nav-settled.png` — mobile sidebar open.

The inspected browser session reported no page errors. The preview emitted existing syntax-highlighting fallbacks for `promql` and `env`. No production build or post-redesign browser acceptance has been performed during planning.

## Boundaries and existing decisions

- Follow [ADR-0016](../../adr/archive/0016-docs-ia-redesign.md): logical navigation may change independently of file ownership. Keep current public routes, canonical sources, and existing rewrites.
- Follow the current [documentation design tokens](../../../docs-site/dev/reference/design-system.md#documentation-site-tokens-vitepress): local font stacks, `html.dark`, `--docs-*` reading tokens, and royal blue as the single reading-layer brand accent. Keep acid green and full-bleed brand sections on the marketing homepage.
- Reuse the task/reference/concept/runbook distinctions already documented in [AI documentation layering](../2026-07-11-ai-documentation-layering-governance-draft.md). This proposal does not introduce another taxonomy or metadata schema.
- Keep the root marketing composition, application UI, public API/schema, database, content generators, search provider, and release versions outside this redesign.
- Preserve screenshots, video sources, manifests, editable diagrams, and the isolated Scalar reference page. This task changes placement and presentation, not media capture or provenance approval.
- Do not copy all 167 pages into a new format. Shared styling is site-wide; manual reorganization is explicitly limited to the ten pages below. The resulting authoring guide is the convention for subsequent ordinary documentation edits.
- No new package, service, account, credential, runtime font request, reader preference setting, or authentication-dependent navigation is required.

No repository hard rule conflicts with this approach. Navigation is reader guidance, not an access-control mechanism.

## Visual specification

### Shared frame

| Area           | Proposed treatment                                                                                                                                                                                                                                                                                                                                                                   |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Top navigation | Keep a 64 px bar. Use the four clear domain labels `使用指南`, `开发文档`, `API`, `部署运维`; keep secondary history links under `更多`. Give search a useful desktop field width and localized prompt. Make `快速开始` a quiet text action on reading pages; preserve homepage CTA styling with an explicit page scope.                                                             |
| Left sidebar   | Target a 256 px desktop rail with a subtly tinted surface. Add the current domain label through the sidebar slot. Use 14 px links, approximately 32–36 px row height, restrained indentation, and 20–24 px gaps between groups. Remove the large divider-and-padding treatment between every group. The active item combines blue text, a pale blue background, and a narrow marker. |
| Article        | Target a maximum reading measure of 760 px, with 32–48 px gutters when space permits. Use a flat page canvas, without wrapping the whole document in a card. Verify the computed result at 1440 px rather than assuming the custom rule wins.                                                                                                                                        |
| Right outline  | Target 192–208 px. Label it `本页内容`; distinguish heading levels with indentation and weight. Allow long names to wrap instead of ellipsis. Use the native active-section indicator and scroll behavior.                                                                                                                                                                           |
| Hub            | Keep the sidebar, disable the article outline, and allow up to 1040 px of main content. Use at most three card columns; a card should have at least 260 px available. Collapse to two or one column based on content width.                                                                                                                                                          |
| Mobile         | Retain the default navigation drawer and local outline control. Label them `文档目录` and `本页内容`; use 20–24 px article padding, single-column entries, and comfortable touch targets. Preserve keyboard dismissal, focus behavior, and active-page indication.                                                                                                                   |

Responsive behavior stays aligned with the existing theme: desktop sidebar from 960 px, desktop outline from 1280 px. Below 960 px use the drawer; between 960 and 1279 px use a two-column sidebar/article view. Test 390, 768, 1024, 1280, and 1440 px widths.

Conceptual reading-page arrangement:

```text
┌────────────────────────────────────────────────────────────────────────┐
│ Brand     Search documentation      Guide · Dev · API · Ops    Theme   │
├─────────────────┬────────────────────────────────────┬─────────────────┤
│ USER GUIDE      │ User guide / How-to                │ ON THIS PAGE    │
│                 │                                    │                 │
│ Get started     │ Polygon annotation                 │ Before starting │
│ Prepare data    │ What this page helps you finish    │ Draw and save   │
│ Projects/tasks  │                                    │ Edit a shape    │
│ Workbench       │ Before starting                    │ Advanced tasks  │
│   Image         │ 1. Draw → 2. Close → 3. Save        │ Limits          │
│   ▌ Polygon     │ Demonstration + useful caption     │                 │
│   Video         │                                    │                 │
│ AI assistance   │ Advanced tasks / visible warnings  │                 │
│ Quality         │ Related next steps                 │                 │
└─────────────────┴────────────────────────────────────┴─────────────────┘
```

This is a structural wireframe, not a rendered mockup. The product labels remain Chinese.

### Typography and content surfaces

- Keep the existing local font stacks. Use the display serif for H1 and hub section titles only; use sans-serif for article H2/H3 and navigation to improve mixed Chinese/English scanning.
- Desktop H1: 40 px / 1.2; mobile H1: 32 px / 1.25. Article H2: 26 px / 1.4; H3: 20 px / 1.5. Body: 16 px / approximately 1.8; secondary navigation/captions: 13–14 px. These are design targets, to be accepted against real pages rather than treated as accessibility limits.
- Use larger space before a section than between its title and text. Avoid a horizontal rule before every article H2. Retire automatic decorative H2 numbering on hubs; keep actual procedure numbering in the content.
- Reuse `DocLinkCard` for choices. Restyle it with a quieter border, shorter padding, clear title/description alignment, and color-only hover/focus changes. Remove emoji from the four redesigned hubs instead of introducing an icon package.
- Use native ordered lists for steps. Separate each operation from its expected result in prose. Use existing Markdown containers for notes and warnings; reserve warnings for a consequence that changes the user's next action.
- Use VitePress code groups only for genuinely alternative commands. Keep copy buttons, language labels, and horizontal code scrolling. Do not hide mandatory steps or warnings in tabs or disclosures.
- Keep parameter matrices as tables. Give them readable padding, differentiated headers, and local horizontal scrolling on small screens. Never allow the entire page to overflow horizontally.
- Keep existing video/diagram/lightbox behavior. Place media next to the operation it explains and use captions to say what to observe. Put redundant theme/device examples and long optional examples in native details blocks when they interrupt the primary workflow. Required limitations stay visible.
- Keep blue for links, selection, and focus; retain existing semantic warning colors. Dark mode uses the existing ink surfaces and lifted blue. No gradients, hover lifts, scroll animations, or decorative textures are needed for the reading layer.

## Navigation organization

The existing files in `.vitepress/navigation/` remain the source. No parallel route manifest or custom navigation store is added.

| Domain   | Ordered groups and concrete changes                                                                                                                                                                                                                                                                                      |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 使用指南 | `开始使用` → `数据准备` → `项目与任务` → `标注工作台` → `AI 辅助` → `审核与交付` → `平台管理` → `场景方案与支持`. Keep image/video/point-cloud subgroups. Move export formats under delivery, and FAQ/notifications/account settings into support. Keep all existing targets reachable.                                  |
| 开发文档 | `开始开发` → `操作指南` → `系统架构` → `SDK 与 CLI` → `协议与规范` → `测试与发布` → `故障排查` → `架构决策`. Put local development and first contribution directly in the first group. Move testing/release out of the opening three links. Retain architecture subgroups but collapse inactive branches.                |
| API      | `开始接入` → `资源与标注` → `异步任务` → `模型与集成` → `完整参考`. Group auth first; projects/tasks/predictions together; async/video-tracker jobs together; backend/import/websocket/export/storage/system-settings together; retain the generated route index. Add the existing system-settings guide to the sidebar. |
| 部署运维 | `部署环境` → `升级维护` → `监控与安全` → `故障处理`. Put the overview and deployment options together, upgrade in its own group, combine monitoring/security links, and keep every existing runbook.                                                                                                                     |

Rules:

- Move the existing ML Backend starter tutorial into `开始开发`, preserving `/dev/ml-backend/starter` and its existing rewrite; keep the protocol contract under `协议与规范`.
- Keep the introductory group available; other large groups are collapsible. The default theme opens active branches. Do not impose a one-open-group accordion or persist extra state.
- Keep intentional cross-links such as AI assistance → an existing workbench page. Do not duplicate the article to make navigation look like the filesystem.
- Keep the title above an article simple: domain link plus existing document type. Do not calculate a full breadcrumb from duplicated sidebar entries.
- Reuse `audience` and `type` where helpful; missing optional metadata must omit the label without leaving an empty panel. Do not render `since` or show repository provenance as a reader badge.
- Continue using native local search, including its keyboard shortcut and empty-result state. Localize its supported strings; do not promise role filtering or an AI assistant.

## Content structures and exact migration set

| Page                                   | Revised sequence                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `user-guide/index.md`                  | Brief purpose → six common tasks (`领取并完成任务`, `导入数据`, `创建与分配项目`, `使用 AI 辅助`, `审核标注`, `导出结果`) → role routes → compact support links. Every card points to an existing canonical page. Remove the two overview screenshots and task tables from this hub's rendering.                                                                                                                                                                  |
| `dev/index.md`                         | Purpose → start locally / first contribution → task-oriented entry cards → architecture/reference links → concise repository map. Replace the inline setup recipe with the canonical local-dev link to avoid competing setup instructions.                                                                                                                                                                                                                        |
| `api/index.md`                         | Purpose and URL/auth essentials → first-integration path and task groups → OpenAPI download/full-screen reference link → existing interactive frame → local live-doc/codegen references. Preserve existing public anchors.                                                                                                                                                                                                                                        |
| `ops/index.md`                         | Choose deployment environment → upgrade and health checks → symptom-based runbook links → security/monitoring references. Include the existing LAN deployment and all runbooks currently present in navigation.                                                                                                                                                                                                                                                   |
| `user-guide/getting-started.md`        | Goal/role prerequisites → sign in → find the assigned task → complete and save an annotation → submit/check status → next task or role-specific route. Keep one primary login illustration inline; group dark/mobile/password-recovery examples beside the relevant step in optional details.                                                                                                                                                                     |
| `user-guide/workbench/polygon.md`      | Outcome/prerequisites → draw, close, choose class, verify save → demonstration → edit vertices → advanced actions (sampling, boundary reuse, merge/subtract/split) → shortcuts → limits/recovery → related tasks. Preserve every existing editing, cancellation, online-save, and undo constraint.                                                                                                                                                                |
| `user-guide/workbench/video-track.md`  | Scope and related playback/AI pages → minimal first-track workflow → one primary demonstration → frame-versus-track behavior → editing/attributes → segmented collaboration → candidate review → quality/export/limits. Keep draft-protection conditions beside the operations they affect.                                                                                                                                                                       |
| `dev/reference/ml-backend-protocol.md` | Contract purpose and endpoint map → authentication → existing health/predict/result/setup contracts → optional lifecycle endpoints → errors/compatibility → examples and implementation references. Move the current architecture introduction after the endpoint map or into a clearly linked explanatory section within the same page. Relevel secondary capability headings; preserve every wire field and old anchor. Do not split the page in this delivery. |
| `dev/concepts/overview.md`             | What the architecture explains → existing physical diagram → a short browser/API/worker data-flow walkthrough → logical layers → supporting components and deeper references. Keep the editable diagram source and existing layer owners.                                                                                                                                                                                                                         |
| `ops/runbooks/celery-worker-stuck.md`  | Symptoms → identify the affected service/queue → read-only diagnosis → choose the matching existing recovery branch → verify the intended worker and task progress → prevention/escalation references. Visually distinguish inspection from recovery commands; do not execute them as documentation-layout acceptance.                                                                                                                                            |

These templates are authoring structures, not new Vue components. Tutorials/how-to pages lead with an outcome and procedure; references lead with the lookup map; explanations lead with relationships; runbooks lead with symptoms and verification. Reuse the stronger parts of existing pages rather than mechanically giving every article the same headings.

The six primary user-guide cards target, in the order above: `/user-guide/getting-started`, `/user-guide/datasets/import-images`, `/user-guide/workflows/new-project-end-to-end`, `/user-guide/ai/`, `/user-guide/review/`, and `/user-guide/reference/export-formats`.

Move the hub's existing `role-dashboard-overview.png` and `platform-nav-overview.png` references into a secondary interface-orientation details block in `getting-started.md`. Retain `video-real-scene.png` in the video guide's interface explanation below its first workflow. These images currently have only one published reference each; placement changes must not create orphan assets or silently invalidate media review records.

## Implementation sequence

This is a change across more than eight files, approximately 24–28 files including documentation and a small browser check. Each phase is independently mergeable and leaves a usable site. Indicative effort is four to six focused engineering days including visual acceptance; this is an estimate, not a release commitment.

### Phase 1 — Shared reading frame and navigation

Targets:

- `.vitepress/theme/docs-theme.css`: consolidate the reading layout, typography, navigation, outline, content-surface, responsive, and accessibility styles.
- `.vitepress/theme/docs-home.css`: only scope the existing homepage CTA behavior so shared navigation changes preserve the landing page.
- `.vitepress/theme/index.ts` and new `.vitepress/theme/DocsLayout.vue`: wrap `DefaultTheme.Layout`, use `doc-before` for the domain/type context and `sidebar-nav-before` for the domain label. Keep the H1 and lead paragraph in Markdown; do not duplicate them in the wrapper.
- `.vitepress/config.ts` and the five `.vitepress/navigation/*.ts` files: localize labels, apply the agreed groups, and retain native sidebar/search/outline behavior.
- New `docs-site/scripts/check-reading-layout.mjs`: one focused smoke check using Node's built-ins and the already installed `agent-browser` CLI against a supplied preview URL. Cover domain labels, missing metadata, active navigation, widths, and narrow-screen overflow. No new test framework or package script is required.
- `docs-site/dev/reference/design-system.md` and `CHANGELOG.md`: document the final reading-layer contract and user-visible navigation/reading changes.

Use public theme slots; do not alias or copy private VitePress components. Give new CSS enough specificity to beat the observed scoped default rule, and keep overrides under reading-page selectors. Continue rendering existing pages when metadata is absent or when the route belongs to generated history/ADR sections.

The smoke check uses positional arguments: mode (`capture` or `verify`), preview URL including any deployment prefix, and baseline JSON path. It uses its own named browser session and closes that session on completion; it does not start or stop an application runtime. Implement this check first so the unmodified pages can be captured before the theme or content changes.

- Before changing pages: `rtk proxy node docs-site/scripts/check-reading-layout.mjs capture http://127.0.0.1:5173/ /tmp/docs-redesign-anchors.before.json`.
- After changes: `rtk proxy node docs-site/scripts/check-reading-layout.mjs verify http://127.0.0.1:5173/ /tmp/docs-redesign-anchors.before.json`.
- Capture records each of the ten migration routes and its rendered article heading/explicit IDs. Verify requires every previous ID to exist on the same route, rejects duplicate article IDs, and runs the layout/navigation checks. Exit nonzero for missing/unreadable baselines, missing IDs, inaccessible routes, or failed assertions; never overwrite the baseline in verify mode.
- When preview chooses another port, use the URL actually printed. Repeat verification against the subpath preview with the same route-relative baseline. Keep the before/after evidence with implementation acceptance artifacts; if lost, regenerate the baseline from the pre-change revision rather than the modified content.

Acceptance: the new frame works with unmodified Markdown, old URLs, local search, generated history pages, light/dark mode, and mobile navigation. It must not depend on Phase 2 or 3 to look complete.

### Phase 2 — Four hubs and three task guides

Targets:

- `.vitepress/theme/components/DocLinkCard.vue`: update the existing card rather than adding another entry-card component.
- The four domain hubs and the three user-guide articles in the migration table.
- New `docs-site/dev/how-to/write-documentation.md`, linked from the developer sidebar: record page structures, heading/anchor rules, media placement, native containers/details/code groups, and canonical-source boundaries. Write this developer-facing authoring guidance in English.

Set `aside: false` on the four hubs and keep `pageClass: docs-hub-page` as the existing layout selector. Preserve old heading IDs explicitly when renaming or moving headings. Reuse current media components without changing their loading or playback implementation.

Acceptance: each hub exposes its first primary task in the initial mobile viewport; all six user-guide task entries are usable within the first desktop viewport. Each task guide shows its outcome and first step before large media or optional variants. Every previous substantive condition remains available.

### Phase 3 — Reference, explanation, and runbook examples

Targets: the final three articles in the migration table; refine the shared CSS only when these actual content types reveal a gap. Complete the authoring guide and plan outcome with the final verification evidence.

Group the protocol outline into navigable sections without adding tabs for whole endpoint contracts. Keep required errors and compatibility rules visible. Do not change command semantics, wire contracts, or runtime ownership just to simplify prose; factual corrections discovered during editing require checking their actual source and should be explicitly reported.

Acceptance: a reader can find health/predict/setup requirements from the protocol opening, understand the system diagram before directory listings, and identify a runbook's diagnostic versus recovery steps. Existing deep links still land on the corresponding content.

## Verification and completion

Before implementation, read back the current worktree diff and capture the affected pages' existing heading IDs. Preserve renamed IDs with VitePress custom anchors; where one section is regrouped, retain additional legacy anchors near the corresponding content. Review internal references as well as directly opening selected old URLs.

Run these checks after the affected changes:

1. `rtk proxy pnpm exec prettier --check` with the explicit changed files.
2. `rtk proxy node docs-site/scripts/check-navigation-coverage.mjs --strict`.
3. `rtk proxy pnpm --filter @anno/docs-site check:frontmatter`.
4. `rtk proxy pnpm --filter @anno/docs-site check:codegen` after preview/build regeneration; no generated source drift is expected.
5. `rtk proxy pnpm docs:build` for SSR, links, snippets, and diagram validation. Keep unrelated pre-existing failures distinguishable from this diff.
6. Run the focused reading-layout smoke check against the printed local preview URL. Also build/preview with `DOCS_BASE=/ai-annotation-platform/` and check domain links, cards, assets, and the Scalar reference link under that prefix.
7. `rtk git diff --check` and final diff review.

After moving media references, also run `rtk proxy node docs-site/scripts/check-orphan-images.mjs --strict` and `rtk proxy pnpm --filter @anno/docs-site media:audit -- --strict`. Investigate any changed review hashes through the existing media workflow; a layout edit does not itself constitute a media approval.

Manual browser matrix:

- Four hubs; Polygon/video task pages; the protocol reference; the architecture diagram; the runbook; plus marketing home, a generated ADR, changelog, and the API frame as regression controls.
- 390, 768, 1024, 1280, and 1440 px widths. Check desktop light/dark and mobile light/dark; inspect 200% text zoom and reduced motion.
- Domain navigation and back/forward; active branch on a deep-linked page; intentionally cross-linked AI pages; long sidebar/outline labels; keyboard search/open/no results/Escape; both mobile menu controls; focus visibility; skip-to-content; copy-code button; overflow tables; image/diagram zoom; video controls; previous/next links.
- Article H1 and every heading appear once; missing optional metadata produces no empty block; no whole-page horizontal overflow. A wide table or code block may scroll locally.
- Ordinary text meets 4.5:1 contrast and visible focus/large-text treatments meet the relevant 3:1 minimum. Check both themes against actual rendered backgrounds.
- No new console errors, failed local assets, hydration warnings, or third-party font requests. Treat the existing highlighting fallback warnings separately.

The biggest implementation risk is CSS interaction with the default theme and the marketing page. The most fragile design assumption is that the proposed column widths improve both Chinese instructions and code-heavy references. If a reference table still needs more width, preserve the reading measure and use its local overflow/full-screen reference route rather than widening every paragraph or replacing the theme.

Rollback is a revert of the relevant independent phase commits; no database or content-source migration is involved. Keep media assets intact and existing public URLs stable. The authoring guide is the sole additive documentation route. Update this plan with `## Outcome` only when implementation actually lands. Commits, pushes, publishing, and releases remain separate follow-through actions and are not performed by this planning task.

## Framework references checked during planning

- [VitePress 1.6.4: extending the default theme](https://vuejs.github.io/vitepress/v1/guide/extending-default-theme) — CSS overrides and supported layout slots fit the proposed wrapper.
- [VitePress 1.6.4: sidebar configuration](https://vuejs.github.io/vitepress/v1/reference/default-theme-sidebar) — domain sidebars and collapsible groups support the navigation plan.
- Installed VitePress component/composable source was checked for column widths, the active-branch behavior, and available localized labels; no framework upgrade is part of this plan.

## Outcome

- Implemented the shared reading frame, localized task navigation, wide domain hubs, and all ten content reorganizations in the originating worktree. The originating branch HEAD remains `daad9ee1871bf646c43de59abb3b74130bb47ff4`; no originating-branch commit, push, release, or publication was performed.
- Reused the default theme and existing media/cards. Added only `DocsLayout.vue`, the focused browser checker, and the authoring guide. No package, API, database, content-generator, or runtime-service change was needed.
- Worker commits `ac7fa92fb20094e07f850190694c38fad6a91cab`, `13a16fc6f10cdc4f02648c9dd2a30799a5a0d6d8`, and `5b7e0a76a5330b5a6b1867d7bd432213a810a4cf` were integrated without committing and reviewed/refined in the originating worktree. Their three clean Orca worktrees and idle terminals were removed; the unmerged worker branches remain as recovery copies.
- User documentation: `docs-site/user-guide/index.md`, `getting-started.md`, `workbench/polygon.md`, and `workbench/video-track.md`.
- Developer/API/operations documentation: the three domain indexes, `dev/concepts/overview.md`, `dev/reference/ml-backend-protocol.md`, `ops/runbooks/celery-worker-stuck.md`, `dev/reference/design-system.md`, and new `dev/how-to/write-documentation.md`.
- Added the reader-visible change to `CHANGELOG.md` under Unreleased / Changed. No release milestone was assigned.

### Validation evidence

- Default production build passed. The final subpath production build passed in 17.24 seconds with `DOCS_BASE=/ai-annotation-platform/`.
- Navigation coverage: all 168 rendered pages reachable. Frontmatter: 165 checked files passed. Codegen: 112 hotkeys, 48 settings fields, and 415 routes across 65 modules matched their sources. All 62 image assets remain referenced, with no unapproved duplicate image detected.
- The browser checker passed against the final production preview at `http://localhost:4173/ai-annotation-platform/`: all 124 previous article IDs retained across the ten pages, no duplicate article IDs, native active branches, correct base-prefixed links, missing-metadata behavior, 1440/390 px light/dark layouts, and 768/1024/1280 px intermediate widths.
- The final checker also covers the homepage tablet menu and its text contrast in both themes. Browser inspection found the original 768 px header overflow and the homepage menu inheriting paper-colored text onto a pale surface; both are corrected using existing native navigation and local reading tokens.
- Independently exercised the mobile sidebar and Escape, local outline anchor navigation, local search including no-results and a keyboard-opened result, code-copy feedback, diagram/image zoom and Escape, and the existing video controls. A 720 × 480 CSS viewport, equivalent to the layout area of a 1440 × 960 window at 200% browser zoom, also retained local media sizing without horizontal page overflow.
- The API reference page and OpenAPI document returned HTTP 200 under the deployment prefix. All eight rendered image URLs on the quickstart page returned HTTP 200 using the bundled asset URLs.
- The architecture article's axe checks reported zero violations in light and dark mode. A full-page check still reports VitePress's existing nested interactive sidebar controls; this implementation does not claim a site-wide accessibility certification or replace private default-theme components. Media-caption checks that need manual interpretation remain distinct from automated contrast checks.
- A read-only source review found no concrete issues in the shared theme, layout wrapper, configuration, or browser checker. Prettier, Node syntax validation for the checker, and `git diff --check` passed.

Local evidence:

- Before-anchor capture: `/tmp/docs-redesign-anchors.before.json`.
- Production build logs: `/tmp/docs-redesign-build.log`, `/tmp/docs-redesign-build-final.log`.
- Final browser check: `/tmp/docs-redesign-reading-final.log`.
- Visual samples: `/tmp/docs-redesign-hub-after-light.png`, `/tmp/docs-redesign-hub-after-mobile.png`, `/tmp/docs-redesign-reference-after-light.png`, and `/tmp/docs-redesign-home-menu-fixed.png`.
- Media before/after reports: `/tmp/docs-redesign-media-before.json` and `/tmp/docs-redesign-media-after.json`.

### Remaining limitations

Strict media audit still fails on the same existing baseline: 182 stale assets, 1 review-due asset, 28 current assets, and 0 broken assets. Compared with the pre-change report, no asset hash, status, or newly changed watched path was introduced. No media review record was rewritten or represented as a new human approval.

Build output retains the existing `promql`/`env` highlighting fallbacks and a large-chunk warning. The preview server was restarted after rebuilding because its old asset map returned 404 for new hashed bundles; the final preview serves the new CSS and JavaScript successfully. Remote CI and deployed-site validation were not run.
