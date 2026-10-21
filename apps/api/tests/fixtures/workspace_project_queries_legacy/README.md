# Frozen Project read observations

These are actual pre-extraction observations, not candidate-owner results.
Do not regenerate them after changing the Project read implementation.

- `samples.json`: 27 HTTP observations and two caller autoflush/no-autoflush
  probes, captured at `c50835877b4b8bd0aefbc7023f75b6ffd48e7223`. The driver
  checked 28 projection/auth/model files against
  `50fc1df9ac9f99fdbbed7ac0eda7205d7287156d`; a supplemental Root guard checked
  all 1,100 existing source/schema/SQL inputs in the selected runtime scopes.
  SHA256: `3b0641a905f7e1950cdf22972d3a8a09553bba3daa1da8ee0e5a30731366bcbe`.
- `scalar-codecs.json`: real PostgreSQL date encoding under both ISO and SQL/DMY
  DateStyle settings, plus the 43 physical Project column names. Binary date
  infinities become `date.min`/`date.max`; native JSON retains infinity strings.
  Finite native JSON dates stay ISO under both settings.
  SHA256: `c3a8339be8c9ffaa083b0edfa50159c5ed3381afe7e99d10fe64eb37a1cd6047`.
- `temporal-extremes.json`: 12 real binary/text/JSON expression probes and eight
  actual HTTP reads, captured before extracting any Project reader. Finite BC
  dates and year 10000 dates fail with ValueError; corresponding timestamps fail
  with OverflowError, before current-account or profile reads. Stats also loads
  and decodes the full Project row. True infinities remain valid; timestamp
  infinities decode as naive extrema, while normal finite timestamps are UTC-aware.
  The 21-file guard checks the unchanged runtime against `86f4ff5`.
  SHA256: `c25cc7f6b507b18ec6eac4641c3041401d7eae927fa22183902d752e02aad584`.
- `consumers.json`: 15 actual HTTP responses for six search actors, collaborative
  video visibility, invalid video-boolean failure before dataset/member queries,
  class-rename no-op/change, transfer readback and configuration projection after
  committed actor demotion. No-op rename does not commit; accepted writes still
  return their projection after demotion. Exact response bytes and digests are
  retained alongside normalized bodies. The 34-file guard checks `86f4ff5`.
  SHA256: `f0d6a376e6d32765038af0de330059d8edff53ef090915ed7a8e2bb0746e7727`.

The HTTP matrix covers visible lists, pagination and filters, validation order,
detail/member roles, readiness, stats, configuration creation/update and a
committed JSONB-null rendering-config response failure. Creation ignores extra
`label_config`, `annotation_guide` and `rendering_config` fields; the nullable
PATCH probe uses the actually supported `rendering_config` field.

IDs, display IDs, generated timestamps and the run suffix are aliased. Fixed
dates remain literal. Far-past/future task timestamps keep captured stats stable;
the separate stats-series tests exercise weekly bucket boundaries. The recorded
SQL counts are diagnostic observations, explicitly not a matching requirement
for Native internal statements.

The supplemental observations freeze the remaining planned consumers; additional
authority races still require their own integration acceptance. These fixtures
alone do not qualify a complete Project query owner. All captures
used verified disposable resources, which were removed. Failed fixture attempts
and the supplemental source guard remain under Root `.worktree`.
