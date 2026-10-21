# Inference service contracts

This package defines private protocols between Workspace and credential-owning Inference services. Backend setup probes and health observations carry opaque encrypted Native subjects; neither wire carries backend URLs or authentication credentials as plaintext fields. The Inference owner opens each subject only while its originating PostgreSQL transaction is still in progress.

The wire is versioned as `aap-inference-backend-probe/v1`. Requests are limited to 16 MiB and replies to 4096 bytes. The optional HTTP client sends exactly one authenticated `POST /v1/backends/probe`, disables redirects and environment proxy lookup, and never retries or selects another provider. HTTP/provider protocol failures raise a sanitized `BackendProbeUnavailable`; a valid legacy materialization failure raises `BackendProbeLegacyFailure` from `reply_modalities`.

Importing the value and contract modules requires only Pydantic. Install the `client` extra to use `BackendProbeHttpClient` and `BackendHealthObservationHttpClient`, or the `test` extra to run the isolated contract tests.

The health observation contract carries an opaque encrypted subject and returns separately sealed evidence bound to the request, registry, and subject digest. Its request and reply are each limited to 24 MiB; `BackendHealthObservationHttpClient` sends one authenticated `POST /v1/backends/health-observation` without retry, redirect, or provider fallback.
