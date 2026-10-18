# Audit summary experiment cost breakdown

Observed on 2026-10-18, macOS 27.0.1 arm64: Python 3.11.14, httpx 0.28.1, Pydantic 2.13.3, Go 1.27.2.

The canonical synthetic input has 10,000 grouped rows (1,456,306 request bytes, 4,847 response bytes), spread over 29 dates, 32 actions, 16 target types, 8 roles and status families 0/2/3/4/5. Counts exceed JavaScript's exact-integer range. This is a newly reproducible fixture; do not treat its timings as a same-input rerun of the earlier one-off measurement.

Median local time per call:

| Stage                                    | ms/call | Sampling                       |
| ---------------------------------------- | ------: | ------------------------------ |
| Python reducer                           |   2.975 | 5 × 10 calls/sample            |
| Python request JSON encode               |  14.873 | 5 × 2                          |
| Python response JSON decode + validation |   0.042 | 5 × 10                         |
| Go request decode                        |  25.173 | Go benchmark, 3 runs           |
| Go summarize                             |   5.328 | Go benchmark, 3 runs           |
| Go response JSON encode                  |   0.006 | Go benchmark, 3 runs           |
| Pre-encoded loopback HTTP path           |  34.089 | 5 × 2; includes Go server work |
| Full adapter with a new client           |  50.788 | paired 5 × 5                   |
| Full adapter with a reused client        |  46.933 | paired 5 × 5                   |
| AsyncClient construct and close          |   2.313 | 5 × 20; no request             |

Go allocation measurements (median of three runs):

| Stage           |       B/op | allocs/op | Iterations per run     |
| --------------- | ---------: | --------: | ---------------------- |
| Request decode  | 26,061,403 |   325,420 | 8, 8, 8                |
| Summarize       |    664,601 |    40,536 | 45, 45, 44             |
| Response encode |      5,223 |         3 | 38,410, 35,551, 37,780 |

One bounded localhost request repeated the fixture five times: 50,000 rows and 7,281,306 request bytes, below the 8,388,608-byte local ceiling. It returned HTTP 200 with exactly five times the event and error totals. RSS sampled every 50 ms was 65,776 KiB for Python and 56,272 KiB for Go, across five samples. These sampled values are not process high-water marks or concurrency capacity evidence.

The pre-encoded HTTP path includes Go's handler, body read, decode, reducer, response encoding, loopback transport and Python byte receipt. Full adapter timings include Python request encoding, client lifecycle, HTTP, Go processing and Python response validation. These stages overlap; do not subtract or add them to infer pure network cost. An earlier paired run measured 54.271 ms with a fresh client and 55.685 ms with reuse; the direction changed between runs, so this does not establish a reliable client reuse benefit.

This evidence is synthetic and local. It includes no SQL, database, production data, deployment, concurrent load or production cardinality. Under this fixture Python's local reducer is inexpensive; the Go path adds encoding and process communication, with request decoding the largest measured Go stage. The measurements do not show a user benefit for adopting Go, so it remains an experiment.

The benchmark builds with a small allowlist of Go build/cache environment values and starts the Go server with an empty environment. It never passes business credentials to the server. Reproduce from the repository root with the configured API Python environment:

```sh
PYTHONPATH=apps/api /path/to/api-venv/bin/python apps/api/experiments/bench_cost.py
```

## Local container concurrency qualification

The separate experiment Compose project was built and run on the same development machine with a task-only limit of two CPUs and 512 MiB. It ran as `65532:65532`, exposed only loopback port 8091, and had no business credentials or volume mounts. Health, the served OpenAPI, and the mixed golden response matched exactly. The normal API continued using its local Python reducer.

`bench_concurrency.py` sent 20 synchronized bursts for every row-count/client-concurrency pair: 600 requests in 44.667 seconds. Payloads were pre-encoded outside timing; latency includes HTTP, server processing, Python response decoding and oracle validation. Each successful response matched the Python oracle. Percentiles use nearest rank. Client concurrency is offered load, not a promise that all requests simultaneously hold server slots.

| Rows   | Request bytes | Clients | Accepted | HTTP 503 | Transport errors | Success p95 ms | Accepted/s |
| ------ | ------------- | ------- | -------- | -------- | ---------------- | -------------- | ---------- |
| 10,000 | 1,456,306     | 1       | 20       | 0        | 0                | 49.558         | 21.929     |
| 10,000 | 1,456,306     | 2       | 40       | 0        | 0                | 59.731         | 34.246     |
| 10,000 | 1,456,306     | 4       | 40       | 33       | 7                | 71.128         | 8.704      |
| 10,000 | 1,456,306     | 8       | 40       | 101      | 19               | 79.467         | 5.230      |
| 50,000 | 7,281,306     | 1       | 20       | 0        | 0                | 210.816        | 4.896      |
| 50,000 | 7,281,306     | 2       | 40       | 0        | 0                | 261.231        | 7.787      |
| 50,000 | 7,281,306     | 4       | 40       | 0        | 40               | 293.155        | 3.844      |
| 50,000 | 7,281,306     | 8       | 40       | 0        | 120              | 293.689        | 3.759      |

The Linux cgroup lifetime memory high-water was 170,659,840 bytes (162.75 MiB), including the service, normal health checks and a temporary read-only probe. Memory limit events and OOM kills were zero; swap usage was zero. CPU accounting increased by 37.055 seconds, with 21 throttled periods and 50.679 ms of throttling. This is an actual cgroup high-water reading, distinct from the earlier sampled RSS. It does not qualify two concurrent requests at the full 64 MiB protocol ceiling: the largest tested payload was 7,281,306 bytes.

Qualification is limited. All 120 requests at one or two clients succeeded, but excess clients produced 134 observable retryable 503 responses and 186 HTTPX `ReadError` failures. These failures remain errors in the report and make the full over-capacity matrix exit nonzero. No resource exhaustion or incorrect summary was observed.

The transport failure was also reproduced without Docker. A separate socket probe that read the response before finishing its upload received a retryable 503 with `Connection: close`, while three full-body HTTPX uploads under the same held-slot condition failed after about 509–513 ms. This is consistent with Go HTTP/1.1 closing large unread request bodies after its 500 ms RST-avoidance delay while HTTPX sends the body before reading the response. It does not prove that every failed request received a complete 503 on the wire. The real slow-upload regression uses `Expect: 100-continue` to prove slot admission, 503 rejection, health availability and cancellation release without guessing delays.

The configured two-slot protection and successful path have local evidence; reliable large-upload overload responses are **not qualified** for the default HTTPX client. No production cardinality, target SLO, WAN, TLS/proxy path, database, GPU or production capacity is inferred. Go remains an unadopted experiment.

Raw observations are retained in `.worktree/local-audit-concurrency.json`, `.worktree/local-audit-concurrency-resources.json`, `.worktree/local-audit-overload-repro.json` and `.worktree/local-audit-standalone-repro.json` in the validating checkout.
