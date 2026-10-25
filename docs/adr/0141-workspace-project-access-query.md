# 0141 — Workspace Project Access Query

- **Status:** Accepted; selected local qualification complete
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Supersedes:** None

## Context

`GET /api/v1/projects/{project_id}/access` previously resolved visibility in its
dependency, then resolved current access again in the handler. The second result
supplied the membership ID/version and capabilities. Reusing one earlier context
would change revocation, role/version and disappearance behavior.

The actual pre-extraction fixture freezes16 HTTP observations and two direct
resolver probes. The first capture incorrectly expected an old-role token to
return401. Actual behavior accepts the valid credential, resolves the current
project-admin role and returns404 for an unowned Project without membership.
Production was unchanged; the corrected capture passes and stays immutable.

## Decision

Extend the existing `aap-workspace-project-query/v1` owner with
`get_project_access`. It uses the existing request and reply envelope, Project
query URL/DSN, control bearer and function-only credential. No separate runtime,
secret family or Directory operation is introduced. The API retains credential
admission, the public route and its nine-field response model.

The first phase reads initial full Project, current actor, refreshed full Project
and optional membership. The final phase starts at current actor, then refreshed
full Project and optional membership. It does not repeat the initial Project
read. Each step retains its own statement snapshot; no stronger snapshot, new
lock, accepted-authority token or response cache is introduced.

New `access_member_rows` facts carry JSON text for zero or one complete nine-column
ProjectMember row, bound to the requested Project and actor. Both phases decode
all columns and timestamps before membership policy. The final membership ID,
role and version come from the same final read. Existing operations continue to
use their original four-field `member_access` action and temporal boundary.

Portable manager/member policy now lives in `access_contract.py`. Project and
Directory read helpers and the API's existing locked/nonlocked resolver consume
the same policy. The API resolver retains every actor/Project/member read, lock,
NOWAIT decision and caller-owned transaction boundary. Its old pure manager
builder is removed. A non-owner project administrator can still use membership;
an employee who physically owns the Project still needs membership. SA precedence
and manager null membership fields remain unchanged.

The complete ProjectMember codec moves to a shared Workspace leaf. Directory
retains its own unavailable-error translation and result format. Finite stored
timestamps outside Python's range remain internal errors; real infinities retain
the original decoded extrema. Public capabilities stay lexically sorted.

Native0264 replaces the existing query function body with the same signature.
OID, owner and deployed privilege chains remain in place. Existing operations
retain the frozen implementation and0262 timestamp checks. New access actions
accept only null extra binds; common reads delegate without extra validation I/O,
then echo the original request. Downgrade restores the frozen0262 wrapper through
`CREATE OR REPLACE`, without renaming functions or rewriting historical artifacts.

Access responses never initialize Identity Briefs. Explicit provider failures
retain the existing Project-query failure handling and never fall back locally.
Independent read-only connections cannot see another caller's pending work.
The frozen direct probes invoke the still-used API resolver; they deliberately
bypass the full HTTP visibility dependency and are reported separately.

## Verification and limits

The immutable fixture has16 HTTP responses, including owner/SA/member/viewer,
non-owner PA membership, missing/inactive/unknown-role cases, both finite
year10000 member timestamps and genuine infinities. Source Workspace tests
pass 239 cases. Native facts, malformed binds and deployed OID/owner/privilege
preservation pass their first three owned database cases.

The original Project50, Directory13 and Access16 HTTP observations match each
of the borrowed, dedicated and installed-HTTP bindings. Direct resolver probes
remain separate from the HTTP owner trace. Four process cases cover query/profile
grant loss and restart; Access continues to work without Briefs. A separate
noneditable package passes239 tests and matches all 43 Workspace source files.

Three paired legacy/new database cases commit role/version changes, deactivation
plus Project deletion, and an out-of-range timestamp between the two real read
phases. The final context uses version2/current reviewer role, inactive-account
401 precedes missing Project in the second phase, and late timestamp decode
remains500. With existing access, authority and lock regressions that run passes49
checks. Three invalid private reply role/kind combinations first reproduce a
missing consistency check, then pass after rejecting impossible SA/member and
viewer/annotator contexts. Public OpenAPI remains exact; architecture checks pass
with their141 inapplicable rules skipped.

A real configuration backup/restore additionally preserves ordinary member
access at version37 and manager access through independent Project/Briefs
credentials. The0262 privilege-chain regression also passes on the new schema.
Full migration validation passes fresh264,173down/base/up, the90-step reversible
suffix, authentic forward conversion and pre-conversion restore. Its four
derived databases and dump are removed.

The final selected-case union has 84 distinct passing cases, with no skips or
unresolved failures. The two earlier adapter failures retain their original
reports and are covered by the successful paired phase cases. Ten current-schema
Workspace/Identity restore cases pass, in addition to the actual container case.
Repeated runs do not increase the union. Aggregate evidence is
`.worktree/workspace-project-access-owner-qualification.json`.

Current owner and API images are built from
`966ceadf39624539654a18d9ffde8938ea42fb81`. The owner image
`sha256:cf374759d1ce78a7405bae9463d7875261c936155acf0fa53b1ec8d07b579e1d`
matches 185 installed package files. The API image
`sha256:5bacb66eb4190f0f3e2a4183f4bb1965b7165365d7aa2c43a8bc2fdd22532ee7`
matches 187 package files and 201 API/Native inputs, unchanged OpenAPI and head 0264.
Nonroot, read-only, offline proof containers pass and are removed.

The live owner image needs two read-only secrets and no Identity Briefs
configuration. A real API read observes an ordinary member's role/version change
from reviewer version37 to viewer version38, and restart retains the current result. Sixteen
requests at concurrency 4 pass within the owner's 1 CPU/256 MiB budget; the API and
database are outside that budget. Docker reassigns the ephemeral host port on
restart, so the test uses the inspected current address. Owned databases, Redis,
storage identities, buckets and containers are removed, with no cleanup errors.

This evidence is local to a Mac ARM64 host and linux/arm64 containers. It does not
establish production capacity or a common snapshot across independent services.
The default binding retains its caller transaction; independent bindings remain
read-only and cannot see pending caller changes. This extension does not move the
other API/worker resolver callers to independent services. Full Workspace, other
modules, another-language provider and primary cutover remain open. Go stays an
independent experiment.
