# 0143 — Workspace Project Class Rename Owner

- **Status:** Accepted; implementation in progress
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Supersedes:** None

## Context

Project class rename currently combines Workspace binding policy, Labeling
Annotation mutation, live authority locks, caller-owned commit and Project Query
projection. It cannot be split into two independently committed HTTP writes.

Two actual old-behavior captures freeze 21 HTTP scenarios, one explicit caller
rollback probe and five real competing-lock scenarios. Their SHA-256 values are
`976b5b1d7c0508c17e7102b0d712db8e8dacdacb0876a2189ca60d3055335ea7`
and `6e7abbf1a55c48a8eab27e1ec864e9f63a9065c5b9fcd2779b9f05d4a6a763ef`.
Their unchanged-source guards and actual effects are documented in
`apps/api/tests/fixtures/project_class_rename_legacy/README.md`.

The existing Configuration manager's generic `begin`/`finish` path creates
operation state and a receipt. Equal-name rename is currently a no-op with no
DML, receipt or caller commit; it cannot use that path unchanged. The generic
`update_project` policy also performs unrelated validation and side effects.

## Decision

Extend the existing Workspace Configuration owner with a dedicated
`rename_project_class` operation. Reuse its private endpoint, credentials,
selection rules and portable data port. Keep all six existing operations and
their reply shapes unchanged. Use a separate rename success result with the
changed flag, lock-derived class-rename evidence and optional bound NoOp
projection. No new runtime or public route is introduced.

Public admission and initial management preflight remain before name trimming.
Blank names retain 400 before business locks. Current account SHARE, Project
KEY SHARE and optional membership SHARE locks use NOWAIT and preserve their
existing busy reasons. Complete Project/member values must be decoded before
the policy decisions that consumed them previously. A changed rename upgrades
the Project to UPDATE before binding validation; an equal-name rename skips
that upgrade and all class/unit existence checks.

Portable Workspace policy preserves stored-unit iteration and error precedence,
disabled units, nonmatching entries and nested extra fields. It receives ordinary
JSON values and emits the new bindings, without API or ORM dependencies. It
does not add create/update-project validation, configuration revisions or Audit.

Native 0267 adds
`aap_labeling_v1.rename_project_annotation_class(uuid,text,text) RETURNS void`.
The Labeling-owned, fixed-search-path SECURITY DEFINER function updates only
Annotation.class_name, scoped by Annotation.project_id and the prior name.
It has no Task, tool-unit, active, canceled or source filter, and does not change
Annotation version or timestamp. Null arguments fail 22023. Workspace code receives
EXECUTE only; it does not receive raw Annotation UPDATE. Labeling receives only
the additional class_name UPDATE privilege. No new LOGIN is introduced.

The Workspace transaction performs the Annotation effect before writing Project
bindings, matching the actual old SQL order. Both effects roll back together.
The default API binding does not commit, roll back or close its caller Session.
ORM identity-map and dirty-object internals are not a private wire contract.

Rename follows a state-free Configuration branch instead of generic begin/finish.
It does not manufacture a durable receipt for the former ephemeral
ProjectClassRenameEvidence. The API's current two-attempt transport retry is
disabled for this operation only; other Configuration operations retain their
receipt-backed retries. A failed or uncertain transport never falls back to raw
local writes. Public post-commit projection failure retains the old 503 behavior
with already-committed data; this operation does not claim automatic replay or
exactly-once semantics.

### NoOp projection and independent owners

The borrowed API path retains its existing post-command Project Query while the
caller transaction still holds authority locks. In an independent DSN/HTTP
owner, returning a no-op before projection would release these locks too early,
allowing an ownership/configuration change before a trusted projection reads it.

An optional NoOp projection callback therefore runs inside the owner transaction.
The Workspace HTTP assembly invokes its existing Project Query manager and
profiles directly, without recursively calling its own HTTP endpoint. The API's
direct-DSN adapter supplies its existing Project Query selector as the callback.
It runs while Configuration still holds the current authority/configuration
fence. Missing query/profile capabilities fail closed.

Independent no-op success carries the full strict Project Query reply, including
its bound request and data_json. Actor, Project, operation and evidence must
match the rename result. The gateway validates and materializes this result;
it does not perform a second unlocked read. NoOp ends the independent owner's
own transaction by rollback; it does not roll back a borrowed caller Session.
Changed operations retain commit before the ordinary gateway projection.

Project projection authorization already uses the fresh lock-derived evidence;
it does not require a duplicate stale admission-role field. Public credentials
remain at the API. No request supplies is_manager or another authority decision.

## Implementation and remaining gates

The pure binding transformation passed seven source tests. Native 0267 passed
four actual disposable-database cases covering function ownership/ACL, its narrow
row effect, caller-owned rollback and commit, and each null argument. All eleven
existing Identity, Workspace and Labeling restore checks passed at head 0267.
The first new effect test compared PostgreSQL's internal `"char"` byte result to
text; an explicit catalog cast fixed the test, and all four new cases then passed.
Both owned acceptance runs cleaned up their resources without errors. This is a
qualified primitive, not the completed command or a current container qualification.

Current public rename callers have not switched. Native 0268, the dedicated
Configuration branch/reply, NoOp callback, API selector and removal of the old
workflow/Annotation method follow as one integration boundary.

Before qualification, replay both frozen files through borrowed, dedicated and
installed HTTP bindings; verify actual NoOp lock duration, authority revocation
and changed/no-op busy behavior, caller rollback, field preservation, post-commit
projection failure and absence of retry. Old Configuration operations, function
OID/ACL, current-schema restore, migration reversal, noneditable installs and
restricted current images remain required gates. Baseline evidence and completed
class-query qualification are separate and must not be recounted.

This does not complete Workspace, Labeling or the ten-module/seven-gate goal.
Primary runtime and production remain unchanged. Go remains an optional experiment.
