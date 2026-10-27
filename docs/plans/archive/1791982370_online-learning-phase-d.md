# Online learning: automatic training policy

Status: accepted

The user delegated phase scope and acceptance decisions to the implementation agent. Phase C passed its recorded acceptance. This stage adds opt-in scheduling without automatic model publication.

## Contract

- One policy per image-detection project, disabled initially. Managers explicitly provide the reviewed-data scope, initial split parent, controlled trainer/base model, bounded training parameters, required changed-sample count, check interval, and cooldown. Store the exact catalog version, image, base digest, and resource identity; catalog changes require a new policy revision.
- Reuse immutable dataset construction and the existing fenced training state machine. A periodic Celery task scans due policies and reconciles active triggers. A manager can request a check through the same path. Persist trigger history and recover the database-commit/broker-dispatch gap with the same dataset attempt.
- Derive semantic partition identities from ordered classes, actual media SHA256, and sorted normalized labels. Exclude task/annotation IDs, actor identities, paths, timestamps, and repeated identical samples. Assign full content/scene groups before automatic-package deduplication. Count new or corrected training contents relative to the last successful auto run; validation-only changes do not satisfy the growth threshold.
- Keep one active automatic candidate per project, including queued, cancelling, and lost training. Later edits remain visible to a fresh scan after completion. Only successful training advances the baseline. Block the same failed training digest within the policy revision, including candidates with validation-only changes; explicit policy re-enablement or revision permits a deliberate retry.
- Serialize policy changes and checks with the existing project advisory key before authority/policy/trigger/run locks. Executor claim keeps its resource/run ordering and reads current policy eligibility without taking a policy lock. Disable cancels unclaimed automatic training and pending data construction; started work remains visible with existing cancel controls. Recheck current account, project, key scopes, project status, policy revision, and fixed catalog before automatic enqueue and claim.
- Keep automatic provenance and referenced inputs until their dependents are explicitly removed. Check references before deleting storage objects. Policy removal requires disabled state, terminal triggers, and no remaining referenced runs.

## Implementation

Add policy/trigger models, migration, schemas, service, project API, and scheduled worker. Add semantic identities and automatic deduplication to the dataset bridge. Add nullable automatic-trigger identity and an active-project constraint to training runs, claim/resume guards, deletion guards, typed web API, and policy/history controls in the learning page. Synchronize OpenAPI, SDK web-only scope, user/API/architecture/operations documentation, and changelog.

## Verification

Use the verified disposable test environment for semantic noise/duplicates, validation-only changes, inherited groups, overlapping checks, edits while active, success/failure baselines, failed-digest blocking, explicit retry, disable/claim races, authority revocation, fixed-template changes, resume conflicts, and dispatch recovery. Exercise a real owned dataset-to-training trigger and browser enable/check/disable/history flows; retain existing publication unchanged. Refresh only owned workers. Run affected schema, lint, type, docs, and diff checks, then clean intermediates.

## Rollback

Disable the policy to stop new triggers and cancel unclaimed automatic work. Keep started work, datasets, experiments, trained-model versions, manual training, and current project releases available through their existing controls.

## Acceptance evidence

The isolated browser saved an explicitly enabled policy with threshold 1, interval 1 minute, cooldown 0 and two training epochs. Its automatic dataset `50cc02a4-2196-4a7b-8ddb-e11f1c980edd` contained six unique training contents. Run `bc3b8ecd-42ad-4def-874d-a97e2669d983` completed on the authorized single GPU and advanced the success baseline. The project release remained generation 5 / V1. Browser disable advanced policy revision to 2 and preserved its records.

The owned ordinary and maintenance workers were refreshed and both registered the new periodic task. A real dispatch through the owned broker executed it successfully after the final baseline-cache change. The 390px policy/history view had no horizontal overflow or console errors; inspected screenshots were removed.

Verification passed: 24 initial semantic/policy/bridge cases, four additional authority/resume/dedup cases, 12 fairness/manual-training cases, one baseline-parent cache regression, ten web component cases, Python/web/CSS lint, TypeScript and diff checks. The shared documentation/schema acceptance runs with the final multi-adapter delivery. Temporary test directories, XML reports and caches were cleaned. The idle owned training executor was removed; disposable fixture retention remains limited to the final authorized adapter acceptance.

## Outcome

- Delivered scope: Delivered opt-in automatic training policies, semantic change baselines, fenced triggers, history and disable/claim protection; model publication remains manual.
- Completion evidence: the existing `Acceptance evidence` section above records implementation and verification. Archival on 2026-10-22 reuses that record; application tests and live acceptance were not rerun for this documentation move.
- Enduring documentation: [automation.md](../../../docs-site/user-guide/online-learning/automation.md), [training-datasets.md](../../../docs-site/dev/concepts/training-datasets.md); user impact is recorded in `CHANGELOG.md`.
- Release milestone: no new milestone or version is assigned by this archival.
- Remaining boundaries: preserve the exclusions and qualification limits recorded above; new task, inference or deployment scope requires its own plan and acceptance.
