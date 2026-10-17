# Audit summary experiment

This Go slice is an isolated experiment, not an adopted production service. The normal API computes monthly summaries locally in Python and never selects this adapter through configuration. Default/production Compose, worktree startup and ordinary CI do not start or depend on Go. The experiment has no database, storage, credential or external-service access.

The Python HTTP adapter and process tests live in `apps/api/experiments`, outside ordinary API test discovery and application images. Language replacement, wire precision and process recovery have been exercised; production adoption is explicitly out of scope until the maintainer changes that instruction.

## Run

Explicit experiment-only Compose, from the repository root:

```sh
docker compose -f docker-compose.experiments.yml --profile audit-summary-experiment up -d --build
docker compose -f docker-compose.experiments.yml --profile audit-summary-experiment down
```

This uses a separate project and loopback port. It does not reroute the normal API.

```sh
go run . -listen 127.0.0.1:8091
```

The process prints one JSON readiness record after binding the socket. `GET /health` reports protocol 1. `GET /openapi.json` serves the canonical wire contract in `openapi.json`.

In another terminal, `go run . -healthcheck` checks `localhost:8091/health` and exits.

For a container build:

```sh
docker build -t audit-summary .
docker run --rm -p 127.0.0.1:8091:8091 audit-summary
```

The container listens on `0.0.0.0:8091`; the executable defaults to `127.0.0.1:8091` when run directly.

## Contract

`POST /v1/audit/monthly-summary` accepts a `YYYY-MM` month, an explicitly present nullable `materialized_through`, and rows with every field present. Dates must be valid Gregorian dates, rows must belong to the requested month, labels may be empty strings, and status families are `0`, `2`, `3`, `4`, or `5`. Input event counts are canonical nonnegative decimal strings of at most 38 digits. The reducer sums them exactly and returns all event and error counts as decimal strings.

The response includes every UTC calendar day in the month, including leap days. Error counts include status families 4 and 5. Ranking is descending count then ascending key; `top_actions` contains at most ten entries, while target types and actor roles contain all entries. Request bodies are limited to 64 MiB and at most two summaries run concurrently.

Errors use `{ "code": string, "message": string, "retryable": boolean }`. Invalid input returns 422 and is not retryable; bodies over 64 MiB return 413 and are not retryable; full summary capacity returns 503 and is retryable. A client cancellation stops row reduction without retaining request state.

## Verify

```sh
go test -race ./...
go vet ./...
```

From `apps/api`, explicitly run `.venv/bin/python -m pytest experiments/tests` for the HTTP adapter, lost-response and process restart checks. The manual GitHub workflow is `Backend audit experiment`; it has no push or pull-request trigger.
