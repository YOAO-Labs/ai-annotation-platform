# 0138 — Workspace Project Read Foundation

- **Status:** Accepted foundation; Project query owner integration remains open
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Supersedes:** None

## Context

Template reads now have their own qualified owner. Project list, pagination,
detail, stats, readiness and post-write response projection still run through API
queries. They combine Workspace rows with Labeling counters and annotations,
Identity profiles, Assets links/items, Jobs state and Inference pool mappings.
Moving the serializer alone would leave those ownership dependencies intact.

The reads also differ in authority and snapshot timing. Lists and stats use the
authenticated request's admitted role. Detail and readiness load the Project,
re-read current account status, refresh the Project and resolve current member
access. Readiness captures dataset IDs before its later item and Task counts.
Those phases must survive extraction rather than becoming one new snapshot.

## Decision

Publish fifteen fixed, read-only PostgreSQL capabilities in migration0260:

- Labeling owns raw persisted Project counters, grouped AI task counts, two
  annotation count variants, Task and annotation time series, linked/non-uploading
  Task counts, non-archived batches and grouped batch Task counts.
- Assets owns captured ProjectDataset IDs, item count and a dataset-ID-only item
  relation. Labeling consumes that relation without re-reading current links.
- Jobs owns separate create-tasks status counts and latest status reads.
- Inference owns a stable pool-to-legacy-instance lookup. The existing volatile
  configuration helper remains unchanged.

Each function is STABLE, PARALLEL UNSAFE, SECURITY DEFINER and uses a fixed
`pg_catalog, pg_temp` search path. Functions belong to their actual NOLOGIN domain
owner. Workspace receives fourteen execute capabilities; only Labeling receives
the Assets item-ID relation. No raw foreign-table permissions reach Workspace.
Additional timestamp/status reads remain within Labeling's own data authority.
No business rows, receipts, locks or new query state are created.

The checked-in schema is a PostgreSQL function catalog, not a JSON message DTO.
SQL argument/return declarations define its wire types. Later query transport
must encode exact bigint counts without losing precision; it must not duplicate
ProjectOut validation inside the private owner.

Time-series capabilities retain one36/24-FILTER aggregate scan before expanding
the twelve result buckets. They normalize input array lower bounds and preserve
endpoint order. AI-completed Task counts intentionally include active cancelled
annotations; dashboard annotation totals and series exclude cancelled rows, as
the current API does. Raw materialized counters are not normalized or recounted.

Move Project access values and capability policy to the existing independent
Workspace package's `aap_workspace.access_contract`. Actual API, service, worker
and test callers use that owner directly; the old API value module is deleted.
The membership owner shares its compatibility helper instead of maintaining a
second implementation. SQL access resolution and write locks remain in their
current application adapters. These local values are not remote permission
assertions and do not replace final current-authority checks.

## Frozen behavior and evidence

The pre-extraction fixture records27 actual HTTP observations and two caller
autoflush/no-autoflush probes through committed request sessions. The driver
checks28 relevant files against `50fc1df9ac9f99fdbbed7ac0eda7205d7287156d`;
a supplemental guard verifies all1,100 existing source/schema/SQL files in the
selected runtime scopes. The actual capture ran at `c50835877b4b8bd0aefbc7023f75b6ffd48e7223`,
after test and additive Native commits but before changing those runtime files.
IDs and generated timestamps are aliased; original captures stay immutable.

Actual capture exposed incorrect fixture assumptions: ProjectCreate ignores the
extra label-config, guide and rendering fields; the nullable PATCH probe must use
the supported rendering-config field. The corrected fixture records its committed
JSONB-null/public500 result. Failed attempts remain separate and are not counted
as passes. The initial catalog preflight needed an explicit empty definitions map;
no acceptance resources had been created at that failure.

The date probe verifies43 physical Project columns and both ISO and SQL/DMY
DateStyle settings. Finite JSON dates remain ISO; date infinities decode through
asyncpg as `date.min`/`date.max`, while native JSON keeps infinity strings. The
query row materializer must preserve that distinction, alongside the already
established float and timestamp codecs.

Six owned-database Native cases pass for domain owners/grants, raw counters,
distinct/cancelled AI semantics, bucket boundaries, captured IDs after unlink,
batch/job/pool rules and malformed inputs. The separate scalar probe and corrected
legacy capture pass. All their disposable resources were removed without errors.

The integrated source checks pass519 cases:184 Workspace,10 API value/predicate
checks and325 architecture cases, with137 inapplicable architecture parameters
skipped. The affected public API/authority suite passes67 cases. Public OpenAPI
matches its existing snapshot. These groups overlap and are not a single count.

Ten affected protected restores pass at0260. The configuration restore also
executes the new counter capability as Workspace before and after the actual
backup restore, verifying its cross-owner grant and data result. Full migration
validation passes fresh260,173 reversible downgrades to base,86 suffix downgrades
to0174, forward migration and restore. All four derived databases are removed
without cleanup errors or leftovers.

The first independent install exposed a stale cached wheel: it still contained
the previous membership policy and omitted the new access module. uv's default
local-project cache checks do not cover arbitrary source-only changes; the
package now declares keys for metadata, README and Python/schema sources.
See the [uv cache-key semantics](https://docs.astral.sh/uv/concepts/cache/#dynamic-metadata).
Installed source hashes, rather than environment creation alone, remain the
delivery gate.

A warm-cache removal probe also found an obsolete module in setuptools'
incremental `build/lib` directory after its source file had been deleted. The
wheel was rebuilt but still shipped that staging file. Workspace now reuses the
repository's Hatchling backend with explicit
[package selection](https://hatch.pypa.io/latest/config/build/#packages) from
`src/aap_workspace`. Artifact checks must reject unexpected source/schema files,
not merely verify hashes for files they expected to find.

An isolated warm-cache probe passes five consecutive phases: initial install,
Python addition, Python modification, Python removal and schema modification.
Package metadata stays unchanged between phases and no forced refresh/reinstall
is used. Its copied source, environment and cache are removed after verification.

Independent noneditable installation at `1e081adadff6adeeaad8b4909766a30cb42fed9c`
passes 184 Workspace and 27 Inference tests outside the source tree. All 51 package
Python/schema files match their source manifests. The installed environment has
no API, SQLAlchemy, Celery, Boto3 or Torch package.

The Workspace image verifies the same 51 files in an offline, mount-free,
read-only container as UID 10004 with one CPU and 256 MiB. The API image verifies 173
package files and 183 API/Native inputs, unchanged OpenAPI and migration head 0260
as UID 10001 with one CPU and 512 MiB under the same filesystem/network restrictions.
Both drop all capabilities, disallow privilege escalation, cap processes at 64
and exit without OOM. Their proof containers are removed; the images and installed
environment remain local review artifacts. These are package/cold-assembly checks,
not Project-query provider or production-capacity acceptance.

The API image's first cold probe exceeded the operating system's single-argument
limit while passing its source manifest to Python. The corrected probe streams
its program through standard input and reuses the already fingerprinted image.
The failed attempt remains separate from the passing result.

## Remaining work and limits

This establishes data and value ownership foundations. Project query handlers and
their SQL still belong to the API; no independent Project query protocol/runtime
or three-path provider qualification is claimed yet. Current Identity Briefs
selection and failure semantics must remain lazy at the profile-read phase.

The complete read owner also needs global-search Workspace facts, receipt-bound
configuration/transfer readback and the class-rename accepted-access context.
Supplemental captures must precede those integrations. Class-rename no-op keeps
its no-commit behavior; successful writes must not gain a new postcommit authority
check. The API remains the trusted coordinator until the corresponding command
ownership moves; computed visibility/manager booleans are not sufficient evidence.

Full Workspace and the all-module deployment/language-replacement objective
remain active. Shared physical PostgreSQL is still allowed by the design;
these typed capabilities do not claim a cross-database snapshot or complete
module credential isolation. Go remains an optional experiment.
