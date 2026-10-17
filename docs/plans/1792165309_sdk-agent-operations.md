# Agent-readable SDK operations and export contract completion

> Status: implemented, locally validated, and included in the SDK follow-up commit on 2026-10-17; authorized by the user on 2026-10-16.
> Base: `0ec451824e052730adb63495d8d2e5b5cfc858b4`, the locally committed SDK capability alignment.

## Outcome contract

Complete the three priorities accepted after the SDK audit: correct the existing export boundary, describe capabilities/requests without requiring repository access, and provide opt-in structured CLI errors. Reuse the synchronous Client, Pydantic request models, Typer commands, atomic streaming transport, and existing authorization/confirmation behavior.

The first requested local commit is already complete. The user requested a separate local commit for this follow-up on 2026-10-17; no push, publication, SDK/application release number, database migration, or service deployment is requested.

## Current evidence

- OpenAPI baseline is `0.26.1` with 568 HTTP operations. The previous manifest watches 363: 196 covered and 167 excluded; another 205 operations are outside that inventory. The committed SDK suite passed 436 tests.
- Project/batch export API accepts JSON `ExportRequestBody` containing video `scope` and `lidar` options. Existing SDK helpers only send query parameters.
- Project VOC export returns synchronous ZIP bytes; `Exports.create` assumes a JSON job response. The batch helper explicitly rejects VOC. Offline mock-response diagnosis reproduced the project `JSONDecodeError` and missing JSON body.
- Learning command help exposes `--body PATH` without a discoverable request schema. Pydantic already exports model JSON Schema, but operation constraints can exceed a shared model's schema: run creation requires the preflight digest even when the preflight request model allows omission.
- `--json` preserves undecorated successful stdout, while errors are plain stderr. Preserve those existing defaults and exit-code conventions.
- The source coverage manifest is not included in the current wheel. Agent discovery must work from an installed wheel, offline and without credentials.

## Scope and minimal design

Implement this without new dependencies or a new server. The work reaches more than eight files because transport, public models/methods, CLI, packaged metadata, contract checks, installation checks, and official documentation share the behavior.

```text
OpenAPI + coverage classifications + SDK/CLI declarations
                     |
          reproducible packaged operation catalog
                     |
      Python describe/list functions + aap capabilities/describe

Typed requests / stdin -> existing Client -> existing HTTP transport -> API
                                      -> atomic streamed VOC/download output
```

### A. Complete export requests and synchronous VOC downloads

- Add standalone SDK export request models matching `apps/api/app/schemas/export.py`: video frame/segment selections, video scope, lidar options, and `ExportRequestBody`. They remain core-only and forbid unknown fields.
- Add keyword-only `body: ExportRequestBody | None` to project and batch async export helpers. Keep existing query options and return values. The JSON body uses JSON-mode/alias/explicit-field serialization; it must not be moved into query parameters.
- Reject misplaced `scope`/`lidar` query kwargs with a clear SDK error rather than silently exporting with an ignored range.
- Preserve async job helpers as async job helpers. Reject `voc` there with guidance to explicit `exports.download_voc(project_id, dest, include_attributes=True)` and `batches.download_voc(project_id, batch_id, dest, include_attributes=True)` methods.
- Extend the existing atomic `stream_download` with a method keyword defaulting to GET. VOC uses authenticated POST with a fixed relative `/api/v1/.../export` path and query `targets=["voc"]`; it streams without a mutation retry or intermediate in-memory ZIP.
- CLI project/batch export exposes `--body`; project VOC takes the synchronous download branch, requires an output destination and rejects async-only options such as `--no-wait`. Existing async JSON results remain unchanged. Batch export gains an explicit VOC download command rather than returning a fictitious job ID.
- The call-site contract extractor must recognize explicit literal stream HTTP methods. Both VOC methods reuse existing API operations, so the distinct covered endpoint count remains 196.

Acceptance: correct JSON/query separation, frame-order and request validation, UUID/alias/explicit-null handling, no mutation on local rejection, streamed binary bytes, auth preservation, interruption/HTTP-error destination preservation, no POST retry, unchanged async workflows, human/JSON CLI behavior, and POST streaming call-site accounting.

### B. Account for all API operations and ship offline discovery

- Extend the coverage manifest to the entire snapshot, including future tags, with a wildcard watch scope. Preserve the 196 covered and 167 deliberately excluded classifications.
- Classify the 205 previously untracked operations as `uncovered` with reason `not-implemented`, instead of guessing that they lack external demand or promising an unassigned release. Keep `covered`, `excluded`, and legacy `planned` meaningful. Expected inventory: 568 = 196 covered + 167 excluded + 205 uncovered.
- Generate one compact packaged catalog from the current OpenAPI/coverage and actual SDK/CLI declarations. Do not maintain a second handwritten list of HTTP routes. Add a check mode and contract test for drift; the installed wheel must not depend on monorepo files or test modules.
- Expose core-only Python `list_capabilities(...)` and `describe_operation(name)` functions, and CLI `aap capabilities --json` / `aap describe <operation> --json`. Use bounded pagination/tag/status selection for the operation list; describe expands one selected operation.
- Support canonical SDK method names such as `training_runs.create` and the corresponding CLI dotted path `learning.runs.create`. Return SDK method identity, available CLI path(s), parameters/defaults, relevant API method/path(s), request schema where a typed model exists, response type, and operation constraints/examples.
- Request/response schema comes from existing models or the selected backend contract. Keep Python-only callbacks and opaque kwargs accurately described; do not claim every Python argument is a JSON-invocable tool.
- Include explicit constraints for preflight digests, stable request IDs, CAS revisions/generations, domain states/cursors, and confirmations. Clearly distinguish CLI confirmation from Python caller responsibility. Static metadata does not grant permission or prove runtime availability; the server remains authoritative.
- Document known domain effects instead of equating every GET with no server-side effects. Training queue/list reads can reconcile lost execution state. Do not infer permissions solely from HTTP verbs.
- Source field descriptions explain ambiguous identities such as frozen training-version `dataset_id`, task-stream IDs, artifact identity, cursor direction, and budget units. Any operation metadata override has a concrete existing rule as its owner.

Acceptance: all 568 classified exactly once, covered matches real calls, catalog regeneration/check is deterministic, unknown names/statuses fail clearly, bounded/filterable discovery, alias resolution, schema plus create-only constraints, no credentials/network needed, packaged catalog present in wheel, and core-only import remains independent of CLI/TUI extras.

### C. Structured errors and stdin without changing old defaults

- Add root `--error-format text|json`, default text. In JSON mode emit one structured error object to stderr with error kind, HTTP status when present, server error code/detail, and sanitized field locations; successful stdout and existing exit codes retain their current contracts.
- Cover local request/file validation, HTTP errors, network failures, confirmation refusal, and CLI usage errors. Avoid duplicate JSON envelopes and avoid leaking request input, API keys, or traceback text. Do not fabricate a server error code or automatic recovery instruction.
- SDK HTTP exceptions may gain optional response request-ID/retry-after context when available, preserving existing constructor/attributes. Retry safety is operation-dependent: never interpret an ambiguous POST result as permission to submit a new request ID.
- Reset the selected error format for every invocation, including repeated CliRunner calls in the same process. Verify the actual console-script entry point as well as CliRunner behavior.
- Let the existing JSON body reader accept `Path("-")` as stdin, applying exactly the same parsing/model rules. Stable IDs/digests remain explicit caller input; stdin does not create an implicit idempotency store.
- The installed Typer uses its own Click subset and has no `Context.to_info_dict`; implementation must use the actual available command/parameter surface rather than assume the upstream Click convenience API exists.

Acceptance: opt-in structured stderr for 401/403/404/409/422/429/503 and network/local/usage/refusal errors, successful stdout unchanged, no secret/input values, stable exit codes, no cross-invocation mode leakage, no mutation on validation/refusal, stdin/file equivalence, and core-only console-shim behavior.

## Delivery ownership and integration

- Export worker: export models, Client export methods, CLI export/batch commands, stream method support, and export regressions. It may extend stream-related guard tests.
- Discovery worker: complete manifest, packaged catalog/generator, core discovery functions, discovery CLI, inventory/discovery tests, and package-data metadata. It may extend coverage-related guard tests.
- Error worker: shared CLI error/body handling, root error-format option, SDK exception context if needed, console entry point, and error/stdin tests.
- Parent: formal docs/changelogs, root CLI/public export wiring as needed, integration conflict resolution, catalog regeneration after all worker declarations land, final acceptance, and cleanup. Workers use isolated worktrees from the current local committed base and return actual local commits; no worker pushes.

Changes to shared files must preserve the other worker's owner: streaming download and HTTP error mapping in `_http.py`, stream-method versus manifest checks in `test_openapi_contract.py`, and discovery-command registration versus error-format setup in `cli/main.py`. Integrate sequentially and run acceptance in the originating checkout.

## Documentation and checks

Update package README/changelog, root Unreleased changelog, `docs-site/dev/sdk/{python-client,cli,cookbook,quickstart}.md`, and an agent workflow reference under the existing SDK documentation. Explain discovery → schema/constraints → project access → preflight → persist/replay request → submit → bounded status reads → verified download. No separate agent runtime or setup wizard is needed.

Use the existing worktree SDK environment and repository checks:

```sh
rtk proxy packages/python-sdk/.venv/bin/python -m pytest packages/python-sdk/tests -q
rtk proxy uvx --from ruff==0.15.22 ruff check packages/python-sdk
rtk proxy uvx --from ruff==0.15.22 ruff format --check packages/python-sdk
rtk proxy pnpm docs:build
rtk proxy git diff --check
```

Also run the catalog generator's check mode, `uv build` from the package, and fresh core/CLI/TUI wheel smoke checks outside the checkout. Verify describe/list/schema and structured usage/HTTP errors through the installed CLI. No API/backend change requires runtime refresh; live database/GPU acceptance remains a separately stated prerequisite, not a mocked-test claim.

## Risks and rollback

The fragile premise is that machine descriptions stay attached to the actual installed code. A checked generated catalog plus runtime model schemas and wheel smoke checks address this; no manual full copy of OpenAPI is authoritative. Descriptions of dynamic callback/kwargs paths and permissions must remain honest.

Existing scripts depend on bare successful JSON and text stderr; structured errors are therefore opt-in. Revert code/package installation to roll back; this work has no data migration. An export request already accepted by the API cannot be undone by reverting the SDK. Preserve the old API target/version unless compatibility/release scope changes explicitly.

At completion append `## Outcome` with actual checks, documentation paths, source commits, and live limitations. The user's initial commit request was fulfilled by `0ec451824`; the subsequent local commit request covers this completed follow-up.

## Outcome

The initial capability update is locally committed as `0ec451824e052730adb63495d8d2e5b5cfc858b4` (`feat(sdk): align training and learning capabilities`). This follow-up is integrated in the originating worktree and included in the separate local commit `feat(sdk): add offline discovery and complete export contracts`. Nothing was pushed or released; SDK `0.20.0` and compatibility target `0.26.1` are unchanged.

Integrated worker commits:

- Export contracts: `bbaf4ba162cfe0110f012c2b89bdb23f18410b6e`.
- Structured errors and stdin: `3c351da9d176bbede60239460dca4701e3651b7d`.
- Offline discovery and inventory: `59a0e7ee42120557be16120d0ecd601fbe5143a2`.

Project/batch async exports now send typed JSON bodies, and explicit VOC helpers stream synchronous authenticated POST responses atomically. The full API inventory is 568 operations: 196 covered, 167 excluded, 205 uncovered, and zero planned. Wildcard tag checking detects future inventory drift; it does not claim complete business-semantic parity.

The generated package catalog supports bounded offline listing, SDK/CLI names, multi-route helpers, runtime request/response schemas, and explicit preflight/CAS/domain constraints. The parent integrated public exports and root commands, added Study/export constraints and field descriptions, preserved selected SDK method identity on shared routes, and retained schema union structure. Source generation remains monorepo-only; installed discovery does not require repository files, credentials, or HTTP.

Structured errors preserve existing success/exit defaults, sanitize fields, retain Job IDs for recovery, and suppress stderr progress in JSON-error mode. Cross-feature checks cover typed stdin submissions with stable UUIDs/digests and explicit nulls, 409/422 failures without repeated POST, synchronous/asynchronous export discovery, and progress/error separation. Compatibility smoke found old Pydantic's broad `model_` protection emitted import warnings; model configs now protect actual model methods while allowing existing API fields, keeping machine stderr clean.

Local verification:

- SDK suite: **508 passed**, Python 3.11.14, including export, coverage, discovery, structured error, stdin, and existing TUI behavior checks.
- Catalog generator `--check`, offline expansion of all 200 SDK method descriptions, Ruff 0.15.22 check/format, and `git diff --check` passed.
- Documentation build, frontmatter (174 files), navigation (177 pages), and syntax parsing of 21 Python documentation snippets passed. Existing syntax-highlighter and bundle-size warnings remain advisory.
- `uv build` produced wheel and sdist with the generated catalog and English `AGENT_GUIDE.md`. Fresh core, CLI, and TUI wheel smoke checks passed outside the checkout. Core-only discovery required no Typer/Textual; the console shim returned its structured extras-install hint.
- Fresh dependencies resolved Pydantic 2.14.1, Typer 0.27.3, and Textual 8.2.8. An additional CLI environment with Pydantic 2.5.3, Typer 0.12.5, and Click 8.1.8 passed discovery, usage errors, stdin validation, and clean stderr checks. These are installation smoke checks, not full-suite claims for each dependency combination.
- Installed console commands passed HTTP 403/409 and typed stdin submission against an ephemeral loopback test server with both CLI dependency sets. These responses are test fixtures, not acceptance against a running application API.

Artifacts remain under `packages/python-sdk/dist/`:

- Wheel SHA-256: `52d1e96984a5862268d9b778dd2571fe73156e19cbc340aaf2a3639fb0d4e197`.
- Sdist SHA-256: `4654819c35ad9c35bd79ff10b3c1490d1761e8f624a073aa14af7a8bd81d61da`.

Documentation is in `docs-site/dev/sdk/agents.md`, the SDK reference/CLI/Cookbook/quickstart, package README/changelog, and root README/Unreleased changelog. The installed English guide is available via `importlib.resources`.

The three task-created worker worktrees were removed through Orca after integration and validation; their source branches were retained. Temporary installation environments/test servers were cleaned up. The originating SDK environment and distribution artifacts remain available.

No live API/database, executor/GPU, browser, or remote CI acceptance was performed for this follow-up. No runtime/API/worker code changed, and no service restart or migration was necessary. The 205 uncovered operations remain a real SDK scope gap; coverage classifications and offline metadata do not grant permissions or prove executor availability.
