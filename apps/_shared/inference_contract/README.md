# Inference backend probe contract

This package defines the small private API between Workspace and the credential-owning Inference probe. A request carries a registry ID and an opaque encrypted snapshot subject; it never carries a backend URL or authentication credential as a plaintext field. The Inference owner opens the subject only while its originating PostgreSQL transaction is still in progress, then probes `/health` and `/setup` using the exact captured registry values.

The wire is versioned as `aap-inference-backend-probe/v1`. Requests are limited to 16 MiB and replies to 4096 bytes. The optional HTTP client sends exactly one authenticated `POST /v1/backends/probe`, disables redirects and environment proxy lookup, and never retries or selects another provider. HTTP/provider protocol failures raise a sanitized `BackendProbeUnavailable`; a valid legacy materialization failure raises `BackendProbeLegacyFailure` from `reply_modalities`.

Importing the value and contract modules requires only Pydantic. Install the `client` extra to use `BackendProbeHttpClient`, or the `test` extra to run the isolated contract tests.
