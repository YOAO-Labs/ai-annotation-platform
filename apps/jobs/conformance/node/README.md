# Jobs list-query Node acceptance

`list_query_acceptance.mjs --self-check` validates the packaged Jobs schema and
the copied migration SQL without a database. `--live` uses the `pg` peer in this
test-only package and a separate `jobs_list_query_client` LOGIN supplied by the
root-owned verifier. It checks the page and total ABI, returned PostgreSQL types,
row order, exact function grants, and denial of private/helper access.

The live command requires `JOBS_LIST_QUERY_DATABASE_URL`,
`JOBS_LIST_QUERY_EXPECTED_ROLE`, `JOBS_LIST_QUERY_ACTOR_ID`,
`JOBS_LIST_QUERY_PLATFORM_ROLE`, `JOBS_LIST_QUERY_EXPECTED_TOTAL`, and
`JOBS_LIST_QUERY_EXPECTED_PAGE_IDS` (a JSON UUID array). Optional filters are
`JOBS_LIST_QUERY_STATUSES`, `JOBS_LIST_QUERY_KINDS`,
`JOBS_LIST_QUERY_PROJECT_ID`, `JOBS_LIST_QUERY_SEARCH_PATTERN`,
`JOBS_LIST_QUERY_LIMIT`, and `JOBS_LIST_QUERY_OFFSET`.

The child worktree does not install dependencies or run the live check. The root
verifier supplies the disposable database URL, expected role, actor snapshot,
filters, expected total, and expected ordered page IDs after applying the
migration and scoped grant profile.
