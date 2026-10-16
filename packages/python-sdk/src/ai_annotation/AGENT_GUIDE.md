# Operating AAP through the Python SDK and CLI

Use the installed SDK's operation description before constructing an unfamiliar request. Metadata describes the installed package and its API baseline; it does not grant authorization or establish current server/executor availability.

## Discover only what you need

```sh
aap capabilities --status covered --limit 20 --offset 0 --json
aap capabilities --tag training-runs --json
aap describe training_runs.create --json
aap describe learning.runs.create --json
```

The SDK method name is canonical; an available CLI dotted path is an alias. Browse bounded pages and describe one operation at a time. `covered` means there is an SDK implementation, `excluded` records an intentional scope decision, and `uncovered` means the API exists but the SDK does not expose it. Check operation limitations and request constraints as well as the HTTP route.

Core-only Python consumers can use `list_capabilities()` and `describe_operation()` without CLI/TUI extras, credentials, or a running API. Python callbacks and opaque kwargs are not automatically JSON-invocable tools.

## Read identities and access before writing

- Use UUIDs for resources. A display ID, task-dataset stream ID, version ordinal, storage dataset ID, and frozen training-version ID are different identities.
- Training request `dataset_id` identifies a frozen training version. Keep its manifest/config/provenance and digests unchanged.
- Use `client.projects.access(project_id)` to inspect project access. The server still checks current roles, API-key scopes, resource relationships, qualification, quotas, and runtime availability.
- Some queue/list reads reconcile lost-execution status. Do not infer the absence of server-side effects solely from a GET method.
- Keep reads bounded. Use list limits/offsets or the operation's cursor; use `include_manifest=False` when the full frozen manifest is unnecessary.

## Preflight, preserve, then submit

1. Read the operation's request schema, defaults, and additional constraints.
2. Select explicit task/model/data identities and any native-compute or review budgets.
3. Call the resource's preflight/preview operation. Inspect readiness, exclusions, and qualification reasons.
4. Add the returned digest to the create request. Preserve the exact request and a caller-generated stable `request_id` before submission.
5. Submit once. If the result is uncertain, replay the same request only where the operation supports idempotency; do not substitute a new request ID.
6. Keep compare-and-swap values from the relevant read: policy revision, recipe head revision, or model release generation. A conflict requires a fresh read and a deliberate decision.

Run creation requires `preflight_digest`; Study creation requires `preview_digest`, with its request ID in `base.request_id`. A shared preflight model may allow a field that the creation operation requires. `configuration` and explicit legacy `parameters` must not be submitted together.

Complex CLI requests can use a UTF-8 JSON object file or stdin:

```sh
aap --error-format json learning runs preflight --project "$PROJECT_ID" --body run-draft.json --json
aap --error-format json learning runs create --project "$PROJECT_ID" --body - --json < run-request.json
```

`--error-format` is a root option and precedes the command. `--json` selects successful machine output on the leaf command. Protected commands require explicit `--yes` in noninteractive JSON mode. That flag confirms the requested action; it never overrides server authorization or a stale preflight/revision.

## Observe the owning resource

`202` means accepted, not completed. Async Job states are not interchangeable with training run/attempt, preview, evaluation, Study, or active-learning states.

- Read the resource ID returned by submission and its current attempt.
- Poll with an explicit time limit. On timeout keep the ID and current status for later observation; do not implicitly cancel, resume, or recreate it.
- `cancelling` or `lost` does not prove execution has stopped or the GPU lease is free. Read recovery eligibility and reasons before a deliberate resume.
- Keep logs/telemetry scoped to an exact attempt and bounded cursor page. Reset attempt-local cursors when the current attempt changes. Logs accept one cursor direction at a time.
- Model registration, deployment, sample evaluation, human candidate decisions, and project publication are separate operations. None should be inferred from success of the preceding operation.

## Export the requested scope and verify output

Video export range and lidar options belong in typed `ExportRequestBody`, not query kwargs. Async project/batch export returns a job ID; wait/read that Job explicitly and then download its artifact.

VOC is synchronous binary output. Use `client.exports.download_voc(...)`, `client.batches.download_voc(...)`, `aap export project ... --target voc --out ...`, or `aap batches download-voc ... --out ...`. It has no async job ID.

Downloads retain platform authorization for relative API routes and isolate it from absolute presigned storage URLs. Available digest/size metadata is checked before atomic destination replacement; errors preserve the previous destination. Do not work around a hash mismatch by disabling verification or substituting live media for frozen evidence.

## Handle errors without guessing

With `--error-format json`, read one structured error from stderr and a nonzero exit status. Successful stdout retains the command's normal `--json` contract. Keep status/code/detail and sanitized field locations; absence of a server code is not evidence for an invented one.

Job failure and wait-timeout errors retain `job_id`; failures also retain `job_status`. Use that identity to observe the existing job instead of submitting another export. Raw job payloads and failure messages are omitted.

Authentication/permission errors, missing resources, stale preflight/CAS conflicts, request validation, throttling, and unavailable execution need different responses. Preserve the original request identity on ambiguous network failures. Retry-after context is advisory; it does not make a write automatically safe to repeat.

Never print API keys or full request input when reporting a failure. Core-only installation intentionally gives a CLI-extras installation hint if `aap` is invoked without its optional dependencies.
