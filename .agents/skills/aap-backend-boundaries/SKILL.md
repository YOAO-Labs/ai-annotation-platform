---
name: aap-backend-boundaries
description: Change or qualify backend module ownership, transaction-preserving capabilities, independent artifacts and processes, or inference-plugin installation boundaries. Not required for routine route/service edits.
---

# Backend owner boundaries

First identify the actual branch and capability being changed. The primary checkout and experimental module/plugin branches do not have the same architecture. Start with [backend layers](../../../docs-site/dev/concepts/backend-layers.md); if present in this checkout, read the affected section of `docs-site/dev/concepts/module-deployment.md`, the module README, and its packaged contract. A historical plan, existing wheel, or successful child slice does not establish a complete module or authorize a new architecture.

For inference-plugin or GPU lifecycle work, read [installation and lifecycle](references/inference-lifecycle.md). For native model training/return, use [aap-online-learning](../aap-online-learning/SKILL.md); for payload/geometry and final labeling writes, use [aap-prediction-pipeline](../aap-prediction-pipeline/SKILL.md).

## Keep one owner

- Separate the algorithm/data owner, caller authorization, transaction owner, and physical execution owner. Reuse the existing typed contract; do not replace a finite capability with generic SQL, arbitrary callbacks, ORM objects, or a second business implementation.
- A Native capability borrowing the caller's connection must preserve the caller's commit/rollback, savepoint, locks, flush order, dirty rows and ORM cache behavior. Moving that operation to HTTP is a transaction change, not a packaging refactor.
- Remote operations need the existing durable command/status semantics. Recover an ambiguous acknowledgement by status; unknown does not mean rolled back and does not authorize reposting a mutation.
- Keep cross-module reads on the current public relation/DTO and writes on their owner. Preserve exact parent/child/project authorization and original API Key proof; a frozen execution scope is context, not continuing permission.
- Where the current branch uses function-only PostgreSQL capabilities, test with the actual restricted LOGIN and prove private-table and unrelated-function denial. A superuser or owner test does not prove isolation. Retain frozen SQL resources, constrained search paths, object ownership and migration/grant order from that module's implementation.
- Keep capability-specific credentials with their process owner. An ordinary API/worker must not regain private DSNs, backend secrets, or signing keys through inherited URL/file selectors, broad role grants, or a compatibility fallback.

## Qualify the actual consumer

Verify the requested integrated acceptance slice, not every helper independently. Distinguish source checks, installed-package checks, cold-image execution, real HTTP/worker consumers, restore, daily adoption, and production qualification. Do not infer any later boundary from an earlier one.

For installed artifacts, use the existing locked non-editable build and verify packaged SQL/schema/resource files plus entrypoints in the target image. Bind evidence to actual source inputs, image digest, grants and runtime configuration; a changed Git SHA alone neither proves invalidation nor permits relabeling old evidence. Reuse unchanged evidence only when the inputs and scope are demonstrably unchanged.

Exercise rollback/late-result/cancellation or process-loss behavior where the change crosses those boundaries. Preserve failed receipts separately from later passes. Refresh the real owning services through [aap-runtime](../aap-runtime/SKILL.md); do not switch the user's daily topology, rotate credentials, or revoke old grants unless that cutover is part of the requested task.
