# Plan archival conventions

Each file in `docs/plans/` records an implementation plan for one development session. **Plans are session logs and historical indexes, not the source of lasting knowledge.** Content intended for developers or users must be incorporated into `docs-site/` and `CHANGELOG.md`.

## Current work

| Plan                                                                                     | Remaining scope                                                                                                                                                        |
| ---------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Point-cloud densification route](1791963159_pointcloud-densification-route.md)          | Experimental integration implemented: one augmentation selector, traditional compatibility, prepared assets and managed generation; broader qualification remains open |
| [Project-scoped employee roles](1789807315_project-scoped-employee-roles.md)             | Shared-development/production cutover and compatibility cleanup remain separately gated                                                                                |
| [Internal usability and admin settings](1788952434_internal-usability-admin-settings.md) | Implementation completed locally; first-time colleague trial and target-deployment acceptance remain                                                                   |
| [Documentation media refresh](1789269444_docs-media-refresh-macos.md)                    | Point-cloud canary, one layout recording and the Linux/GPU batch remain                                                                                                |
| [Raster-mask workbench Epic](2026-07-21-raster-mask-workbench-unification-epic.md)       | Retains cross-workflow invariants; semantic/panoptic work is an unscheduled backlog draft                                                                              |

Unscheduled directions are indexed in [backlog/](backlog/README.md). Completed
session records remain in [archive/](archive/), with their original filenames and
evidence. The point-cloud route links its six predecessor sessions; research
results are indexed separately in [docs/research/](../research/README.md).

## Naming conventions

New plan files in both the root directory and `backlog/` must use `<unix-seconds>_<topic>.md`:

- `unix-seconds` is the **10-digit Unix timestamp in seconds at creation**, available from `date +%s`. Do not use milliseconds or assume the timestamp starts with `17`.
- Separate the timestamp and topic with one underscore `_`. Use lowercase English words separated by hyphens `-` for the topic, without a version number.
- Plans for different topics created in the same second may share a timestamp. If the full filename conflicts, add words that distinguish the scope; do not overwrite an existing file.
- Keep the filename when editing, promoting, or archiving a plan. Preserve historical filenames with date or version prefixes and their references; do not migrate them in bulk. Legacy drafts are renamed on promotion as described below.

Examples:

- `1788739200_docs-deep-optimization.md`
- `1788739201_admin-feedback.md`

## Release milestones

Organize plans by topic, scope, dependencies, and acceptance criteria without assigning a version in advance. Maintainers determine release milestones, version assignments, and release dates. Agents must not assign versions themselves or infer release schedules from filenames, plan order, or phases. Maintainer-confirmed milestones may be recorded in the body; filenames must still omit version numbers.

## Active plans and archives

Keep plans that are being implemented, awaiting approval, or still constrain future work in the `docs/plans/` root. Completed, abandoned, or superseded plans may move to `docs/plans/archive/`; update all Markdown references at the same time. A minor version change no longer automatically determines the archival scope. Follow the maintainer's scope when one is specified.

Leave existing archives in place. Do not archive solely by timestamp or date: Epics that still constrain future work stay in the root until completed, abandoned, or superseded.

Store unscheduled research drafts that require a fresh repository review before implementation in `docs/plans/backlog/`. These drafts do not reserve versions, authorize implementation, or participate in the root's stale-plan check. Before implementation, re-audit them through the [promotion gate](backlog/README.md#promotion-gate) and move them to the root as current plans. Do not code directly from a backlog draft.

## Required completion steps

After implementing each plan, complete all three steps:

1. **Append a `## Outcome` section** listing the landed changes and corresponding official documentation paths (user-guide / dev / adr / changelog).
2. **Update official documentation**: annotator, administrator, or super-admin impact belongs in `docs-site/user-guide/`; developer impact in `docs-site/dev/`; architectural decisions in `docs/adr/`; releases in `CHANGELOG.md`.
3. **Do not leave knowledge only in the plan.** If a plan results in no official documentation updates, its knowledge has not been incorporated into lasting documentation.

## `## Outcome` template

```markdown
## Outcome

- Landed commits: `xxxxxxx`
- Release milestone: record only maintainer-confirmed arrangements; otherwise write "Not yet determined"
- User documentation: `docs-site/user-guide/admin/xxx.md`
- Developer documentation: `docs-site/dev/troubleshooting/xxx.md`
- ADR: `docs/adr/00NN-xxx.md`
- CHANGELOG: Unreleased entry added (record the actual version once released)
- Remaining work: ... (hand off to the next plan / issue / TODO)
```

## CI guard

The `validate` job in `docs-validate.yml` warns about plans older than 30 days without a `## Outcome` section; it does not block merging. For a plan that will never have an outcome (an abandoned exploration), add `> Status: abandoned` at the top of the file.

`docs-site/scripts/check-plans-freshness.mjs` scans only active plans in the root directory. Archived files are excluded from stale-plan reminders.
