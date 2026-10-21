# 0139 — Workspace Project Query Owner

- **Status:** Accepted design; Native, API and runtime integration in progress
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Supersedes:** None

## Context

The [Project read foundation](0138-workspace-project-read-foundation.md) exposes
typed foreign facts and portable access values. Ordinary Project reads still
execute API SQL and serialization. Extracting them requires preserving authority,
per-statement snapshots, postcommit responses and selected Identity profiles.

## Decision

The Workspace package owns a closed Project query protocol, raw row codecs and
ordered query policy. Its seven operations cover list, pagination, detail,
statistics, readiness, accepted-command projection and Project search hits.
Twenty-four fixed read actions expose typed facts, echoing the request and their
exact parameters. Schema conditionals bind each action to its argument/fact
types and permitted operations. No arbitrary SQL or caller-supplied visibility
boolean is accepted.

List and statistics use admitted actor facts. Detail and readiness preserve the
first Project read, current Identity status read, refreshed Project read and
current role/membership checks as separate phases. Missing Project wins before
inactive actor; a missing refresh wins before invalid role. Readiness requires
management authority after those checks. No new locks or transaction ownership
are introduced. Pagination counts before loading rows in a later statement.

Full Project rows combine the 38 Workspace fields and five raw Labeling fields
in one statement snapshot, with one typed counter call for all selected IDs.
Aggregate bigint facts use canonical decimal strings. Persisted int4 counters
remain raw signed values; statistics sums may therefore be negative. Separate
annotation count snapshots may produce a ratio above 100, as before.

JSONB columns travel as their exact JSON text, distinguishing SQL NULL from JSON
null without prematurely applying public DTO validation. This also preserves
arbitrary JSON integers and finite JSON numbers that overflow Python floats.
Float columns are promoted to double precision before round-trippable text
encoding, preserving the original binary driver's float32-to-Python value. The codec retains the original driver
failure phase for finite temporal values outside Python's range. True date
infinities become date extrema; timestamp infinities become naive datetime
extrema, while ordinary finite timestamps remain UTC-aware.

Lists read grouped AI counts, then one batched Identity Briefs lookup, per-row
member/pool facts and requesting-user roles. Single-row responses read profiles
before member/AI/pool facts. Empty lists, statistics and readiness never initialize
the profile provider. The borrowed adapter reuses the API's selected Briefs port;
the standalone runtime requires an explicit Identity dependency. Its public
client belongs to Identity and validates the full profile contract. Selected
HTTP failures never fall back, and profile failures retain their public 503 detail.

Configuration and transfer projections bind actual accepted operation receipts.
Transfer retains both legacy row-read phases. The write coordinator borrows its
current transaction without autoflush to read and decode the row before its
caller commits. After commit, the selected query provider verifies the accepted
receipt and reads the current row again before enrichment, as the previous
route's refresh did. Concurrent updates are visible at that second read;
disappearance or decoding failure remains an internal error after the accepted
write. Neither phase reauthorizes the accepted operation. The precommit read is
independent of query-provider selection and is not fallback after remote failure.
Class rename carries raw accepted locked actor/project facts, bound to its source
operation, and recomputes management authority without a new postcommit check.
This context trusts the authenticated API coordinator; it is not a durable receipt
or a cryptographic proof. No-op rename retains its no-commit behavior.

Project search hits use the existing six output fields after full row decoding.
Task/member search consumes fixed Workspace relational facts in its own SQL
statement, preserving filter-before-limit behavior and raw video-collaboration
JSON. Global Search as a whole remains a separate multi-domain extraction.

## Evidence and remaining work

Two supplemental pre-extraction captures pass on owned disposable resources:
12 scalar expression probes plus eight HTTP reads, and 15 consumer HTTP samples.
They freeze extreme temporal behavior, six search roles, collaborative video,
the invalid-boolean 500 phase, no-op/change rename and committed projections after
role demotion. The first consumer fixture assumed a reviewer could not see an
unassigned-reviewer batch; actual legacy behavior corrected that expectation.
No production rule changed. The failed attempt is retained separately.

The portable manager/contract/codecs pass 25 focused cases covering read phases,
authority order, late business validation, exact counts, profile isolation,
membership scope binding and accepted projection context. Missing configuration
projection is an internal failure; malformed search results fail the private
contract before entering its untyped public response. The complete Workspace
source suite passes 214 cases, including Project-only runtime assembly, empty
profile isolation and the explicit HTTP Identity dependency without SQL fallback.
Native0261 passes three owned database cases for role grants, row/filter/projection
facts and search relations. Migrations upgrade, roll back and reapply grants;
owned resources are removed after the check. Migration compilation and invalid
fixture attempts are retained separately from that passing run.

API Project readers and accepted-write response paths now call the query owner;
Task/member search consumes the fixed Workspace relations. Thirty-five selected
Native/public API regressions pass without skips. The borrowed owner also matches
all 27 frozen HTTP observations and both caller transaction probes. Public
OpenAPI is unchanged. Full three-path comparison, faults/restores and noneditable
package/image qualification remain open. No production cutover is claimed.

The full all-module deployment/language-replacement objective remains active.
Shared PostgreSQL and the trusted coordinator are explicit current constraints.
