# Dropdown placeholder consistency

## Scope and decisions

- Audit native selects, shared FilterSelect controls and Radix Select across the app, with emphasis on online learning. The docs site's custom theme has no affected native dropdowns.
- Mark display-only prompts and empty/loading states as disabled hidden options. Derive muted trigger text from the selected option's explicit attributes, including nonempty retained IDs; do not infer semantics from empty values or translated wording.
- Preserve meaningful all/none/default options, exact asynchronous selections, pagination fallbacks and dependent-field resets.
- Provide explicit clear actions for model comparison, evaluation comparison, dataset baselines and class bindings. Preserve segment lease release through a named exit option.
- Move instructional prose below the field while keeping names, status and source information needed to distinguish choices.

## Acceptance

- Real-browser fixtures cover light/dark themes and narrow screens, native keyboard selection, nonempty temporary prompts, option colors, meaningful empty values, Radix menus and binding clear actions without an API or database.
- Component regressions cover comparison cancellation, dependent filters, stale bindings, deployment resets, original selection retention and segment exit semantics.
- Run relevant tests, web lint, type checking, formatting and diff checks; remove generated test artifacts and task-owned browser/runtime processes.

## Outcome

- Implemented shared native prompt styling, explicit prompt/status markup across app surfaces, external instructional copy and explicit clear/exit actions. Kept existing Radix placeholder behavior.
- Added retained selections for pagination, refreshed model/deployment/service lists, connection creation, video/point-cloud controls and receiver/project choices. A missing preset in a different backend/task bucket returns to the prompt without applying another preset. Invalid deployment, merge and receiver choices cannot proceed as a different object.
- Verification: the full web suite plus corrected asynchronous-wait reruns cover 603 test files and 6,121 passing tests; the four rerun files passed all 58 tests. Nine browser fixture checks passed across desktop light/dark and narrow viewports. Manual browser inspection confirmed both themes, no console errors and no API traffic for the isolated fixture. These checks do not claim a live training/inference run or remote CI.
- Web lint, the CSS token gate, type checking, formatting and diff checks passed. Five existing lint warnings and two existing style advisories remain. Test output, temporary screenshots/logs, the task browser session and the task Vite server were cleaned up.
- User documentation: `docs-site/user-guide/online-learning/index.md`.
- Developer documentation: `docs-site/dev/reference/design-system.md`.
- CHANGELOG: Unreleased fix entry.
- Release milestone: Not yet determined.
- Remaining work: none for the requested dropdown scope.
