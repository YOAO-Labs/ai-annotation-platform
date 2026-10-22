# Inference service contracts

This package defines shared preannotation structure and private protocols between Workspace and credential-owning Inference services. Backend setup probes and health observations carry opaque encrypted Native subjects; neither wire carries backend URLs or authentication credentials as plaintext fields. The Inference owner opens each subject only while its originating PostgreSQL transaction is still in progress.

The wire is versioned as `aap-inference-backend-probe/v1`. Requests are limited to 16 MiB and replies to 4096 bytes. The optional HTTP client sends exactly one authenticated `POST /v1/backends/probe`, disables redirects and environment proxy lookup, and never retries or selects another provider. HTTP/provider protocol failures raise a sanitized `BackendProbeUnavailable`; a valid legacy materialization failure raises `BackendProbeLegacyFailure` from `reply_modalities`.

Importing the value and contract modules requires Pydantic and the shared task contract. Install the `client` extra to use `BackendProbeHttpClient` and `BackendHealthObservationHttpClient`, or the `test` extra to run the isolated contract tests.

The health observation contract carries an opaque encrypted subject and returns separately sealed evidence bound to the request, registry, and subject digest. Its request and reply are each limited to 24 MiB; `BackendHealthObservationHttpClient` sends one authenticated `POST /v1/backends/health-observation` without retry, redirect, or provider fallback.

The preannotation pipeline models and `validate_saved_pipeline` define structural input and canonicalization rules shared by Inference and Workspace. Validation enforces the existing `PipelineStage` fields, parent tree, write targets, conflict policy, and task-name normalization; it does not check backend existence, project authorization, or backend availability. Those checks remain with their existing owners. `PipelineStage.model_json_schema()` and `PreannotateRequest.model_json_schema()` are the schema sources; generated JSON Schema belongs in `src/aap_inference_contract/preannotate.schema.json` and must not be hand-authored.

## Preannotation structure

`preannotate.schema.json` publishes the canonical `PreannotateRequest` JSON Schema, including `PipelineStage` in `$defs`. Regenerate it from `PreannotateRequest.model_json_schema()` using the current package; the checked-in artifact test checks that it matches the model. The schema describes canonical values. The compatibility parser also accepts the existing task aliases and Pydantic coercions; the captured legacy vectors in `tests/fixtures/preannotate_legacy.json` preserve representative normalized outputs and exact failure details.

Semantic rules supplement the field schema:

- Exactly one stage has `parent_stage=null` or omits it. Stage IDs are unique and need not start at zero. The root supplies the request's backend, model, task, prompt type, variants, parameters and class filter.
- Each child names an earlier, defined parent. Dependencies are acyclic and at most three stages deep; parents consumed by children must produce `geometry` or `intermediate` output.
- Children can write `attributes`, `geometry` or `intermediate`; only `target_stage=root` is supported. `drop_box` is valid only for a direct child of the root.
- For children, ROI mode is `crop` or `geometry`, with padding in `[0, 0.5]`. Explicit downstream input mode is `crop` or `geometry`.
- Attribute conflicts use the final key after the optional label prefix. The default rejects repeated keys; the full request also exposes `on_key_conflict=last_wins`. Geometry/intermediate writes do not participate in this check.

`validate_saved_pipeline` takes the saved stage array, retains the legacy falsey-value behavior, checks the single root, then validates and serializes stages with unset fields excluded. It preserves array order and uses the default conflict policy. A failure raises `PipelineValidationError` with its existing `detail`; backend existence, admission, availability and transaction ownership stay with their existing owners. Inference's former import paths remain aliases to these shared objects.

The model description documents the current tree semantics; the legacy fixture retains its captured historical description for comparison. Only that description is excluded from schema-field regression comparisons.

Before sealing, Inference encodes the private health outcome with `health_observation_outcome_text`; the API decodes the verified text with `decode_health_observation_outcome`. This private body is limited to 16 MiB and accepts Python's legacy `NaN`, `Infinity`, and `-Infinity` number tokens to preserve the original response-materialization phase. The HTTP request and reply envelopes remain strict finite JSON and contain only opaque encrypted Native values.
