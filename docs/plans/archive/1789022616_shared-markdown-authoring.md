# Shared Markdown authoring and annotation guides

> Status: implemented and locally verified on 2026-09-10.
> Direction: visual editing with a Markdown source option. The user requested parallel implementation by GPT-5.6 Luna workers at max reasoning effort, with integration and acceptance in the main thread.

## Goal and recommendation

Make annotation guides easy to write without remembering Markdown syntax, and reuse the same authoring and reading components for BUG descriptions, BUG comments, and project-template guides. Keep Markdown strings as the persisted format.

Use `@mdxeditor/editor` behind the existing local `MarkdownEditor` entry point. Keep `react-markdown` and `remark-gfm` for a shared `MarkdownView`. All editor previews and published reading surfaces use that view; the visual editing surface shares its typography and supported syntax. Editors load only when needed.

The minimal option is to improve the existing CodeMirror toolbar and preview, then reuse them in BUG forms. It has the lowest integration risk but still asks users to write Markdown source. The recommended option adds a visual editor because direct table, list, link, and image editing better serves authors unfamiliar with Markdown. This assumption is material: if authors prefer source editing, choose the minimal option while retaining the shared renderer and business adapters described below.

## Evidence from the current repository

| Area                | Current behavior and consequence                                                                                                                                                                                                                                                                                                                                                                                                     |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Dependencies        | `apps/web/package.json` already includes React 18, CodeMirror 6, `react-markdown`, and `remark-gfm`. This is a React/Vite application.                                                                                                                                                                                                                                                                                               |
| Guide authoring     | `apps/web/src/components/markdown/MarkdownEditor.tsx` wraps CodeMirror with a custom toolbar, image upload, and blur callback. It is a source editor. Upload errors currently insert an HTML comment into the draft.                                                                                                                                                                                                                 |
| Guide persistence   | `apps/web/src/pages/Projects/sections/AnnotationGuideSection.tsx` owns the draft, saves on blur, and switches between edit and preview. `useGuideAssets.ts` owns project uploads and a signed-URL cache.                                                                                                                                                                                                                             |
| Duplicate readers   | `GuideMarkdownView.tsx` and `components/bugreport/MarkdownBlock.tsx` duplicate GFM/component configuration. The guide imports the BUG stylesheet. Heading and image layout rules are incomplete.                                                                                                                                                                                                                                     |
| Private images      | Markdown stores `guide-asset:KEY`. The current view extracts keys with a regex and displays failed resolutions as loading. Static inspection also shows that it does not override `react-markdown`'s default URL transform, whose installed implementation rejects this scheme before the image renderer receives it. Verify this path with a regression test during implementation; this session did not reproduce it in a browser. |
| Guide read progress | `utils/annotationGuide.ts` hashes the exact Markdown string. Formatting-only writes can invalidate guide-reading confirmations. Workbench and Dashboard must retain their current confirmation behavior.                                                                                                                                                                                                                             |
| BUG text            | `BugReportDrawer.tsx` and `pages/Bugs/BugsPage.tsx` use textareas for authoring and `MarkdownBlock` for reading. Description limit: 20,000 characters; comment limit: 10,000. Creation can append diagnostic Markdown to the submitted description.                                                                                                                                                                                  |
| BUG images          | Screenshots are independent report attachments: at most five PNG/JPEG/WebP files, 10 MiB each. The drawer handles paste at the form level. Attachment access requires a report association; its storage bucket expires objects after 180 days.                                                                                                                                                                                       |
| Other surfaces      | `ProjectTemplates/TemplateEditModal.tsx` has a guide textarea. Templates copy Markdown without guide assets. `Workbench/shell/AttributeForm.tsx` has another Markdown reader. Workbench comments have character-offset mentions, drawing attachments, and annotation/task anchors.                                                                                                                                                   |

The historical guide plan selected CodeMirror and a separate project asset API. This proposal changes the primary editing experience while preserving the Markdown storage and project asset decisions. Current repository rules introduce no blocker: use local adapters, semantic tokens, `data-theme`, and existing business owners.

## Library assessment

| Option                                | Fit                                                                                                                  | Decision                                                                                                     |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Existing CodeMirror + shared renderer | Lowest dependency and migration cost; excellent source editing                                                       | Minimal alternative if source editing is preferred                                                           |
| MDXEditor                             | React component with visual Markdown editing, tables, source mode, image upload/preview hooks, and translation hooks | Recommended for this task                                                                                    |
| Milkdown / Crepe                      | Markdown-focused visual editor built on ProseMirror and remark                                                       | Viable, but its framework/plugin integration offers no demonstrated advantage for these existing React forms |
| `@uiw/react-md-editor`                | Ready-made source editor with preview and toolbar, based on a textarea                                               | Considered; replacing the existing source editor gives less benefit than adding visual editing               |

Official documentation confirms the MDXEditor capabilities, but it does not prove compatibility with this application's content or browser flows. The integration must pass the acceptance checks below. Use a stable published package and commit its exact resolution in the lockfile; do not install the upstream development branch. No new service, paid account, API key, MCP server, CLI, or environment variable is required. Runtime assets are served with the application, not fetched from a public CDN.

## Shared boundaries

```text
Guide settings / template dialog / BUG forms
    | own draft, validation, submission and attachment policy
    v
MarkdownEditor  ---- Markdown string ----> existing APIs and fields
    | preview                                  |
    v                                          v
MarkdownView <------------------------ reading surfaces
    |
    +---- optional image resolver ----> project guide asset API

BUG screenshot queue ----------------> existing BUG attachment API
```

- `MarkdownEditor` keeps `value`, `onChange`, optional `onUploadImage`, `placeholder`, and a logical editor-exit callback. Add document identity, disabled state, an accessible label, a compact/document presentation, and an optional image resolver. Business IDs and API calls stay outside it.
- `MarkdownView` owns GFM rendering, typography, link policy, table overflow, and image presentation. Its inputs are Markdown content, presentation density, an optional image resolver, and the resolver's document scope. It does not import BUG-specific code or the editor bundle.
- Keep `GuideMarkdownView` and `MarkdownBlock` as thin compatibility adapters in the first phase. Move shared styles into `components/markdown/`; remove the guide-to-BUG CSS dependency.
- A resolver receives a stored image source and a refresh request and returns the display URL plus its expiry when applicable. Extend the existing guide hook for this metadata and forced refresh, retaining existing callers. Persist stable resource identifiers; signed URLs remain transient display data.
- Reuse local UI primitives and Lucide icons. Map editor colors and public popup classes to `--sc-*` tokens and semantic overlay layers. Use the application's existing `data-theme`; do not add a second theme state or `.dark` selector. Translate the enabled toolbar, dialog, and error strings into Chinese.

## Content and lifecycle decisions

1. **One supported format.** Support headings, emphasis, strikethrough, ordered/unordered/task lists, quotes, links, images, fenced code, and GFM tables. Both toolbar presentations must parse existing supported content, even when a compact toolbar hides an insertion command. Retain code-block language labels and fall back to plain code editing for unknown languages.
2. **No executable extensions.** Disable HTML processing in the visual editor and do not enable JSX, MDX execution, raw HTML rendering, arbitrary styles, or embeds. Disable image resizing and dimension controls because MDXEditor serializes dimensions as HTML. Unsupported existing syntax remains recoverable in source mode; never silently discard it or replace a parsing failure with an empty document.
3. **Preserve unchanged originals.** Initialization, external synchronization, focusing, previewing, and changing modes do not mark content dirty or save normalization output. Retain the original source and track actual user edits. Undoing to the initial content must restore the unchanged state. Do not batch-rewrite old guides. A real rich-text edit may normalize equivalent Markdown syntax when saved; preserve meaning and resource identifiers.
4. **Keep save ownership in each form.** Guide settings retain save-on-exit behavior, add an explicit Save action, and show unsaved/saving/saved/failed states with retry. Toolbars and editor popups are part of the editor focus boundary. Serialize guide saves; a stale response must not overwrite a newer draft. BUG submission remains explicit, with existing reopen behavior and keyboard shortcuts. External document replacement must not clobber a dirty draft.
5. **Protect asynchronous ownership.** Reset on explicit document identity changes. Uploads, URL resolution, and saves are scoped to that identity; late completions must not insert into another project/report or clear its state. Capture insertion anchors before uploads and preserve multi-image ordering. Upload errors appear outside the text with retry; they never become document content.
6. **Resolve actual image nodes.** Handle image nodes, including reference-style images, instead of scanning arbitrary Markdown with a regex. Permit `guide-asset:` only on image `src` with an authorized guide resolver; delegate ordinary URLs to the existing safe transform. Never use a blanket identity URL transform. Show distinct loading, unavailable, and retry states. Reuse per-project caching, deduplicate in-flight signing, and refresh an expired/failed URL once before offering manual retry. Validate expiry on retry and when the surface becomes visible again.
7. **Reading quality.** Apply explicit heading spacing, nested-list indentation, quote styling, responsive images, horizontally scrolling tables/code, and separate inline-code/block-code styles. Add code copy and an accessible image-enlargement dialog using existing primitives. Reuse the same body typography in visual editing and reading, with compact spacing for BUG comments and narrow guide panels.
8. **Retain screenshot ownership.** In BUG forms, uploaded screenshots remain in the existing attachment queue and list. Route file-image paste to that queue before the visual editor consumes it; one paste must create one attachment. Preserve ordinary text paste. Do not enable an inline upload command, store base64/blob URLs, or invent a BUG asset scheme in this iteration. Existing Markdown image URLs can still be read.
9. **Validate final submitted text.** Apply BUG limits to the full Markdown payload, including automatically appended diagnostics. Count characters consistently with the API. Over-limit errors preserve the draft and explain the remedy; never silently truncate diagnostic or user text.

## Independently mergeable delivery steps

### 1. Shared reading and guide image compatibility

Create `MarkdownView.tsx`, its shared stylesheet, and focused rendering/media tests. Adapt `GuideMarkdownView` and `MarkdownBlock`; update `useGuideAssets.ts` and its tests for refresh, deduplication, and project scope. All existing guide and BUG reading surfaces benefit while the existing editor remains usable. Add tests for the custom URL scheme, reference images, failures, expired URLs, and cross-project late responses.

### 2. Visual authoring for project and template guides

Integrate MDXEditor behind `MarkdownEditor.tsx`, with document/compact toolbar presentations, source mode, shared preview, Chinese labels, and local theme styles. Update `AnnotationGuideSection.tsx` and its tests for dirty state, explicit save, editor-exit saving, upload behavior, and failure recovery. Update `TemplateEditModal.tsx` to use the same editor with project uploads unavailable; retain the existing template asset warning. Add an empty-guide starter action for category definitions, boundary rules, positive/negative examples, and a review checklist; it inserts only on an explicit user action and does not replace existing text.

Retain Workbench/Dashboard read-confirmation state owners. Test that opening and leaving an unchanged guide causes no write and no new reading version. Keep heavy editor imports lazy and inspect the built dependency graph. Existing budgets in `.size-limit.json` are main 700 KB and vendor-markdown 150 KB; do not increase them merely to hide eager editor loading. Report the editor's separate generated chunk sizes.

### 3. BUG description and comment authoring

Replace BUG create/edit description fields and both BUG comment inputs with the compact editor. Preserve diagnostics, attachment capture, status/reopen semantics, submission limits, and Ctrl/Cmd+Enter in the management page. Update `BugReportDrawer.test.tsx` and `BugsPage.test.tsx`; add browser coverage for the real editor in the drawer and management page. This step reuses the established editor and renderer without changing BUG schemas or storage.

The complete scope exceeds eight files: shared components/styles/tests, three form areas, the guide hook, package metadata/lockfile, browser coverage, and documentation. Rough engineering estimate: 4–6 person-days for all three steps including integration checks; 1–2 person-days for the minimal source-editor option. These are planning estimates, not release commitments.

## Deferred scope

- Inline uploaded images in BUG Markdown require a separate design for report association, drafts, access, retention, and comment attachments. Do not use the temporary BUG bucket for durable project-guide images.
- Workbench comments retain their mention offsets, attachments, and canvas anchors. They need a dedicated format/mention migration design before adopting this editor.
- Attribute-description reading and workbench issue descriptions are later consumers of the public components; they are not required for these three delivery steps.
- Collaborative editing, document history/publishing approvals, PDF/Word import, AI writing, Mermaid/math rendering, and changes to the Vue/VitePress documentation renderer are outside this proposal.

## Acceptance and verification

| Path               | Acceptance                                                                                                                                                                                                                                                                                                                |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Normal authoring   | Chinese typing and IME composition, formatting, undo/redo, table editing, code, source switching, save and reopen preserve supported content.                                                                                                                                                                             |
| Existing documents | Nested lists, task lists, reference images, escaped punctuation, autolinks, code containing angle brackets/braces, and unsupported syntax preserve source or provide source recovery. Merely opening a guide never writes.                                                                                                |
| Save errors        | A failed save keeps the draft and offers retry. Fast edits during saving and project switches cannot cause stale writes to replace current state.                                                                                                                                                                         |
| Images             | Paste/drop/upload success and failure, multiple uploads, cursor movement, expiry, authorization failure, deleted assets, and switching documents behave predictably. Duplicate images do not duplicate signing requests.                                                                                                  |
| BUG workflows      | Create/edit/comment work from both entry points; diagnostics and complete-payload limits remain correct; attachments are neither swallowed nor duplicated; closed-report behavior and shortcuts remain intact.                                                                                                            |
| Reading            | Light/dark themes, a narrow guide panel, mobile-width BUG drawer, keyboard navigation, popup focus, image enlargement, and wide tables/code remain usable.                                                                                                                                                                |
| Performance        | Read-only guide/BUG views do not import MDXEditor/Lexical. On a documented test machine/browser, a 100 KB guide with 20 image references remains editable without repeated signing on keystrokes; target p95 input-to-next-paint below 100 ms over 50 edits. This is a proposed acceptance target, not a measured result. |

Run focused Vitest suites for `components/markdown`, `useGuideAssets`, `AnnotationGuideSection`, `BugReportDrawer`, `BugsPage`, and the existing guide-version/Dashboard/onboarding tests. Add real-browser cases in `apps/web/e2e/tests/markdown-authoring.spec.ts` for the flows above. Use route-mocked data for editor/browser behavior and a verified disposable environment for actual upload/API checks.

Required commands after implementation: `pnpm --filter @anno/web typecheck`, `pnpm --filter @anno/web lint`, `pnpm --filter @anno/web lint:css-tokens`, `pnpm --filter @anno/web build`, `pnpm --filter @anno/web size`, and `git diff --check`. Run the targeted browser suite with `pnpm --filter @anno/web exec playwright test e2e/tests/markdown-authoring.spec.ts --project=chromium` using the repository's actual configured server URL. Distinguish mocked browser results from real API/upload checks and remote CI.

Inspect symlink targets before installing dependencies: this checkout's Node dependencies and `.env` currently point to the primary checkout. Follow the runtime skill for an isolated dependency setup. Use the checkout's Python virtual environment if API checks become necessary. After every test run, remove only artifacts and disposable data created by that run, including uploaded objects, temporary fixtures, traces, reports, and build output; preserve pre-existing files and all unrelated work.

## Documentation, rollback, and completion

- With each implemented step, update relevant `docs-site/user-guide/projects/`, `docs-site/user-guide/workbench/index.md`, `docs-site/user-guide/superadmin/bug-management.md`, and the Unreleased changelog. Document the shared component/asset contract in `docs-site/dev/concepts/`. Do not describe proposed behavior as already shipped.
- No backend schema migration, new API, service, or storage move is planned. Reverting a frontend step retains readable Markdown and current image references. Reverting the editor does not restore the exact pre-edit whitespace of documents deliberately edited and saved; do not promise byte-for-byte rollback of those edits.
- Network failures retain local form drafts and expose existing save/upload retries. Unsupported content has a source-mode recovery path. The renderer remains independently usable if the authoring bundle fails to load.
- Implementation was authorized after the initial source and library review. The Outcome below records the completed local verification; release and publication remain separate maintainer decisions.

## Official references reviewed

- [MDXEditor getting started and Vite integration](https://mdxeditor.dev/editor/docs/getting-started)
- [MDXEditor source mode](https://mdxeditor.dev/editor/docs/diff-source), [tables](https://mdxeditor.dev/editor/docs/tables), and [code blocks](https://mdxeditor.dev/editor/docs/code-blocks)
- [MDXEditor image behavior](https://mdxeditor.dev/editor/docs/images) and [image plugin API](https://mdxeditor.dev/editor/api/functions/imagePlugin)
- [MDXEditor parsing failures and source recovery](https://mdxeditor.dev/editor/docs/error-handling)
- [MDXEditor themes](https://mdxeditor.dev/editor/docs/theming), [content styling](https://mdxeditor.dev/editor/docs/content-styling), and [translation](https://mdxeditor.dev/editor/docs/i18n)
- [MDXEditor upstream package manifest and MIT license declaration](https://github.com/mdx-editor/editor/blob/main/package.json)
- [react-markdown security and URL handling](https://github.com/remarkjs/react-markdown#security)
- [Milkdown design and integration](https://milkdown.dev/docs/guide/why-milkdown)
- [uiw React Markdown editor](https://github.com/uiwjs/react-md-editor)

## Outcome

- Implemented by GPT-5.6 Luna workers at max reasoning effort in isolated worktrees; integrated, reviewed, and accepted in the originating checkout.
- Shared `MarkdownEditor` now serves project guides, template guides, BUG descriptions, and both BUG comment entry points. It provides visual editing, source, shared preview, Chinese controls, and application theme tokens. Markdown remains the stored format.
- Shared `MarkdownView` owns reading layout, safe URLs, code copying, image enlargement, and bounded image recovery. Project resolvers deduplicate signing and retain raw Unicode/space/percent asset keys, including reference images and first-definition precedence.
- Guide saves retain failed drafts, serialize acknowledgements, preserve later edits, and flush the active table cell before explicit saving. Image uploads preserve their insertion position and order, retain concurrent text edits, and provide retry without persisting temporary URLs. BUG screenshots remain separate attachments and final payload limits include diagnostics.
- User documentation: `docs-site/user-guide/projects/index.md`, `docs-site/user-guide/projects/project-templates.md`, `docs-site/user-guide/workbench/index.md`, and `docs-site/user-guide/superadmin/bug-management.md`.
- Developer documentation: `docs-site/dev/concepts/frontend-layers.md`. No backend schema change or ADR was needed. Unreleased Added and Fixed entries were added to `CHANGELOG.md`.
- Verification: 44 focused tests across nine Vitest files passed; the changed renderer/save suites were rerun after the final fixes. All 11 Chromium browser cases passed against a disposable API database and object buckets. These cover no-op source preservation and undo, saving and upload failures, concurrent edits, table cells, CDP-simulated Chinese composition, template link popup/reopening, and BUG create/edit/attachments/comments/keyboard submission. Light/dark reading and popup screenshots plus a 375 px BUG drawer were visually reviewed.
- Browser measurement: Linux, Xeon Gold 6238R, Chromium 147.0.7727.15, development server, 105,847 UTF-8 bytes, 20 image references, and 50 input events. Input-to-paint p95 was 28.8 ms, measured using two animation frames after input; signing remained one request. This is a local device result, not a cross-browser or production-device qualification. CDP composition does not constitute manual operating-system IME testing.
- Type checking, web lint, CSS tokens, production build, bundle budgets, documentation frontmatter/navigation, and `git diff --check` passed. Lint retains two pre-existing warnings in `EditUserModal.tsx` and `useOnboardingProjectState.ts`. The main entry is 684.8 KiB within its 700 KiB budget; Markdown vendor code is 115.3 KiB within 150 KiB. The lazy editor chunk is 1,390,596 bytes (462,644 gzip bytes). Parsing the production entry's static import graph confirmed that it excludes the editor chunk.
- Temporary databases, buckets, Redis, analytics files, browser reports, traces, screenshots, test caches, and build output were removed after inspection. The small checked-in example PNG is an intentional reusable test fixture.
- Release milestone: Not yet determined. No push, release, or remote CI run was requested. Deferred scope remains inline uploaded BUG images, workbench mention/canvas comments, and the other consumers listed above.
