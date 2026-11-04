---
name: aap-sdk-contracts
description: Extend or verify the Python SDK, CLI, and TUI against this repository's API snapshot, coverage manifest, and independent SDK release metadata.
---

# Python SDK contracts

Start with `packages/python-sdk/api-coverage.toml`, `packages/python-sdk/src/yoao/client.py`, the affected CLI/TUI entry, and `apps/api/openapi.snapshot.json`. The adjacent `tests/test_openapi_contract.py` checks real SDK call sites and coverage classifications.

Trace endpoint authorization, serializer shape, pagination, job responses and error behavior before exposing a new consumer method. Update the coverage entry and typed models with the implementation. Keep covered, excluded and planned capabilities meaningful; do not mark an endpoint covered just because the backend has it.

For parent/child resources, validate the relationship used by the operation, not only each ID's existence. Destructive CLI actions need the existing interactive/automation confirmation behavior; rejected actions must send no mutation request. Preserve JSON output and exit-code contracts.

Check optional installation extras: a core-only install must not crash merely because CLI/TUI dependencies are absent. Verify the affected package entry point when changing packaging.

SDK version and target application version are independent static literals in `packages/python-sdk/src/yoao/_version.py`. Update them only for the requested SDK release/compatibility change, keeping `pyproject.toml` metadata and CLI version output consistent. Do not bump the application as a side effect.

Use the package's existing tests, including the OpenAPI contract test, and a wheel/build check for packaging changes. A stale ignored frontend client is a separate artifact issue: use root `pnpm codegen` rather than editing generated files.
