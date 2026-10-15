# T01 — Versioned training parameter catalog

> Status: research-draft
> Priority: first
> Depends on: existing qualified training adapters
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and evidence

Managers can inspect the complete native configuration inventory for a qualified framework/model/task, edit supported fields, and see why other fields are controlled or unavailable. A submitted value must reach the native runner with the promised meaning.

Current [TrainingParameters](../../../apps/api/app/schemas/training_run.py) exposes epochs, batch, imgsz, seed, lr0, and patience. [API adapter options](../../../apps/api/app/services/training_adapters.py) and the [executor adapters](../../../apps/training-executor/training_executor/adapters.py) separately describe fixed behavior. [Frozen configuration](../../../apps/api/app/services/training_runs.py) and [automatic policies](../../../apps/api/app/services/learning_policies.py) already protect input identity.

The pinned [Ultralytics configuration](https://github.com/ultralytics/ultralytics/blob/v8.4.177/ultralytics/cfg/default.yaml) mixes multiple modes. [LibreYOLO configuration](https://github.com/LibreYOLO/libreyolo/blob/809009bc294daa6e75e0976ab7cfdd0057d17d79/libreyolo/training/config.py) uses inherited family defaults and warns before ignoring unknown keys. A method signature or one global key list is insufficient.

## Scope and decisions

- Generate candidate inventories from the exact dependency lock and qualified model family, including types, defaults, aliases, validation rules, and provenance. Generation happens during development/build, not through runtime internet access.
- Review every candidate's training/task applicability, Chinese label, group, help, and status: editable, platform-managed, inapplicable, or unqualified. Upstream support alone does not authorize platform exposure. New upstream keys must appear in a coverage/drift report before qualification.
- Use one packaged definition consumed by API and executor. Extend existing catalogs with schema identity and structured metadata rather than introduce a discovery service. Do not let unreviewed executor self-reporting expand writable capabilities.
- Keep platform device allocation, input/output paths, callbacks, downloads, process control, checkpoint integrity, and resume ownership reserved. Initially retain fixed batch semantics; AutoBatch and silent OOM parameter changes remain unavailable.
- Validate framework-specific types, finite values, ranges, aliases, dependencies, and model/task conditions on the server. Reject unknown keys, conflicting aliases, reserved-field writes, and accepted-but-unused settings.
- Freeze schema digest, adapter revision, resolved effective parameters, fixed options, and value origins. Record the native resolved configuration and reject unexplained mismatches before treating execution as qualified. Dynamic choices must be explicitly modeled or remain unqualified.
- Keep legacy six-field configurations and their original adapter revisions readable and resumable. New definitions cannot reinterpret historical runs or rewrite their digests. Automatic policies pin the reviewed definition and require an explicit revision when adopting new defaults.

```text
Pinned native config + validators + family overrides
                 -> reviewed packaged definition
                    +-> API catalog/preflight -> frozen run/policy
                    +-> UI metadata
                    +-> executor validation -> native resolved configuration
```

## Interfaces and candidate changes

Extend the existing project training catalog and global catalog summaries; detailed parameter metadata is project-authorized. Use typed framework-specific request validation behind the existing create/preflight endpoints. Candidate areas are the linked schema/services, executor `train.py` and `libreyolo_adapter.py`, catalog schemas, dependency export tooling, generated API types, and SDK contracts. This is a large change involving more than eight files; no new service or credential is required. Prefer additive JSON snapshot fields; determine whether indexes or a shared package require migration at promotion.

## Acceptance and failure handling

Every discovered field has a reviewed disposition. Native scalar, enum, optional, list/tuple, and union values round-trip without precision loss. Unknown or inapplicable fields fail before enqueue. API/UI/executor definitions agree. Framework-specific augmentation and optimizer changes affect a bounded real native run; ignored settings fail qualification. Legacy cancellation/resume, automatic-policy freezing, artifact downloads, and permission checks remain valid.

The fragile assumption is that a syntactically accepted field affects the selected model/task. Native resolved-config checks and focused native behavior tests are mandatory; unresolved fields stay visible but unqualified. The minimal alternative is a manually reviewed small typed expansion per framework. Rollback restores the prior creation schema while retaining revision-specific readers and adapters for accepted runs. Apply the roadmap's verification, cleanup, documentation, and promotion protocol.
