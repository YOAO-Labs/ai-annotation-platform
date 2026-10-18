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

## Local cost breakdown

From the repository root, use the configured API Python environment:

```sh
PYTHONPATH=apps/api /path/to/api-venv/bin/python apps/api/experiments/bench_cost.py
```

This compares the Python reducer, Python JSON stages, Go decode/reduce/encode benchmarks, and localhost adapter paths on one deterministic synthetic fixture. It prints JSON measurements, builds the Go binary in a temporary directory, binds an ephemeral loopback port, and runs one bounded request near 7 MiB while sampling process RSS every 50 ms. `EXPERIMENT_COST.md` records the checked-in run; the request ceiling is 8 MiB. It does not use a database, production data, external traffic, or concurrent stress. The report's sampled RSS is not a process high-water mark or proof of production capacity.

## Local concurrency observations

Use `apps/api/experiments/bench_concurrency.py` from the repository root. With no peer URL it builds and cleans up its own empty-environment Go process. To target the separate local experiment container explicitly:

```sh
PYTHONPATH=apps/api /path/to/api-venv/bin/python apps/api/experiments/bench_concurrency.py \
  --base-url http://127.0.0.1:8091 --rounds 20 > local-audit-concurrency.json
```

The URL is limited to HTTP loopback. The matrix uses 10,000/50,000 rows, client concurrency 1/2/4/8 and at most 20 bursts per pair. It validates every successful response against Python and records raw samples, nearest-rank p50/p95, successful throughput, typed retryable 503s and unexpected errors. Timing includes response validation; request encoding and fixture generation are excluded. The command exits nonzero on unexpected failures and has no timing-based production SLO assertion. For an external container, its resources must be recorded separately; `--peer-pid` can observe a local process with Linux VmHWM or explicitly labelled 50 ms sampled RSS.

The checked-in observations in `EXPERIMENT_COST.md` used an experiment-only Compose override with `cpus: 2.0` and `mem_limit: 512m`; the normal Compose files were unchanged. Large uploads beyond the two-slot limit produced transport errors, so the overload matrix is not qualified as a production-capacity pass.

`tools/cgroup-probe/main.go` reads Linux cgroup v2 memory high-water, limits, OOM events and CPU accounting without adding a service endpoint. For a task-owned scratch experiment container, compile that helper for the container's architecture, copy it into that container, and execute it before and after the run:

```sh
probe_binary=$(mktemp -t aap-cgroup-probe.XXXXXX)
experiment_project=aap-audit-summary-experiment
container_id=$(docker compose -p "$experiment_project" -f docker-compose.experiments.yml \
  --profile audit-summary-experiment ps -q audit-summary)
test -n "$container_id"
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath \
  -o "$probe_binary" apps/audit-summary/tools/cgroup-probe/main.go
chmod 755 "$probe_binary"
docker cp "$probe_binary" "$container_id":/cgroup-probe
docker exec "$container_id" /cgroup-probe
rm "$probe_binary"
```

Set `experiment_project` to the task-owned project you actually started, and choose `GOARCH` from its container architecture. The probe is not included in the normal scratch image; its small process and regular health checks are included in the cgroup lifetime high-water. Remove the owned experiment container and temporary probe after verification; leave the default API and infrastructure intact.
