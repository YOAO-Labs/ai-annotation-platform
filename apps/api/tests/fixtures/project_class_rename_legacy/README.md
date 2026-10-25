# Project Class Rename Legacy Capture

`samples.json` is immutable, actual pre-extraction evidence from the unchanged
Project class-rename route and application workflow. It is not a proposed response
or a new implementation's qualification result.

- SHA-256: `976b5b1d7c0508c17e7102b0d712db8e8dacdacb0876a2189ca60d3055335ea7`
- Runtime base: `e2019de02d821b2f202ab4bc1770b004af159084`; 31 affected source files
  match byte-for-byte. The captured HEAD includes only later test/documentation
  changes. The driver hash records the actual executed test bytes.
- 21 real ASGI HTTP calls use a separate `legacy_runtime` connection to a verified
  disposable PostgreSQL database at head0266. Selected headers, normalized JSON,
  original response bytes, ordered client SQL and explicit Session transaction
  events are recorded. Internal Native statements and network performance are
  not measured. Response bytes contain synthetic fixture identifiers; replay
  compares the normalized body rather than unrelated new UUID bytes.
- A separate direct workflow probe records the dirty Project at workflow return,
  the caller's subsequent read/flush, independent-connection visibility and
  explicit rollback. The caller flush is not attributed to the workflow.
- The owned capture passes and removes its PostgreSQL, Redis, buckets and
  containers without cleanup errors. The source report remains
  `.worktree/project-class-rename-legacy-validation.json`.

The samples preserve scoped bindings with cross-unit Annotation updates,
including inactive, canceled and AI rows; omitted unit selection also updates
disabled stored units. Annotation version and timestamp remain unchanged.
Conflicts, missing classes, blank names, authorization and unsupported legacy
platform roles retain their actual status and error phase. A valid stale role
claim resolves the current actor and succeeds for the current manager.

After trimming, an equal old/new name bypasses class/unit existence checks and
does not commit. A real rename commits before its Project projection; a real
unavailable projection returns503 while bindings and annotations remain changed.
Uncommitted direct workflow changes remain invisible to another connection and
caller rollback restores both domains.

Do not regenerate this file after production rename code changes. Future current
owner replay must use a different report, preserve these bytes, and supply its
own actual lock/authority, three-binding, installed-artifact, image and recovery
qualification.

`locks.json` is a second immutable capture from five real held-row-lock cases.
Its SHA-256 is
`6e7abbf1a55c48a8eab27e1ec864e9f63a9065c5b9fcd2779b9f05d4a6a763ef`.
An independently held User UPDATE lock rejects both changed/no-op requests with
409 `account_busy`. A held Project UPDATE lock, or a KEY SHARE lock during a real
rename's lock upgrade, yields409 `project_busy`. Those four cases record actual
SQLSTATE55P03 and no commit. Project KEY SHARE permits the equal-name no-op;
bindings and annotations remain unchanged throughout. The test uses actual
independent transactions, with no injected lock error or production lock changes.
Its owned validation also passes and cleans up. These are baseline observations,
not qualification of a future command owner or production latency measurements.
