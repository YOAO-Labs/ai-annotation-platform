# aap-inference

`aap-inference` contains the portable Pydantic values and pure algorithms used
to validate saved inference pipelines and normalize backend capability
snapshots. It depends on the shared task and protocol vocabularies, and imports
no application, web framework, ORM, or settings module.

The optional `probe` extra provides the existing authenticated HTTP GET to a
backend's `/setup` endpoint. Callers provide the endpoint locator and health
timeout explicitly. Capability probing fails open and returns `None` when the
request or normalization fails; direct setup fetching preserves the HTTP client
errors.

```sh
uv run --project apps/inference --extra test pytest
```

Tracker window limits are passed as `CapabilityLimits`; defaults match the API
configuration, while application callers should pass their configured values.
