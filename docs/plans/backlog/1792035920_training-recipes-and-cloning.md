# T03 — Project training recipes and experiment cloning

> Status: research-draft
> Priority: next
> Depends on: [T01](../1792035920_training-parameter-catalog.md), [T02](1792035920_training-configuration-workspace.md)
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and existing owners

Managers reuse an understandable configuration without copying numbers between forms. A recipe update cannot change a queued experiment or an enabled automatic policy.

Reuse [frozen run configuration](../../../apps/api/app/services/training_runs.py), [policy contracts](../../../apps/api/app/schemas/learning_policy.py), [policy execution](../../../apps/api/app/services/learning_policies.py), [training UI](../../../apps/web/src/pages/Projects/ProjectTrainingPanel.tsx), and [policy UI](../../../apps/web/src/pages/Projects/ProjectLearningPolicyPanel.tsx). Existing policy runtime templates are not a user recipe library.

## Decisions and interfaces

- Add project-owned recipe identity and immutable recipe revisions. A revision records name/description, author, framework/model/task, schema identity, controlled initialization selection, overrides, and origin. Archive recipes rather than mutate revisions referenced by runs or policies.
- Provide project-scoped paginated list/detail, create revision, and archive operations with optimistic revision checks and existing manager/scope authorization. Reuse validation from T01; a recipe is data, never an executable script.
- Apply a recipe through a before/after diff in T02. Dataset choice stays explicit for manual experiments. Show incompatible or unqualified fields; never reinterpret an old recipe with new framework defaults.
- Clone an experiment into a new draft with source run/attempt and configuration digest provenance. Copy eligible execution settings, not request IDs, leases, artifact upload URLs, automatic-trigger identity, or resume state. Always rerun preflight.
- Cloning uses the selected original base source. Initializing from trained weights is separately gated by [T10](1792036132_training-controlled-warm-start.md); cloning is not a hidden warm start.
- An automatic policy may select a specific recipe revision and store its fully expanded effective configuration. Recipe edits show an available update but do not change the active policy. Adopting a revision is an explicit policy revision with current authority and catalog validation.
- Start with user-authored project recipes and saving from verified experiments. Do not label suggested presets as quality recommendations without project evidence; no cross-project sharing or global marketplace is introduced.

```text
Project recipe -> immutable revision -> configuration draft -> preflight -> run snapshot
                                  +-> explicit policy revision -> frozen automatic run
Existing run -> clone provenance --^
```

## Implementation, acceptance, and rollback

Candidate additions are recipe/revision models, an additive migration, schema/service/API, typed web API, recipe selector/editor, and provenance fields in run/policy snapshots. More than eight files are expected. Existing runs without recipe references remain valid. No new service, environment variable, or external account is required.

Verify stale revision conflicts, archived/referenced revisions, cross-project denial, deleted source visibility, clone versus resume, independent request IDs, recipe upgrades, policy edits during active training, and preservation of frozen run digests. Browser acceptance covers applying/cancelling a diff, editing a clone, refreshing the draft, and explicitly adopting a policy recipe revision.

The fragile assumption is that users expect a recipe edit to update future automatic work; explicit revision adoption resolves the ambiguity and keeps provenance visible. The minimal option is clone-to-draft with configuration export, without a recipe database. Rollback stops recipe writes and selection while retaining references and expanded run/policy configurations. Apply the roadmap's verification and documentation protocol.
