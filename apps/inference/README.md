# aap-inference

`aap-inference` contains the portable Pydantic values and pure algorithms used
to validate saved inference pipelines and normalize backend capability
snapshots. It imports no API application, ORM or settings module. The shared
protocol package still brings its FastAPI error helpers and cryptographic
lifecycle dependencies; this extraction does not split that existing package.

The optional `probe` extra provides the existing authenticated HTTP GET to a
backend's `/setup` endpoint. Callers provide the endpoint locator and health
timeout explicitly. Capability probing fails open and returns `None` when the
request or normalization fails; direct setup fetching preserves the HTTP client
errors.

```sh
uv run --project apps/inference --extra test pytest apps/inference/tests
```

Tracker window limits are passed as `CapabilityLimits`; defaults match the API
configuration, while application callers should pass their configured values.
