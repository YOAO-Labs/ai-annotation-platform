# Jobs list-query Node acceptance

Run `pnpm run check` in this package with Node 22 or newer. The command validates
JavaScript syntax and checks that the packaged Jobs schema and copied migration
SQL match without connecting to a database.

`node list_query_acceptance.mjs --live` additionally requires the pinned
`pg@8.16.3` driver and a disposable database connection as the dedicated
`jobs_list_query_client` LOGIN. It checks the page and total ABI, returned
PostgreSQL types, row order, exact function grants, and denial of private/helper
access. Provide the scoped fixture values through these environment variables:

The live command requires `JOBS_LIST_QUERY_DATABASE_URL`,
`JOBS_LIST_QUERY_EXPECTED_ROLE`, `JOBS_LIST_QUERY_ACTOR_ID`,
`JOBS_LIST_QUERY_PLATFORM_ROLE`, `JOBS_LIST_QUERY_EXPECTED_TOTAL`, and
`JOBS_LIST_QUERY_EXPECTED_PAGE_IDS` (a JSON UUID array). Optional filters are
`JOBS_LIST_QUERY_STATUSES`, `JOBS_LIST_QUERY_KINDS`,
`JOBS_LIST_QUERY_PROJECT_ID`, `JOBS_LIST_QUERY_SEARCH_PATTERN`,
`JOBS_LIST_QUERY_LIMIT`, and `JOBS_LIST_QUERY_OFFSET`.

The caller supplies the disposable database URL, expected role, actor snapshot,
filters, expected total, and expected ordered page IDs after applying the
migration and scoped grant profile.
