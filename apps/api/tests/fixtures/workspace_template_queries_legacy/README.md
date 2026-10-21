# Frozen template query observations

These files preserve actual pre-extraction observations. They are fixtures, not
new-owner qualification results. Do not regenerate them after changing readers.

- `samples.json`: 40 HTTP observations, two caller autoflush probes and one
  caller-timezone probe. Captured at `bbfb47bf301a6390f788cc3edb736dad383fb71b`;
  the six runtime sources were checked byte-for-byte against pre-port commit
  `a0317013841ca3aaf1b53b06ce051c6eb07ac798`. SHA256:
  `9c2d7207b6baf0168001b474885e9eccf83449de2cfde7903834cfa1abbb5634`.
- `scalar-codecs.json`: driver/native-JSON comparison captured at the pre-port
  commit. SHA256:
  `1426e49d1c75a1cf9b92ede913ce3e5e693a85ea8b181d625a310a56c412b9bf`.

The HTTP matrix covers visibility, non-deleted/retired organization membership,
scope/type/search filters, ordering, raw creator display, create/update/duplicate
projection, usage counts, and a committed JSONB-null/public-response500. Historical
and unknown-role tokens received401 upstream; the query itself did not add a new
role check. IDs, display IDs, generated timestamps and the run suffix are aliased;
fixed2020 timestamps retain their literal values. Source metadata and the temporary
database name are evidence, not values to compare across new-owner runs.

Only schema-valid states were seeded. In particular the two template CHECK
constraints forbid unknown scope and organization scope without an organization
ID; no constraint was removed. Organization has no active/deleted-at field.

The scalar probe proves UTC binary timestamps versus session-zone JSON output,
naive datetime extrema for PostgreSQL timestamp infinities, and native JSON's
loss of float negative zero. The read protocol must preserve those distinctions.

Both captures used verified disposable verifier resources, which were destroyed.
The original immutable files and execution reports remain under Root `.worktree`.
