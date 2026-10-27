# T01 — Versioned training parameter catalog

> Status: completed — implemented and verified on 2026-10-15
> Priority: first
> Depends on: existing qualified training adapters
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and evidence

T01 is delivered. The shared package covers all 113 pinned Ultralytics fields and 92 pinned LibreYOLO YOLO9 fields; 36 and 35 fields respectively are editable. The project modal provides a read-only searchable inventory and the warehouse view reports contract compatibility. T02 owns the complete creation editor.

Verification: 26 shared-contract tests, 48 API tests, 27 executor tests, 10 web tests and 4 SDK contract tests passed. After normalizing the policy fixture import, its 19 cases passed again. Web type checking, Python lint/format, web lint/CSS tokens, OpenAPI generation, documentation build and `git diff --check` passed. Web lint retains five existing warnings.

Live evidence: both pinned frameworks completed two-epoch runs with changed optimizer and augmentation settings; native resolved-configuration artifacts matched all reviewed effective fields. A separate matrix completed real cancellation and resume for both adapter revisions in both frameworks, preserving frozen digests and producing verified best weights. This exposed and repaired Ultralytics historical-best retention; preparation-stage cancellation also retains the downloaded resume input. Independent read-only review found no remaining high-confidence blocker. Desktop and 390px browser checks covered inventory search/filtering, contract summaries, both themes and request/console errors.

All seven temporary native runs, their stored artifacts, 12 async jobs/notifications, disposable test infrastructure, browser credentials and screenshots were cleaned. The two original manual-acceptance runs, data, owned development services and previous executor images remain available.

Build limitation: the executor image built successfully and its packaged native inventory drift check passed. A full API image build could not fetch the pinned base manifest because Docker Hub returned EOF. The Docker API dependency export and packaged inventories were verified offline using the same filesystem layout; a complete production API image build remains unverified. No release or remote CI is claimed.

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

## Promotion review — 2026-10-15

Baseline: `1bf3eb5ea`. Existing A–E and unified training entry are accepted. The retained manual development environment, historical runs and untracked `.orca/drops/` assets must be preserved. No release milestone has been assigned. Orca relay is unavailable; implementation is serial in this checkout, with read-only independent review.

This is a large change across API, a new shared package, executor/build, and web. No database migration is needed. Inventory is generated from installed, pinned native source, never from runtime internet discovery. Each known field receives a reviewed disposition for YOLO11 detection or YOLO9 detection. Other families/tasks are not implicitly qualified.

### Exact implementation surfaces

- `apps/_shared/training_contract/`: `pyproject.toml`, `training_contract/__init__.py`, `catalog.py`, `validation.py`, packaged inventory JSON, `tools/snapshot_native.py`, and `tests/test_contract.py`.
- API: `app/schemas/training_run.py`, `training_hub.py`, `learning_policy.py`; `app/services/training_adapters.py`, `training_runs.py`, `learning_policies.py`; `app/api/v1/training_runs.py`, `training_hub.py`, `learning_policies.py`; `pyproject.toml`, `uv.lock`, and `openapi.snapshot.json`; focused training/learning-policy tests.
- Executor: `training_executor/adapters.py`, `train.py`, `libreyolo_adapter.py`, `worker.py`; `pyproject.toml`, `uv.lock`, `export_requirements.py`, `requirements.lock`, `Dockerfile`; adapter tests. `docker-compose.training.yml` and `infra/docker/Dockerfile.api` package the shared definition.
- Web: `api/trainingRuns.ts`, `api/trainingHub.ts`, a read-only parameter inventory view under `pages/Training/`, `TrainingPage.tsx`, and its focused tests. T02 owns the full editor; removed homepage choose/train actions stay removed.
- Documentation: `README.md`, `CHANGELOG.md`, `docs-site/user-guide/online-learning/index.md`, `docs-site/api/guides/training-datasets.md`, `docs-site/dev/concepts/training-datasets.md`, `docs-site/ops/deploy/docker-compose.md`, generated API route docs, this plan, backlog links, and `packages/python-sdk/api-coverage.toml` if a new route needs an explicit disposition.

### Settled contracts and compatibility

`TrainingRunCreate.configuration` is optional and contains `schema_id`, `schema_digest`, and typed JSON `values`. Absence retains the exact legacy six-field serialization and adapter revision 1. Supplying both an explicit legacy `parameters` object and v2 configuration is rejected. Server-side framework/family/schema validation is authoritative; schemas include finite scalar/enum/optional/list/union rules and ordered tuple bounds. Unknown, reserved, or unqualified writes fail before preflight/enqueue.

Revision 2 freezes identity, submitted values, effective native values and origins. Canonical six-field `config.parameters` remains available to existing progress/report/resume readers. Defaults are immutable snapshots. Native resolved values are observed after setup/checkpoint restoration and before the first epoch; a missing or mismatched reviewed setting fails. Full native observations are stored as an artifact.

Executor protocol 2 advertises exact supported schema identities. Missing handshake can claim only legacy revision 1. Shared matching is used by claim and UI compatibility. Existing lease completion/recovery is independent of the latest creation catalog. Image identity remains strict by default; an optional administrator-reviewed catalog `legacy_images` list can qualify an upgraded image for historical v1 execution only when versions, resource and explicit supported revision all match. Actual runtime is recorded; historical frozen identity/digests are never rewritten.

Legacy policies retain their original template comparison and defaults. An explicit v2 policy save pins schema identity and full effective defaults; automatic children replay that pinned configuration. New catalog defaults never silently reinterpret policy JSON.

### Verification and acceptance gates

- Shared package: offline pytest for full key coverage, strict types/finite values, aliases, ordered lists, bounds, dependencies, schema digest and frozen replay.
- API: focused training-run/hub/learning-policy tests plus v2 contract tests through verified disposable `dev:worktree exec --mode test`; preserve legacy fixtures and prove old executors cannot claim v2, v1 idempotent replay is unchanged, and unsupported/reserved values fail before enqueue.
- Executor: offline contract/adapter tests; bounded actual two-epoch native runs for both frameworks with changed optimizer and augmentation; verify resolved configuration artifact and deliberate mismatch rejection. Verify legacy resume remains revision-specific. Only owned GPU runtime may be refreshed after checking it is idle; retain the previous image and state.
- Web: targeted Vitest, web typecheck, lint/CSS tokens, OpenAPI regeneration and SDK contract tests; live browser inventory browsing/search/status filters on desktop/mobile/dark and existing navigation. No new homepage choose/train CTA.
- Documentation: formatting, relevant links, docs build, final diff review and `git diff --check`.
- Clean test databases/buckets, test caches, screenshots and credentials after validation. Retain the user's manual development environment and its data.

Rollback disables v2 creation while retaining v2 readers/runner for accepted runs. Do not remove installed package data, reinterpret JSON, or release an owned GPU lease without process-exit proof. Any field without actual-use evidence remains visible and unqualified.

## Outcome

- Delivered scope: Delivered the versioned native parameter inventory, effective-configuration validation, historical compatibility and qualified framework execution.
- Completion evidence: the existing `Outcome and evidence` section above records implementation and verification. Archival on 2026-10-22 reuses that record; application tests and live acceptance were not rerun for this documentation move.
- Enduring documentation: [configuration.md](../../../docs-site/user-guide/online-learning/configuration.md), [training-datasets.md](../../../docs-site/dev/concepts/training-datasets.md); user impact is recorded in `CHANGELOG.md`.
- Release milestone: no new milestone or version is assigned by this archival.
- Later build evidence: [unified task/data contracts](1792120837_unified-task-data-contracts.md#completed-verification) records the subsequent API image build; the earlier build limitations above remain historical evidence.
