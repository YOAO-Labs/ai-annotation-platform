# App component customization

Read for styling a local shadcn primitive or integrating an upstream component into `apps/web`.

`apps/web/src/styles/shadcn.css` defines runtime `--sc-*` tokens. Its `@theme inline` mappings expose semantic Tailwind utilities. App dark mode uses `data-theme`; retain the existing Tailwind `dark:` mapping. Do not add `.dark`, `next-themes`, a second global stylesheet, or upstream unprefixed token definitions.

Prefer the primitive's existing variant/size, then appropriate semantic classes, then a maintained variant when the requested behavior needs one. Preserve local compatibility adapters and caller contracts. Wrappers are useful for actual application composition, not as a prerequisite to every change.

Use `bg-card`, `text-foreground`, `border-border`, `bg-primary text-primary-foreground`, and existing status utilities. CSS modules read `var(--sc-*)`. Status colors already support both themes; do not add raw hue pairs. Reuse compact text sizes, radii, and semantic overlay layers from [the design system](../../../docs-site/dev/reference/design-system.md).

If an upstream update introduces incompatible CSS variables, map it into this token system while reviewing the component diff. A requested global theme change belongs in the token source and must cover both themes. A local component edit does not request a preset replacement.

Check focus/contrast and affected interaction states, then run `pnpm --filter web lint:css-tokens`. The documentation site's `--docs-*`, `--home-*`, and `html.dark` are a separate system.
