# Repository instructions

`AGENTS.md` is a symlink to this file. Maintain one shared instruction source. Write agent instructions, skills, supporting references, and agent handoff documents in English. Replies and product documentation follow the user's language and existing conventions.

## Complete the task

- Carry the requested outcome through implementation, relevant documentation, and verification. When requested, include browser validation, commits, pushes, or release work; do not stop at the first implementation. A review or diagnosis alone does not request a fix.
- Keep changes within the requested task or epic and, for staged plans, the active milestone's acceptance criteria. Fix regressions introduced by this work and concrete prerequisites required for acceptance; do not treat the rest of the backlog as authorized work.
- Discovered bugs, nearby cleanup, and unrelated check failures do not authorize scope expansion. Report them briefly and continue the requested work; do not repair them merely to make a broad suite green. If a blocker requires a material scope change, explain the dependency and obtain approval while continuing independent work.
- Resolve routine choices from context. Ask only when missing information materially changes correctness, scope, or authorization; complete independent authorized work meanwhile. Carry prior authorization and corrections forward, including across side questions.
- User instructions take precedence over repository and skill guidance, subject to system and developer instructions. If a skill blocks authorized work, identify the exact file and instruction and explain the actual conflict.
- Read the affected implementation and relevant callers. Use the routing below when useful; do not read every document or load every skill for each edit.
- Reuse existing owners, helpers, patterns, and dependencies. Prefer the simplest design that satisfies the requested capability; explain consequential architectural extensions and preserve unrelated work.
- Delegate independent, bounded work when it saves time or improves quality. Read-only agents may share this checkout; modifying agents need isolated worktrees based on the current local `HEAD`. Before delegation that writes code, read [parallel work](.agents/references/parallel-work.md).

## Implementation discipline

- Validate untrusted input at the boundary that owns it, then rely on the validated internal contract. Do not repeat checks already guaranteed by schemas, constructors, or internal types.
- Tie retries, fallbacks, compatibility paths, and concurrency safeguards to concrete failure modes or requirements within scope, not hypothetical robustness. Preserve meaningful failures; do not mask broken contracts with default values or silent catches.
- Keep straightforward logic local. Extract helpers when they are reused, name a meaningful concept, or isolate a genuinely complex boundary; do not preemptively generalize for imagined callers.

## Verification

- For staged plans, organize implementation and verification around a coherent, independently verifiable acceptance slice, not each helper, file, or checklist item. A passing subtask check does not complete the requested milestone.
- During implementation, use focused checks to resolve concrete uncertainty, reproduce a bug, or localize a failure; follow explicit test-first requirements. Otherwise, batch verification at the acceptance boundary instead of automatically adding tests or running broad suites after every small edit.
- At the acceptance boundary, exercise the integrated behavior and run required checks before reporting completion. Scale verification to the change: instruction-only or documentation-only edits need relevant content, link, and formatting checks, not application suites.
- Permanent tests should protect stable, consumer-visible behavior, boundaries, or failure modes, not temporary intermediate implementations, wiring, or incidental wording. Reuse existing coverage; add regression tests when they protect against a plausible bug.
- Repeat completed checks only when subsequent changes can affect their results or a relevant failure or unresolved concern requires investigation. Do not repeat completed acceptance work simply because a task resumes.
- Review the final diff and run `git diff --check`. Report the result, relevant evidence, and precise remaining limitations concisely, including unrelated failures left out of scope. Distinguish local checks from live browser results and remote CI.

## Project boundaries

- `apps/web` is a React/Vite SPA; `docs-site` is Vue/VitePress. Preserve each surface's framework and theme system.
- For browser automation and live UI validation, when `command -v agent-browser` succeeds, load and prefer the project [agent-browser skill](.agents/skills/agent-browser/SKILL.md). Use `agent-browser skills get core` for guidance matching the installed CLI version; explicit user choices of browser or session take precedence.
- In `apps/web`, use semantic Tailwind classes backed by `apps/web/src/styles/shadcn.css` and `@theme inline`. CSS modules read `--sc-*`, never legacy `--color-*`. Do not add bare/arbitrary colors to `className`; canvas/data colors and shadow/overlay rgba are narrow exceptions.
- Use semantic status utilities such as `text-status-danger bg-status-danger-soft`, not paired hue classes. App dark mode uses `data-theme` and Tailwind `dark:`; do not introduce `.dark` there. The docs site intentionally uses `html.dark` and its own `--docs-*` / `--home-*` tokens. See [design system](docs-site/dev/reference/design-system.md).
- After app styling changes, run `pnpm --filter @anno/web lint:css-tokens` (also included in web lint). Prefer existing compact text sizes, semantic overlay layers, Lucide icons, and local UI adapters.
- Worktree `.env` and Node dependencies may be symlinks to the primary checkout. Check their targets before changing configuration or installing dependencies. Python environments and generated API types belong to the current checkout. See [runtime skill](.agents/skills/aap-runtime/SKILL.md).
- Celery does not hot-reload mounted Python code. Worker changes require refreshing affected running workers in the intended development stack and checking the new task/signature inside them. Dependencies or baked source require rebuilding; migrations require applying. Determine actual mounts and services first.
- Tests that seed or migrate databases must target a verified disposable test database. Follow the relevant test configuration; never assume the everyday development database is disposable.

## UI conventions

- Keep typography consistent by role: `text-sm` (13px) for body text, names, table rows and form controls; `text-xs` (11px) for secondary metadata, hints and table headers; `text-md` (15px) for section titles. Keep page titles at the existing `text-xl` scale. Use shared tokens instead of new pixel sizes.
- Put select labels above the control and explanatory hints below it. Hints use `text-xs text-muted-foreground` with comfortable line height and `aria-describedby`; never use pure white or primary foreground for hints. Keep option labels concise. Do not embed explanations, defaults or instruction text inside selected values or option labels; empty native selects can use a blank disabled hidden option.
- Management lists should use the available content width. Prefer the existing centered `Modal` for item details and creation forms instead of a permanently reserved, mostly empty detail column. Preserve deep links, pagination, return navigation, Escape dismissal and focus restoration. Keep long detail content scrollable inside the dialog.
- Organize complex workflows around the current item, primary action and actionable results. Put infrequent configuration, history and detailed accounting behind clearly named disclosures; keep active work, errors and required confirmations visible. Auto-open initial setup when no item exists and relevant history when directly linked.
- Before completing UI changes, inspect real browser screenshots in light and dark themes and at desktop and narrow widths; exercise the changed controls and check for horizontal overflow, console errors and failed requests. Preserve the manual acceptance environment and clean task-owned test artifacts.

## Documentation and releases

Before committing, update documentation affected by the change in the same commit:

| Change                      | Documentation                                                                    |
| --------------------------- | -------------------------------------------------------------------------------- |
| API                         | `docs-site/api/`, `README.md`; regenerate affected API artifacts                 |
| Feature or user-visible fix | Relevant `docs-site/user-guide/` pages and top `CHANGELOG.md` Unreleased section |
| Architecture                | `docs-site/dev/concepts/`; ADR in `docs/adr/` when warranted                     |
| Environment variable        | `.env.example`, `pnpm docs:gen-env-vars`, `DEV.md`                               |
| Removed or renamed symbol   | Search Markdown references and update affected links                             |

Keep rendered documentation about the current system, without dotted version provenance such as `vX.Y.Z`. Record provenance in Git history, `CHANGELOG.md`, or ADRs — not in source comments or frontmatter. Exceptions: `CHANGELOG.md`, `docs/adr/**`, `docs-site/dev/adr/**`, and `*.generated.md`; runtime requirements, routes, and protocol versions remain valid.

`CHANGELOG.md` follows Keep a Changelog: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`, omitting empty groups. Explain user impact. Pure refactors, tests, and formatting may omit an entry. Release headings use `## [x.y.z] - YYYY-MM-DD`, newest first.

Only bump versions for a requested release; use [aap-release](.agents/skills/aap-release/SKILL.md) for the synchronized version and OpenAPI workflow. `node scripts/check-doc-version-prefix.mjs --staged` is advisory. New plan filenames use `<unix-seconds>_<topic>.md`: a 10-digit Unix timestamp at creation, an underscore, and a lowercase kebab-case topic, with no version number. Keep existing filenames and links; see [plan conventions](docs/plans/README.md) for promotion and archival. Maintainers determine release milestones and version assignments; agents must not infer them from plans or assign them independently.

CI workflow files use `<domain>-<action>.yml`; the aggregate remains `ci.yml`. Top-level names use sentence case, preserving proper nouns and acronyms. Every job sets a static, repository-wide unique `name: <Domain> <responsibility>`, including single-domain workflows. Job names use sentence case, preserving proper nouns and acronyms; keep tool lists in step names. Do not put slashes in job names: GitHub already displays `<workflow> / <job> (event)`. Keep workflow names short and distinct. Workflow files use block-style YAML with two-space job keys and four-space job properties so the dependency-free naming checker can inspect them. `node scripts/check-workflow-names.mjs --strict` enforces these conventions in CI and pre-commit.

## Task routing

Use only the relevant entry. Paths in skills are repository-relative unless linked otherwise.

| Task                                                                    | Entry                                                                            |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Worktree setup, runtime refresh, stuck Celery tasks                     | [aap-runtime](.agents/skills/aap-runtime/SKILL.md)                               |
| App release/version synchronization                                     | [aap-release](.agents/skills/aap-release/SKILL.md)                               |
| Screenshot/video capture, media provenance or review failures           | [aap-doc-media](.agents/skills/aap-doc-media/SKILL.md)                           |
| Workbench layout, preferences, task switching, playback or write guards | [aap-workbench-state](.agents/skills/aap-workbench-state/SKILL.md)               |
| Point-cloud/WebGPU or precise-video qualification                       | [aap-renderer-validation](.agents/skills/aap-renderer-validation/SKILL.md)       |
| Python SDK/CLI/TUI capability or API-contract changes                   | [aap-sdk-contracts](.agents/skills/aap-sdk-contracts/SKILL.md)                   |
| Preannotation pipeline contracts and worker/backend routing             | [aap-prediction-pipeline](.agents/skills/aap-prediction-pipeline/SKILL.md)       |
| Local shadcn/Radix primitive composition or update                      | [shadcn](.agents/skills/shadcn/SKILL.md)                                         |
| Visual audit or improvement of an existing product screen               | [redesign-existing-projects](.agents/skills/redesign-existing-projects/SKILL.md) |
| Public landing/marketing surface                                        | [design-taste-frontend](.agents/skills/design-taste-frontend/SKILL.md)           |

`README.md` covers setup; `DEV.md` covers local commands. `docs-site/dev/` contains architecture, references and troubleshooting; `docs-site/ops/` contains deployment/runbooks. Use `docs/adr/README.md`, `docs/plans/README.md`, and `docs/research/README.md` to locate decisions, plans, and research. Preview docs with `pnpm docs:dev`; verify the actual URL printed by the server.

For a reported UI defect, inspect the affected flow's recent API calls and console errors. `BugReportDrawer` stores evidence in PostgreSQL `bug_reports`; [runtime diagnostics](.agents/skills/aap-runtime/references/diagnostics.md) gives a read-only fallback when an API token is unavailable.

For instruction maintenance only, see the [source and history audit](.agents/references/2026-09-06-instruction-audit.md).
