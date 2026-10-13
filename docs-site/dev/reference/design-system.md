---
title: Design System
audience: [developer]
type: reference
status: stable
last_reviewed: 2026-09-17
---

# Design System

This page covers two related but separate systems: the **web app** (`apps/web`, Tailwind + shadcn — most of this page) and the **documentation site** (`docs-site`, VitePress — see [Documentation Site Tokens](#documentation-site-tokens-vitepress) at the end). They do not share a stylesheet.

The web app uses Tailwind CSS with shadcn/ui primitives. Runtime theme values live in `apps/web/src/styles/shadcn.css`; component styling should consume Tailwind semantic classes or `--sc-*` CSS variables.

Shared filter controls, applied URL state, and query/action scopes are described in [Filtering state and result scopes](./filtering.md).

## Theme Tokens

`shadcn.css` is the single source of truth for neutral surfaces, text, borders, radii, focus rings, and canvas-only theme values.

| Purpose               | Token / class                                                           |
| --------------------- | ----------------------------------------------------------------------- |
| Page background       | `--sc-background` / `bg-background`                                     |
| Card surface          | `--sc-card` / `bg-card`                                                 |
| Popover surface       | `--sc-popover` / `bg-popover`                                           |
| Primary text          | `--sc-foreground` / `text-foreground`                                   |
| Secondary text        | `--sc-muted-foreground` / `text-muted-foreground`                       |
| Muted surface         | `--sc-muted` / `bg-muted`                                               |
| Hairline border       | `--sc-border` / `border-border`                                         |
| Primary action        | `--sc-primary` / `bg-primary text-primary-foreground`                   |
| Brand accent          | `--sc-brand` / `text-brand bg-brand`                                    |
| Danger status         | `--sc-status-danger` / `text-status-danger bg-status-danger-soft`       |
| Caution status        | `--sc-status-caution` / `text-status-caution bg-status-caution-soft`    |
| Positive status       | `--sc-status-positive` / `text-status-positive bg-status-positive-soft` |
| Info status           | `--sc-status-info` / `text-status-info bg-status-info-soft`             |
| Info alternate        | `--sc-status-info-alt` / `text-status-info-alt bg-status-info-alt-soft` |
| Search highlight      | `--sc-mark` / `bg-mark text-mark-foreground`                            |
| Stage: image          | `--sc-stage-image` / `text-stage-image bg-stage-image-soft`             |
| Stage: video          | `--sc-stage-video` / `text-stage-video bg-stage-video-soft`             |
| Stage: 3D/point cloud | `--sc-stage-threed` / `text-stage-threed bg-stage-threed-soft`          |
| Overlay blur          | `--sc-overlay-blur` / `backdrop-blur-overlay`                           |

Dark mode is driven by `<html data-theme="dark">`. `shadcn.css` redirects Tailwind's `dark:` variant to that attribute, so components should use `dark:` classes rather than a `.dark` class.

## Semantic Color

Neutral UI should stay neutral. Use color only for meaning:

| Meaning               | Base hue  | Utility                                        |
| --------------------- | --------- | ---------------------------------------------- |
| Failure / destructive | `rose`    | `bg-status-danger-soft text-status-danger`     |
| In progress / warning | `amber`   | `bg-status-caution-soft text-status-caution`   |
| Success / complete    | `emerald` | `bg-status-positive-soft text-status-positive` |
| AI / model            | `violet`  | `bg-status-info-soft text-status-info`         |
| Information / counts  | `sky`     | `bg-status-info-alt-soft text-status-info-alt` |

Status chips should use the semantic soft background, the matching status text utility, and a small `bg-current` dot when a quick scan cue helps. Do not write paired hue classes such as `text-rose-600 dark:text-rose-400`; the status utilities read theme tokens from `shadcn.css`.

Search hits in list panels use `bg-mark text-mark-foreground` (solid warm background with dark text) so they stay legible on both themes. The stage colors (`image` / `video` / `threed`) are a separate meaning from status: they only distinguish which workbench the pending commands apply to when a view mixes stages (for example the shortcut panel's "all types" filter). Do not reuse them for state. Every centered overlay uses the single `backdrop-blur-overlay` utility so the workbench's floating panels and app modals blur the backdrop to the same degree. The 3D/point-cloud workbench keeps the dim without the blur: backdrop-filtering a live software-rendered canvas stalls compositor cleanup, so those panels use the no-blur overlay variant.

## Type Scale

The app uses a compact type scale. Prefer named text utilities instead of arbitrary pixel classes:

| Class       | Size | Use                                             |
| ----------- | ---: | ----------------------------------------------- |
| `text-2xs`  | 10px | dense badges and compact metadata               |
| `text-xs`   | 11px | small labels, table headers, secondary metadata |
| `text-sm`   | 13px | default app copy, form controls, panel headings |
| `text-md`   | 15px | compact section headings                        |
| `text-stat` | 22px | dashboard metrics                               |

Rare exact-size utilities (`text-3xs`, `text-micro`, `text-ui`, `text-control-xl`) exist for migrated control details. Do not add `text-[Npx]` in component class names; add or reuse a named token in `shadcn.css` when a new size is genuinely required.

## Z-Index Scale

Use semantic z utilities instead of raw numeric z-index classes:

| Class                                        |       Value | Use                                                |
| -------------------------------------------- | ----------: | -------------------------------------------------- |
| `z-local-1` ... `z-local-6`                  |     1 ... 6 | tightly scoped canvas or 3D local stacking         |
| `z-base`                                     |          10 | page-local elevated elements                       |
| `z-dock` / `z-dock-control`                  |     14 / 15 | workbench docks and dock controls                  |
| `z-local-overlay`                            |          20 | local menus and resize handles                     |
| `z-popover` / `z-popover-elevated`           |     30 / 55 | popovers that must sit above local panels          |
| `z-dropdown`                                 |          50 | dropdown and select content                        |
| `z-modal`                                    |          50 | modal overlay/content using current Radix stacking |
| `z-floating`                                 |          50 | floating workbench panels                          |
| `z-tooltip`                                  |          50 | tooltip content and arrow                          |
| `z-drawer-backdrop` / `z-drawer`             |     60 / 61 | custom drawer backdrop and content                 |
| `z-notification-backdrop` / `z-notification` |   200 / 201 | notification popovers and badges                   |
| `z-workbench-modal`                          |        1000 | workbench-local blocking dialogs                   |
| `z-app-drawer-backdrop` / `z-app-drawer`     | 1099 / 1100 | app shell drawer overlay and content               |

## Rules

- Do not use bare color values in component class names: no `#hex`, `rgb(...)`, `oklch(...)`, or Tailwind arbitrary color utilities.
- Do not read legacy `var(--color-*)` tokens in CSS. CSS modules that remain should use `--sc-*`.
- Do not add one-off color meanings in a page. Reuse the semantic status utilities above.
- Do not add arbitrary pixel text sizes. Use the compact type scale, or define a named token in `shadcn.css`.
- Do not add raw numeric `z-N` / `z-[N]` classes. Use the semantic z utilities above.
- Use shadcn/ui primitives from `apps/web/src/components/shadcn/ui/` for low-level behavior where possible.
- Keep existing `@/components/ui/*` adapters only when they preserve the current app API; they should delegate to shadcn/Radix behavior or Tailwind classes internally.

## Decision dialogs

Blocking decisions — confirms, destructive actions, multi-choice steps, and required-reason input — use one shared capability: `@/components/ui/decisionDialog` (`confirmDialog` / `choiceDialog` / `inputDialog` / `alertDialog`), rendered by a single `<DecisionDialogHost />`. The host mounts next to both `ToastRack` sites in `App.tsx` so the full-screen Workbench routes are covered without `AppShell`. Content dialogs (forms, previews, multi-section panels) remain `components/ui/Modal`. `beforeunload` stays native because the browser requires that dialog to be synchronous.

One capability, one visual language: never introduce a second confirm / alert / reason surface. An eslint `no-restricted-properties` rule (error) bans `window.confirm` / `window.alert` / `window.prompt` call sites in `apps/web/src`, with test files exempt. Copy follows an action–object–consequence voice: the title states the action and object (`删除数据集`), the description states the consequence and scope, and the confirm button uses the verb. Danger tone is restrained — a status-colored icon and confirm button over a soft danger icon container (`text-status-danger bg-status-danger-soft`), never full-bleed red. Destructive dialogs focus Cancel by default and neutral ones focus Confirm; Esc and overlay click cancel. Callers keep owning their pending states — the service resolves before the action runs — and results still surface through `Toast`.

## Filter presentation

Filtering entry points use the existing `Icon name="filter"` funnel, not a plus or settings icon. Use `components/filters/FilterTrigger` for an expandable filter and `FilterGroup` for inline scopes. Set explicit SVG size classes (`size-4` for ordinary controls, `size-3` for dense panels) because Button CSS can override numeric SVG dimensions. Keep the Icon adapter's stroke and semantic theme colors.

`FilterPanel` is a controlled, non-modal, trigger-attached Popover on desktop and a bottom Sheet on narrow screens. Its desktop geometry uses an 8px trigger gap and 12px viewport clearance; only the body scrolls. The owner retains drafts across the responsive branch. Give every panel an accessible title and distinguish explicit Apply/Cancel forms from immediate filters. Do not change global Modal positioning to restyle a filter.

Reuse `FilterToggle`, `FilterSelect`, `FilterSection` and `ActiveFilterChip` for compact options, labeled fields and applied summaries. Chip removal is a sibling action, never a nested button; invalid conditions need visible text as well as color. Keep native checkbox/select/range semantics when Workbench keyboard isolation relies on them. Preserve source/status color meanings and virtualized row measurements.

Keep high-frequency choices inline and complex Data Manager expressions below the toolbar. Search, sort, display columns, true navigation tabs and mutation scope controls remain distinct. The [state and result-scope contract](./filtering.md) still owns reset, history, pagination and action-scope rules.

## Icon semantics

The app keeps Lucide as its primary icon library. `components/ui/Icon.tsx` renders
both Lucide icons and the small annotation-specific set in `annotationIcons.ts`.
Local icons use Lucide's renderer, 24px grid, `currentColor`, and the adapter's
size, stroke, forwarded ref, and decorative accessibility behavior.

- Reuse an icon for the same action across pages (search, save, delete). Do not
  require globally unique icons.
- Give different choices in the same navigation or tool group distinct silhouettes.
  Color, selection backgrounds, and tiny badges alone do not establish identity.
- Consume existing owners: Sidebar navigation descriptors, `ToolMeta.icon` via
  `TOOL_REGISTRY` / `ALL_TOOLS`, and `TOOL_UNIT_GROUPS` for project configuration.
  Do not create a parallel registry or pick a new icon independently in a consumer.
- Image and video single-frame tools share geometry. Polygon/polyline trajectories
  add a trailing-frame outline; Mask trajectories combine it with a region contour.
  Rotated boxes use a tilted rectangle with a handle; keypoints use connected nodes.
- Mask painting uses a brush, smart scribble uses a marked stroke, and canvas comments
  retain the pencil. Scissors remain appropriate for cutting and point-cloud selection.
- Check local SVGs at 12px (configuration/task queue), 13px (tool hints), 16px
  (navigation), and 17px (dock). Inspect light/dark, selected, disabled, hover, and
  keyboard focus states in a real browser. Retain accessible button names and tooltips.

Generic decorative/action icons may still import Lucide directly. New dependencies
or broad icon-library migrations require a demonstrated coverage or design benefit.

## Validation

Run the web lint gate after styling changes:

```bash
rtk pnpm --filter @anno/web lint:css-tokens
rtk pnpm --filter @anno/web lint
```

The token gate checks Tailwind class names for bare colors and dark-mode pairing, warns when status colors, arbitrary text sizes, or raw z-index classes are not using semantic utilities, and fails if CSS reads old `--color-*` variables.

## Documentation Site Tokens (VitePress)

The documentation site (`docs-site`) runs VitePress with the default theme and does **not** import the web app's `shadcn.css`. It has its own two-layer token system so the brand landing page and the reading pages can share one brand source while using different intensities of color, type, and motion.

| Layer              | File                              | Scope                                            | Character                                                                                                                  |
| ------------------ | --------------------------------- | ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| Reading layer (L1) | `.vitepress/theme/docs-theme.css` | All content pages (user-guide / dev / ops / api) | Neutral fog-white / ink-black surfaces, royal-blue links, Chinese reading rhythm. Maps `--docs-*` onto VitePress `--vp-*`. |
| Brand layer        | `.vitepress/theme/docs-home.css`  | Home page brand components only                  | Strong royal blue / paper / ink / acid green + display serif (`--home-*`). Never overrides body reading tokens.            |

**Shared brand source.** Both layers derive from royal blue `#172cff`. The home page uses it as a full-bleed background; body pages collapse it to `--vp-c-brand-1` (link + active state). Keep this the single brand hue for the docs — do not introduce a second accent for body pages. Acid green `--home-acid` is home-only (AI / annotation nodes); it must not enter body reading pages.

**Dark mode.** VitePress toggles `html.dark` (not the app's `data-theme`). Dark overrides in `docs-theme.css` therefore use the `.dark` selector, and royal blue is lifted to `#93a0ff` for link contrast on the ink background.

**Fonts are local-only.** `--docs-font-sans` / `--docs-font-mono` / `--docs-font-serif` are pure system/local stacks. The docs site must not inject runtime web fonts (e.g. Google Fonts).

**Sync principle.** When the brand hue changes, update `--home-blue` in `docs-home.css` and `--docs-royal` (light) plus its dark lift in `docs-theme.css` together. When adding a body-reading token, add it under `--docs-*` in `docs-theme.css` rather than inline in a component.

### Documentation reading layout

`DocsLayout.vue` extends the default theme through its public slots. It adds a domain link and, when available, the existing document type above an article; the sidebar carries the same domain label. The page H1 and introduction remain in Markdown. Unknown or missing metadata omits the corresponding label. The wrapper applies `docs-reading-page` only to documentation layouts, keeping the marketing composition separate.

| Element      | Reading treatment                                                                                                  |
| ------------ | ------------------------------------------------------------------------------------------------------------------ |
| Sidebar      | 256 px desktop rail; task groups, native active-branch expansion, blue text and a subtle active background         |
| Article      | `--docs-reading-width: 760px`; one continuous column, with local overflow for wide tables/code                     |
| Outline      | `--docs-outline-width: 200px`; H2/H3 hierarchy, wrapped labels, native active-section marker                       |
| Domain hub   | `pageClass: docs-hub-page` and `aside: false`; `--docs-hub-width: 1040px`, task links before supporting references |
| Display type | Local serif H1, 40 px desktop / 32 px mobile; serif hub section headings                                           |
| Reading type | Sans-serif H2/H3 at 26/20 px, body at 16 px with 1.8 line height; navigation/captions at 13–14 px                  |
| Surfaces     | Flat reading canvas; `--docs-radius-sm: 4px` for selection and `--docs-radius: 8px` for cards/media/code           |

The header menu and desktop sidebar start at 960 px; the desktop outline starts at 1280 px. The header uses the native menu below 960 px on both the reading pages and marketing homepage so Chinese labels fit. Smaller screens retain VitePress's native drawer and local outline control. Domain hubs use the existing `DocLinkCard` and `doc-card-grid`; reading pages keep normal Markdown headings, lists, containers, details, and code groups. Links acknowledge hover/press through color, without decorative motion. See [Writing documentation](../how-to/write-documentation) for content structures and anchor preservation.

Check the actual computed article width when changing CSS: default-theme scoped rules can override a less specific custom max-width. Keep global layout changes under `docs-reading-page`; homepage CTA rules belong to `docs-home.css`. Capture and verify old article anchors with `docs-site/scripts/check-reading-layout.mjs`, then inspect both themes and the 390/768/1024/1280/1440 px breakpoints in a real browser. The checker accepts `capture|verify`, a preview URL, and a baseline JSON path; capture refuses to overwrite an existing baseline.
